"""Public search and retrieval, exercised through their HTTP and DNS boundaries."""
from datetime import datetime
import gzip
from io import BytesIO
import socket
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import agent_web
from agent_web import WebError, WebTools
from remote_api import APICancelled


def dns_answer(address='8.8.8.8', port=80):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    target = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', target


def reply(body=b'', *, status=200, media='text/html; charset=utf-8', headers=None):
    fields = {'Content-Type': media, 'Content-Length': str(len(body)), 'Connection': 'close',
              **(headers or {})}
    wire = f'HTTP/1.1 {status} Test\r\n'
    wire += ''.join(f'{name}: {value}\r\n' for name, value in fields.items())
    return wire.encode('ascii') + b'\r\n' + body


def wire_socket(wire):
    connection = Mock()
    connection.makefile.return_value = BytesIO(wire)
    return connection


class WebTests(unittest.TestCase):
    def setUp(self):
        dns_patch = patch('agent_web.socket.getaddrinfo', return_value=[dns_answer()])
        socket_patch = patch('agent_web.socket.socket')
        tls_patch = patch('agent_web.ssl.SSLContext')
        self.dns = dns_patch.start()
        self.sockets = socket_patch.start()
        self.tls = tls_patch.start()
        self.tls.return_value.wrap_socket.side_effect = lambda connection, **_: connection
        self.addCleanup(tls_patch.stop)
        self.addCleanup(socket_patch.stop)
        self.addCleanup(dns_patch.stop)
        self.web = WebTools()

    def respond(self, *replies):
        connections = [wire_socket(wire) for wire in replies]
        self.sockets.side_effect = connections
        return connections

    def test_rejects_invalid_nonpublic_url_before_connecting(self):
        invalid = ('file:///tmp/test', 'https://user:secret@example.com/',
                   'http://example.com:8080/', 'http://example.com\\@127.0.0.1/',
                   'http://example.com\n/', 'http://localhost/', 'http://foo.internal/',
                   'http://127.0.0.1/', 'http://192.168.1.1/', 'http://169.254.169.254/',
                   'http://168.63.129.16/', 'http://100.100.100.200/', 'http://224.0.0.1/',
                   'http://[::ffff:8.8.8.8]/', 'http://[::1]/', 'http://[fe80::1%25e]/',
                   'http://[64:ff9b::a9fe:a9fe]/', 'http://[fec0::1]/')
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(WebError):
                self.web.fetch(url)
        self.dns.assert_not_called()
        self.sockets.assert_not_called()

    def test_rejects_mixed_public_private_dns_and_noncanonical_ip(self):
        self.dns.side_effect = ([dns_answer(), dns_answer('127.0.0.1')],
                                [dns_answer('127.0.0.1')])
        for url in ('http://www.example.com/', 'http://127.1/'):
            with self.subTest(url=url), self.assertRaises(WebError):
                self.web.fetch(url)
        self.sockets.assert_not_called()

    def test_tls_uses_pinned_ip_with_original_hostname(self):
        self.dns.return_value = [dns_answer('2606:4700:4700::1111', 443)]
        connection, = self.respond(reply(b'Public page'))
        result = self.web.fetch('https://EXAMPLE.com:443/A%20B?term=中文#fragment')
        self.assertEqual(result['text'], 'Public page')
        self.dns.assert_called_once_with('example.com.', 443, type=socket.SOCK_STREAM)
        self.sockets.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM)
        connection.connect.assert_called_once_with(('2606:4700:4700::1111', 443, 0, 0))
        self.tls.assert_called_once_with(agent_web.ssl.PROTOCOL_TLS_CLIENT)
        self.tls.return_value.load_default_certs.assert_called_once_with()
        self.tls.return_value.wrap_socket.assert_called_once_with(
            connection, server_hostname='example.com', do_handshake_on_connect=False)
        connection.do_handshake.assert_called_once_with()
        sent = connection.sendall.call_args.args[0]
        self.assertIn(b'GET /A%20B?term=%E4%B8%AD%E6%96%87 HTTP/1.1', sent)
        self.assertIn(b'Host: example.com:443', sent)
        self.assertNotIn(b'Cookie:', sent)
        self.assertNotIn(b'Authorization:', sent)

    def test_dual_stack_tries_alternate_family_before_more_same_family(self):
        self.dns.return_value = [dns_answer(), dns_answer('1.1.1.1'),
                                 dns_answer('2606:4700:4700::1111')]
        first, second = self.respond(b'', reply(b'Connected'))
        first.connect.side_effect = TimeoutError()
        output = self.web.fetch('http://www.example.com/')
        self.assertEqual(output['text'], 'Connected')
        first.connect.assert_called_once_with(('8.8.8.8', 80))
        second.connect.assert_called_once_with(('2606:4700:4700::1111', 80, 0, 0))

    def test_read_timeout_switches_to_another_verified_address(self):
        self.dns.return_value = [dns_answer(), dns_answer('2606:4700:4700::1111')]
        first, second = self.respond(b'', reply(b'Alternate response'))
        first.makefile.side_effect = TimeoutError()
        output = self.web.fetch('http://www.example.com/')
        self.assertEqual(output['text'], 'Alternate response')
        self.assertIn(b'GET / HTTP/1.1', first.sendall.call_args.args[0])
        self.assertIn(b'GET / HTTP/1.1', second.sendall.call_args.args[0])
        self.dns.assert_called_once()

    def test_redirect_resolves_every_hop_and_ignores_cookies(self):
        self.dns.side_effect = ([dns_answer('8.8.8.8')], [dns_answer('1.1.1.1')])
        _, second = self.respond(
            reply(status=302, headers={'Location': 'http://next.example/article', 'Set-Cookie': 'id=1'}),
            reply(b'<h1>Destination</h1><a href="../other">Other</a>'))
        output = self.web.fetch('http://www.example.com/original')
        second.connect.assert_called_once_with(('1.1.1.1', 80))
        self.assertEqual(output['url'], 'http://next.example/article')
        self.assertEqual(output['links'], ['http://next.example/other'])
        self.assertNotIn(b'Cookie:', second.sendall.call_args.args[0])

    def test_rejects_dns_rebinding_on_same_host_redirect(self):
        self.dns.side_effect = ([dns_answer()], [dns_answer('10.0.0.1')])
        self.respond(reply(status=302, headers={'Location': '/private'}))
        with self.assertRaises(WebError):
            self.web.fetch('http://www.example.com/path')
        self.assertEqual(self.dns.call_count, 2)
        self.assertEqual(self.sockets.call_count, 1)

    def test_blocks_invalid_redirect_location_and_redirect_loop(self):
        for location in ('http://169.254.169.254/latest/', 'ftp://example.com/test',
                         '/hello\\there', '', None):
            with self.subTest(location=location):
                self.sockets.reset_mock()
                fields = {} if location is None else {'Location': location}
                self.respond(reply(status=302, headers=fields))
                with self.assertRaises(WebError):
                    self.web.fetch('http://www.example.com/')
                self.assertEqual(self.sockets.call_count, 1)
        self.sockets.reset_mock()
        count = agent_web.MAX_REDIRECTS + 1
        self.respond(*(reply(status=302, headers={'Location': '/'}) for _ in range(count)))
        with self.assertRaisesRegex(WebError, '次数'):
            self.web.fetch('http://www.example.com/')
        self.assertEqual(self.sockets.call_count, count)

    def test_html_text_title_links_and_truncation(self):
        body = b'''<!doctype html><html><head><title> Hello &amp; world </title>
          <style>.ignored { color: red }</style></head><body>
          <script>ignored script</script><template><div>ignored template</div></template>
          <p>Keep <b>only this</b>.</p><!-- ignored comment --><p>A &lt; B.</p>
          <a href="/other#fragment">Read more</a> <a href="/other">here.</a>
          <a href="javascript:alert()">Unsafe link</a> <a href="http://127.0.0.1/">Private</a>
          </body></html>'''
        self.respond(reply(body), reply(body))
        full = self.web.fetch('http://www.example.com/')
        self.assertEqual(full['title'], 'Hello & world')
        self.assertEqual(full['text'], 'Keep only this.\nA < B.\nRead more here. Unsafe link Private')
        self.assertEqual(full['links'], ['http://www.example.com/other'])
        self.assertFalse(full['truncated'])
        self.assertIsNotNone(datetime.fromisoformat(full['fetched_at']).tzinfo)
        shortened = self.web.fetch('http://www.example.com/', max_chars=9)
        self.assertEqual(shortened['text'], full['text'][:9])
        self.assertTrue(shortened['truncated'])

    def test_limits_links_and_does_not_fetch_embedded_resources(self):
        links = ''.join(f'<a href="/{index}">Result</a>' for index in range(agent_web.MAX_LINKS + 5))
        self.respond(reply(f'<img src="/embedded">{links}'.encode('ascii')))
        output = self.web.fetch('http://www.example.com/')
        self.assertEqual(len(output['links']), agent_web.MAX_LINKS)
        self.sockets.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)

    def test_plain_text_and_html_meta_charset(self):
        self.respond(reply(b'  literal <tag>  ', media='text/plain'),
                     reply('<meta charset="gbk"><p>网页</p>'.encode('gbk'), media='text/html'))
        self.assertEqual(self.web.fetch('http://www.example.com/')['text'], 'literal <tag>')
        self.assertEqual(self.web.fetch('http://www.example.com/')['text'], '网页')

    def test_rejects_binary_encoding_and_oversized_response(self):
        cases = (reply(b'%PDF', media='application/pdf'), reply(b'GIF89a\x00', media='text/plain'),
                 reply(b'z', headers={'Content-Encoding': 'gzip'}),
                 reply(b'x', headers={'Content-Length': str(agent_web.MAX_RESPONSE_BYTES + 1)}),
                 reply(b'x', headers={'Content-Length': '4'}))
        self.respond(*cases)
        for index in range(len(cases)):
            with self.subTest(index=index), self.assertRaises(WebError):
                self.web.fetch('http://www.example.com/')
        chunked = (b'HTTP/1.1 200 Test\r\nContent-Type: text/plain\r\n'
                   b'Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n')
        self.respond(chunked)
        with patch('agent_web.MAX_RESPONSE_BYTES', 4), self.assertRaises(WebError):
            self.web.fetch('http://www.example.com/')

    def test_accepts_gzip_and_checks_decoded_size_and_integrity(self):
        markup = b'<p>Small wire response</p>'
        header = {'Content-Encoding': 'gzip'}
        connection, _, _ = self.respond(reply(gzip.compress(markup), headers=header),
                                        reply(gzip.compress(b'x' * 500), headers=header),
                                        reply(gzip.compress(markup)[:-5], headers=header))
        self.assertEqual(self.web.fetch('http://www.example.com/')['text'], 'Small wire response')
        self.assertIn(b'Accept-Encoding: gzip, identity', connection.sendall.call_args.args[0])
        with patch('agent_web.MAX_RESPONSE_BYTES', 100), self.assertRaisesRegex(WebError, '大小'):
            self.web.fetch('http://www.example.com/')
        with self.assertRaisesRegex(WebError, '(不完整|压缩)'):
            self.web.fetch('http://www.example.com/')

    def test_rss_search_parses_actual_response_and_enforces_limit(self):
        rss = b'''<?xml version="1.0"?><rss version="2.0"><channel>
          <item><title>First &amp; foremost</title><link>https://first.example/a</link>
            <description>Evidence &lt;b&gt;with detail&lt;/b&gt;.</description></item>
          <item><title>Duplicate</title><link>https://first.example/a</link></item>
          <item><title>Unsafe</title><link>http://10.0.0.1/</link></item>
          <item><title>Second</title><link>http://second.example/b</link></item>
          <item><title>Third</title><link>http://third.example/</link></item>
          </channel></rss>'''
        first, _ = self.respond(reply(status=302, headers={'Location': 'https://cn.bing.com/search'}),
                                reply(rss, media='text/xml'))
        result = self.web.search('中文 & links', limit=2)
        self.assertEqual(result['query'], '中文 & links')
        self.assertEqual(result['provider'], 'Bing RSS')
        self.assertEqual(result['results'], [
            {'title': 'First & foremost', 'url': 'https://first.example/a',
             'snippet': 'Evidence with detail.'},
            {'title': 'Second', 'url': 'http://second.example/b', 'snippet': ''}])
        _, target, _ = first.sendall.call_args.args[0].split(b'\r\n')[0].decode('ascii').split()
        self.assertEqual(parse_qs(urlsplit(target).query),
                         {'q': ['中文 & links'], 'format': ['rss'], 'count': ['2']})

    def test_search_failure_is_distinct_from_empty_search(self):
        self.respond(reply(b'<html>challenge</html>', media='text/html'),
                     reply(b'<rss>broken', media='text/xml'),
                     reply(b'<!DOCTYPE rss [<!ENTITY e "no">]><rss><channel/></rss>', media='text/xml'),
                     reply(status=429), reply(b'<rss><channel/></rss>', media='text/xml'))
        for index in range(4):
            with self.subTest(index=index), self.assertRaises(WebError):
                self.web.search('keywords')
        self.assertEqual(self.web.search('keywords')['results'], [])

    def test_search_refuses_redirect_to_unrelated_host(self):
        self.respond(reply(status=302, headers={'Location': 'https://tracker.example/search?q=test'}))
        with self.assertRaises(WebError):
            self.web.search('test')
        self.assertEqual(self.dns.call_count, 1)
        self.assertEqual(self.sockets.call_count, 1)

    def test_network_failure_reports_web_error(self):
        self.dns.side_effect = socket.gaierror('system-only detail')
        with self.assertRaises(WebError) as failure:
            self.web.fetch('https://www.example.com/')
        self.assertNotIn('system-only detail', str(failure.exception))

    def test_precancelled_request_does_not_resolve_dns(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(APICancelled):
            WebTools(cancelled).search('test')
        self.dns.assert_not_called()

    def test_cancel_interrupts_wait_for_dns_and_prevents_connection(self):
        cancelled = threading.Event()
        release = threading.Event()

        def delayed_dns(*_args, **_kwargs):
            cancelled.set()
            release.wait(1)
            return [dns_answer()]

        self.dns.side_effect = delayed_dns
        request = agent_web._Request(cancelled)
        with patch('agent_web._Request', return_value=request):
            try:
                with self.assertRaises(APICancelled):
                    WebTools(cancelled).fetch('https://www.example.com/')
            finally:
                release.set()
                self.assertTrue(request.done.wait(1))
        self.sockets.assert_not_called()

    def test_deadline_interrupts_pending_connection(self):
        release = threading.Event()
        connection, = self.respond(reply(b'Late response'))
        connection.connect.side_effect = lambda *_: release.wait(1)
        connection.shutdown.side_effect = lambda *_: release.set()
        with patch('agent_web.REQUEST_TIMEOUT', .15), self.assertRaisesRegex(WebError, '超时'):
            self.web.fetch('http://www.example.com/')
        connection.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        connection.sendall.assert_not_called()


if __name__ == '__main__':
    unittest.main()
