"""Read-only search and page retrieval for public HTTP(S) resources."""
from __future__ import annotations

from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
import http.client
import ipaddress
from itertools import zip_longest
import re
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree
import zlib

from remote_api import APICancelled


MAX_URL_CHARS = 8192
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 30000
MAX_LINKS = 30
MAX_REDIRECTS = 4
MAX_ADDRESSES = 6
REQUEST_TIMEOUT = 15
CONNECT_TIMEOUT = 2
SOCKET_TIMEOUT = 5
SEARCH_URL = 'https://www.bing.com/search'
SEARCH_PROVIDER = 'Bing RSS'
SEARCH_HOSTS = {'www.bing.com', 'cn.bing.com', 'global.bing.com'}
REDIRECTS = {301, 302, 303, 307, 308}
PAGE_TYPES = {'text/html', 'application/xhtml+xml', 'text/plain'}
RSS_TYPES = {'application/rss+xml', 'application/xml', 'text/xml'}
HOST_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', re.ASCII)
BINARY_CONTROL = re.compile(r'[\x00-\x08\x0b\x0e-\x1f]')
HTML_CHARSET = re.compile(rb'<meta\b[^>]{1,200}\bcharset\s*=\s*["\']?\s*([a-z0-9._-]+)',
                          re.IGNORECASE)
IPV6_TRANSITION = tuple(map(ipaddress.ip_network, ('2001::/32', '2002::/16')))
IPV6_GLOBAL_UNICAST = ipaddress.ip_network('2000::/3')
IPV6_DOCUMENTATION = ipaddress.ip_network('3fff::/20')
IPV4_TRANSITION = ipaddress.ip_network('192.88.99.0/24')
METADATA_IP = ipaddress.ip_address('168.63.129.16')


class WebError(ValueError):
    """A safe description of why a web operation failed."""


def _public_address(address):
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if address.version == 4:
        return address != METADATA_IP and address not in IPV4_TRANSITION
    return (address in IPV6_GLOBAL_UNICAST and address not in IPV6_DOCUMENTATION
            and not any(address in network for network in IPV6_TRANSITION))


def _check_url_text(value):
    if (not isinstance(value, str) or not value or len(value) > MAX_URL_CHARS
            or '\\' in value or any(ord(char) <= 32 or ord(char) == 127 or char.isspace()
                                    for char in value)):
        raise WebError('网页地址格式无效。')


def _parse_url(value):
    _check_url_text(value)
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise WebError('网页地址格式无效。') from None
    if parts.scheme not in ('http', 'https') or not host:
        raise WebError('仅支持公开的 HTTP 或 HTTPS 网页地址。')
    if '@' in parts.netloc or '%' in host or parts.netloc.endswith(':'):
        raise WebError('网页地址不能包含登录信息或无效的主机名。')
    if parts.netloc.startswith('['):
        suffix = parts.netloc.partition(']')[2]
        if suffix and not re.fullmatch(r':[0-9]+', suffix):
            raise WebError('网页地址格式无效。')
    if port is not None and port not in (80, 443):
        raise WebError('仅允许网页访问端口 80 和 443。')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode('idna').decode('ascii').removesuffix('.').lower()
        except UnicodeError:
            raise WebError('网页主机名无效。') from None
        labels = host.split('.')
        if len(host) > 253 or not all(HOST_LABEL.fullmatch(label) for label in labels):
            raise WebError('网页主机名无效。')
        if len(labels) < 2 or labels[-1] in {'localhost', 'local', 'internal', 'lan', 'home'}:
            raise WebError('仅允许访问公开网络上的主机。')
        authority = host
    else:
        if not _public_address(literal):
            raise WebError('仅允许连接公网 IP 地址。')
        host = str(literal)
        authority = f'[{host}]' if literal.version == 6 else host
    if port is not None:
        authority = f'{authority}:{port}'
    path = quote(parts.path or '/', safe="/:@!$&'()*+,;=-._~%")
    query = quote(parts.query, safe="/?:@!$&'()*+,;=-._~%[]")
    if len(path) + len(query) > MAX_URL_CHARS:
        raise WebError('网页地址过长。')
    url = urlunsplit((parts.scheme, authority, path, query, ''))
    return url, host, port if port is not None else (443 if parts.scheme == 'https' else 80)


def _resolve(host, port):
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        answers = socket.getaddrinfo(host + '.', port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(ipaddress.ip_address(row[4][0]) for row in answers))
    if not addresses or any(getattr(address, 'scope_id', None) or not _public_address(address)
                            for address in addresses):
        raise WebError('网页主机未解析到可访问的公网 IP 地址。')
    preferred = [address for address in addresses if address.version == addresses[0].version]
    alternate = [address for address in addresses if address.version != addresses[0].version]
    # Keep the resolver's first choice while trying the other family promptly.
    ordered = [address for pair in zip_longest(preferred, alternate)
               for address in pair if address is not None]
    return ordered[:MAX_ADDRESSES]


class _Request:
    def __init__(self, cancel_event):
        self.cancel_event = cancel_event
        self.deadline = time.monotonic() + REQUEST_TIMEOUT
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.connection = None
        self.stopped = False
        self.result = None
        self.error = None

    def check(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise APICancelled('已取消联网操作。')
        remaining = self.deadline - time.monotonic()
        if self.stopped or remaining <= 0:
            raise WebError('联网请求超时。')
        return remaining

    def register(self, connection):
        with self.lock:
            self.check()
            self.connection = connection

    def stop(self):
        with self.lock:
            self.stopped = True
            connection = self.connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def run(self, operation):
        def work():
            try:
                self.result = operation(self)
            except (APICancelled, WebError) as exc:
                self.error = exc
            except TimeoutError:
                self.error = WebError('联网请求超时。')
            except ssl.SSLError:
                self.error = WebError('无法建立通过验证的 HTTPS 连接。')
            except (OSError, http.client.HTTPException):
                self.error = WebError('无法连接网页服务，或服务中断了响应。')
            except (ValueError, RecursionError):
                self.error = WebError('网页服务返回了无法读取的内容。')
            finally:
                self.done.set()

        self.check()
        threading.Thread(target=work, daemon=True).start()
        try:
            while not self.done.wait(min(self.check(), .05)):
                pass
            self.check()
            if self.error is not None:
                raise self.error from None
            return self.result
        finally:
            if not self.done.is_set():
                self.stop()


def _connect(request, host, port, address, context):
    family = socket.AF_INET if address.version == 4 else socket.AF_INET6
    target = (str(address), port) if family == socket.AF_INET else (str(address), port, 0, 0)
    deadline = time.monotonic() + CONNECT_TIMEOUT
    connection = socket.socket(family, socket.SOCK_STREAM)
    try:
        request.register(connection)
        connection.settimeout(min(request.check(), CONNECT_TIMEOUT))
        connection.connect(target)
        if context is not None:
            connection = context.wrap_socket(connection, server_hostname=host,
                                             do_handshake_on_connect=False)
            request.register(connection)
            remaining = min(request.check(), deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError()
            connection.settimeout(remaining)
            connection.do_handshake()
        connection.settimeout(min(request.check(), SOCKET_TIMEOUT))
        return connection
    except (OSError, WebError, APICancelled):
        connection.close()
        raise


def _read_response(response, allowed_types):
    media = Message()
    media['content-type'] = response.getheader('Content-Type', '')
    content_type = media.get_content_type()
    if not media['content-type'] or content_type not in allowed_types:
        raise WebError('网页返回了不支持的内容类型。')
    encoding = response.getheader('Content-Encoding', 'identity').strip().lower()
    if encoding not in ('gzip', 'identity'):
        raise WebError('网页返回了不支持的内容编码。')
    content_length = response.length
    if content_length is not None and content_length > MAX_RESPONSE_BYTES:
        raise WebError('网页响应超过大小上限。')
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise WebError('网页响应超过大小上限。')
    if content_length is not None and len(body) != content_length:
        raise WebError('网页响应不完整。')
    if encoding == 'gzip':
        body = _decompress_gzip(body)
    charset = media.get_content_charset()
    if body.startswith((b'\xff\xfe', b'\xfe\xff')):
        charset = 'utf-16'
    elif charset is None and content_type == 'text/html':
        match = HTML_CHARSET.search(body[:1024])
        if match:
            charset = match[1].decode('ascii')
    try:
        decoded = body.decode(charset or 'utf-8-sig', errors='replace').removeprefix('\ufeff')
    except (LookupError, UnicodeError):
        decoded = body.decode('utf-8-sig', errors='replace')
    if BINARY_CONTROL.search(decoded):
        raise WebError('网页响应包含二进制内容。')
    return content_type, decoded


def _decompress_gzip(data):
    stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoded = stream.decompress(data, MAX_RESPONSE_BYTES + 1)
    except zlib.error:
        raise WebError('网页返回了无法读取的压缩内容。') from None
    if len(decoded) > MAX_RESPONSE_BYTES:
        raise WebError('网页解压后的响应超过大小上限。')
    if not stream.eof or stream.unused_data:
        raise WebError('网页压缩响应的边界无效。')
    return decoded


def _get_hop(request, parts, host, port, allowed_types):
    addresses = _resolve(host, port)
    context = None
    if parts.scheme == 'https':
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_default_certs()
    target = urlunsplit(('', '', parts.path, parts.query, ''))
    headers = {'Host': parts.netloc, 'User-Agent': 'Free-AI-Web/1.0',
               'Accept': ', '.join(sorted(allowed_types)), 'Accept-Encoding': 'gzip, identity',
               'Connection': 'close'}
    last_error = None
    for address in addresses:
        request.check()
        connection = http.client.HTTPConnection(host, port)
        connection.auto_open = False
        sock = None
        try:
            sock = _connect(request, host, port, address, context)
            connection.sock = sock
            request.check()
            connection.request('GET', target, headers=headers)
            with connection.getresponse() as response:
                if response.status in REDIRECTS:
                    return response.status, response.getheader('Location')
                if not 200 <= response.status < 300:
                    raise WebError(f'网页服务返回 HTTP {response.status}。')
                return response.status, _read_response(response, allowed_types)
        except (OSError, http.client.IncompleteRead) as exc:
            # Retry a transport failure only with another address verified for this hop.
            last_error = exc
        finally:
            connection.close()
            if sock is not None:
                sock.close()
    if last_error is None:
        raise WebError('网页主机未解析到可访问的公网 IP 地址。')
    raise last_error


def _download(value, request, allowed_types, *, allowed_hosts=None):
    for hop in range(MAX_REDIRECTS + 1):
        request.check()
        url, host, port = _parse_url(value)
        parts = urlsplit(url)
        if allowed_hosts is not None and (host not in allowed_hosts or parts.scheme != 'https' or port != 443):
            raise WebError('搜索服务跳转到了不受支持的地址。')
        status, result = _get_hop(request, parts, host, port, allowed_types)
        if status not in REDIRECTS:
            content_type, body = result
            return url, content_type, body
        if hop == MAX_REDIRECTS:
            raise WebError('网页跳转超过允许次数。')
        _check_url_text(result)
        value = urljoin(url, result)


def _compact(text):
    return ' '.join(text.split())


def _link(value, base=None):
    try:
        if base is not None:
            _check_url_text(value)
            value = urljoin(base, value)
        return _parse_url(value)[0]
    except WebError:
        return None


class _Page(HTMLParser):
    ignored_tags = {'script', 'style', 'template', 'noscript', 'svg', 'iframe', 'object'}
    block_tags = {'address', 'article', 'aside', 'blockquote', 'br', 'dd', 'div', 'dl', 'dt',
                  'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'li', 'main',
                  'nav', 'ol', 'p', 'pre', 'section', 'table', 'td', 'th', 'tr', 'ul'}

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.title = []
        self.text = []
        self.links = {}
        self.ignored = []
        self.in_head = False
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.ignored_tags:
            self.ignored.append(tag)
        if self.ignored:
            return
        if tag == 'head':
            self.in_head = True
        if tag == 'title':
            self.in_title = True
        if tag == 'body':
            self.in_head = self.in_title = False
        if self.in_head or self.in_title:
            return
        if tag in self.block_tags:
            self.text.append('\n')
        if tag == 'a' and len(self.links) < MAX_LINKS:
            href = dict(attrs).get('href')
            link = _link(href, self.url)
            if link and link != self.url:
                self.links[link] = None

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag == 'head':
            self.in_head = False
        if tag == 'title':
            self.in_title = False
        if tag in self.block_tags and not self.in_head and not self.in_title:
            self.text.append('\n')

    def handle_data(self, data):
        if self.ignored:
            return
        if self.in_title:
            self.title.append(data)
        elif not self.in_head:
            self.text.append(data.replace('\r', ' ').replace('\n', ' '))


def _extract_rss(body, limit):
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', body, re.IGNORECASE):
        raise WebError('搜索服务返回了不支持的 RSS 格式。')
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise WebError('搜索服务未返回可读取的 RSS 结果。') from None
    channel = root.find('channel') if root.tag == 'rss' else None
    if channel is None:
        raise WebError('搜索服务未返回可读取的 RSS 结果。')
    results = []
    known = set()
    items = channel.findall('item')
    for item in items:
        title = _compact(item.findtext('title', ''))[:300]
        url = _link(item.findtext('link', '').strip())
        if not title or url is None or url in known:
            continue
        description = _Page(url)
        description.feed(item.findtext('description', ''))
        description.close()
        results.append({'title': title, 'url': url,
                        'snippet': _compact(''.join(description.text))[:600]})
        known.add(url)
        if len(results) == limit:
            break
    if items and not results:
        raise WebError('搜索服务未返回可读取的 RSS 结果。')
    return results


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class WebTools:
    def __init__(self, cancel_event=None):
        self.cancel_event = cancel_event

    def search(self, query, limit=5):
        if not isinstance(query, str) or not query.strip() or len(query.strip()) > 512:
            raise WebError('搜索关键词必须为 1 至 512 字符。')
        if type(limit) is not int or not 1 <= limit <= 10:
            raise WebError('搜索结果数必须为 1 至 10。')
        query = query.strip()
        try:
            url = SEARCH_URL + '?' + urlencode({'q': query, 'format': 'rss', 'count': limit})
        except UnicodeError:
            raise WebError('搜索关键词包含无效字符。') from None

        def lookup(request):
            _, _, body = _download(url, request, RSS_TYPES, allowed_hosts=SEARCH_HOSTS)
            return {'query': query, 'results': _extract_rss(body, limit),
                    'provider': SEARCH_PROVIDER, 'fetched_at': _timestamp()}

        return _Request(self.cancel_event).run(lookup)

    def fetch(self, url, max_chars=12000):
        if type(max_chars) is not int or not 1 <= max_chars <= MAX_TEXT_CHARS:
            raise WebError(f'网页正文长度上限必须为 1 至 {MAX_TEXT_CHARS} 字符。')

        def retrieve(request):
            final_url, content_type, body = _download(url, request, PAGE_TYPES)
            title, links = '', []
            if content_type == 'text/plain':
                text = body.strip()
            else:
                page = _Page(final_url)
                page.feed(body)
                page.close()
                title = _compact(''.join(page.title))[:300]
                text = '\n'.join(line for raw in ''.join(page.text).splitlines()
                                 if (line := _compact(raw)))
                links = list(page.links)
            return {'url': final_url, 'title': title, 'text': text[:max_chars], 'links': links,
                    'truncated': len(text) > max_chars, 'fetched_at': _timestamp()}

        return _Request(self.cancel_event).run(retrieve)
