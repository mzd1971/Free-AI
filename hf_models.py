"""Search public Hugging Face GGUF repos and register selected model files."""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import time
import urllib.error
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from capability_manager import PackageError, _atomic_json, _relative


_HUB = 'https://huggingface.co'
_DIRECTORY = 'huggingface-models'
_API_LIMIT = 2 * 1024 * 1024
_DESCRIPTOR_LIMIT = 4096
_TOKEN = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}')
_REVISION = re.compile(r'[0-9a-f]{40}')
_SHA256 = re.compile(r'[0-9a-f]{64}')
_IDENTITY = re.compile(r'hf-[0-9a-f]{40}')
_TEXT_TASKS = {'text-generation', 'text2text-generation', 'image-text-to-text'}
_NO_LICENSE = '请查看仓库的许可说明'


class _HubRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        target = urlsplit(new_url)
        if target.scheme != 'https' or target.netloc != 'huggingface.co' or not target.path.startswith('/api/models/'):
            raise PackageError('Hugging Face 查询返回了不受支持的跳转。')
        return super().redirect_request(request, response, code, message, headers, new_url)


def _get_json(path, params):
    request = Request(f'{_HUB}{path}?{urlencode(params, doseq=True)}', headers={
        'Accept': 'application/json', 'Accept-Encoding': 'identity', 'User-Agent': 'FreeAI/model-browser',
    })
    deadline = time.monotonic() + 20
    try:
        with build_opener(_HubRedirect()).open(request, timeout=10) as response:
            raw = bytearray()
            while chunk := response.read1(min(65536, _API_LIMIT + 1 - len(raw))):
                raw.extend(chunk)
                if len(raw) > _API_LIMIT:
                    raise PackageError('Hugging Face 返回的模型资料过多，请缩小搜索范围或换一个仓库。')
                if time.monotonic() > deadline:
                    raise TimeoutError
        return json.loads(raw)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403, 404):
            raise PackageError('无法读取这个 Hugging Face 仓库。请确认它公开可访问，且无需登录或申请权限。') from None
        if error.code == 429:
            raise PackageError('Hugging Face 请求过于频繁，请稍后重试。') from None
        raise PackageError(f'Hugging Face 暂时无法返回模型资料（HTTP {error.code}）。') from None
    except (OSError, http.client.HTTPException):
        raise PackageError('连接 Hugging Face 失败。请检查网络连接后重试。') from None
    except (ValueError, RecursionError):
        raise PackageError('Hugging Face 返回了无法识别的模型资料。') from None


def _repo_is_valid(value):
    if not isinstance(value, str):
        return False
    parts = value.split('/')
    return 1 <= len(parts) <= 2 and all(
        _TOKEN.fullmatch(part) and '..' not in part and '--' not in part and not part.endswith(('.', '-'))
        for part in parts
    )


def _tag(value):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else ''


def _positive_int(value, maximum):
    return value if type(value) is int and 0 < value <= maximum else None


def search_models(query, limit=20):
    """Return a bounded list of GGUF repositories matching the search term."""
    if not isinstance(query, str) or len(query) > 200:
        raise PackageError('请输入不超过 200 字的模型名称。')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise PackageError('一次可搜索 1 至 50 个模型。')
    data = _get_json('/api/models', {
        'search': query.strip(), 'filter': 'gguf', 'limit': limit, 'sort': 'downloads', 'direction': -1,
        'expand': ['downloads', 'likes', 'private', 'gated', 'disabled', 'pipeline_tag'],
    })
    if not isinstance(data, list):
        raise PackageError('Hugging Face 返回了无法识别的搜索结果。')
    results, seen = [], set()
    for row in data[:limit]:
        if not isinstance(row, dict) or row.get('private') or row.get('disabled'):
            continue
        repo = row.get('id') or row.get('modelId')
        if not _repo_is_valid(repo) or repo.casefold() in seen:
            continue
        seen.add(repo.casefold())
        results.append({'repo_id': repo, 'url': f'{_HUB}/{quote(repo, safe="/")}',
                        'downloads': _positive_int(row.get('downloads'), 10**12) or 0,
                        'likes': _positive_int(row.get('likes'), 10**12) or 0,
                        'pipeline_tag': _tag(row.get('pipeline_tag')), 'gated': bool(row.get('gated'))})
    return results


def _filename_issue(filename):
    if (not isinstance(filename, str) or len(filename) > 512 or not filename.isprintable()
            or any(character in filename for character in '<>:"\\|?*%')
            or not filename.lower().endswith('.gguf')):
        return '文件路径或类型不受支持。'
    try:
        parts = _relative(filename).parts
    except PackageError:
        return '文件路径或类型不受支持。'
    if any(len(part) > 200 for part in parts) or len(parts[-1]) <= len('.gguf'):
        return '文件路径或类型不受支持。'
    if any('mmproj' in part.lower() for part in parts):
        return '这是多模态投影文件，不能单独用于文字聊天。'
    if re.search(r'(?i)[-_.]\d{1,5}[-_.]of[-_.]\d{1,5}\.gguf$', parts[-1]):
        return '这个文件属于分片模型，目前只支持单文件 GGUF。'
    return ''


def model_files(repo_id):
    """List GGUF files with size, pinned revision, checksum and availability."""
    if not _repo_is_valid(repo_id):
        raise PackageError('Hugging Face 仓库名称无效。')
    data = _get_json(f'/api/models/{quote(repo_id, safe="/")}', {'blobs': 'true'})
    if not isinstance(data, dict) or not isinstance(data.get('siblings'), list):
        raise PackageError('Hugging Face 返回了无法识别的仓库文件列表。')
    canonical = data.get('id') or data.get('modelId')
    if not _repo_is_valid(canonical):
        raise PackageError('Hugging Face 返回了无效的仓库名称。')
    revision = data.get('sha', '')
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise PackageError('无法取得这个 Hugging Face 仓库的固定版本。')
    tags = data.get('tags') if isinstance(data.get('tags'), list) else []
    license_id = next((_tag(tag[8:]) for tag in tags
                       if isinstance(tag, str) and tag.startswith('license:') and _tag(tag[8:])), '')
    gguf = data.get('gguf') if isinstance(data.get('gguf'), dict) else {}
    context_length = _positive_int(gguf.get('context_length'), 2**31 - 1)
    pipeline = _tag(data.get('pipeline_tag'))
    if data.get('gated') or data.get('private') or data.get('disabled'):
        repo_issue = '这个仓库无法匿名下载，需要登录、申请权限或仓库目前不可用。'
    elif pipeline and pipeline not in _TEXT_TASKS:
        repo_issue = f'这个仓库的任务类型是 {pipeline}，目前只支持文字聊天模型。'
    elif not (pipeline or 'text-generation' in tags or 'conversational' in tags or gguf.get('chat_template')):
        repo_issue = '仓库资料未标明支持文字生成。'
    else:
        repo_issue = ''

    results, seen = [], set()
    for file in data['siblings']:
        if not isinstance(file, dict):
            continue
        filename = file.get('rfilename')
        if not isinstance(filename, str) or not filename.lower().endswith('.gguf') or filename in seen:
            continue
        if len(filename) > 512 or not filename.isprintable():
            continue
        seen.add(filename)
        lfs = file.get('lfs') if isinstance(file.get('lfs'), dict) else {}
        digest = lfs.get('sha256', '')
        digest = digest.lower() if isinstance(digest, str) else ''
        size = _positive_int(file.get('size'), 2**63 - 1)
        lfs_size = _positive_int(lfs.get('size'), 2**63 - 1)
        reported_size = size or lfs_size
        reason = repo_issue or _filename_issue(filename)
        if not reason and (not reported_size or (size and lfs_size and size != lfs_size) or not _SHA256.fullmatch(digest)):
            reason = '仓库缺少有效的文件大小或 SHA-256，无法校验下载。'
        results.append({'repo_id': canonical, 'revision': revision, 'filename': filename,
                        'size': reported_size, 'sha256': digest if _SHA256.fullmatch(digest) else None,
                        'available': not reason, 'reason': reason, 'license': license_id or _NO_LICENSE,
                        'context_length': context_length})
    return sorted(results, key=lambda row: (not row['available'], row['filename'].casefold()))


def _descriptor(candidate):
    repo = candidate.get('repo_id')
    filename = candidate.get('filename')
    revision = candidate.get('revision')
    digest = candidate.get('sha256')
    size = _positive_int(candidate.get('size'), 2**63 - 1)
    if (not _repo_is_valid(repo) or _filename_issue(filename) or not size
            or not isinstance(revision, str) or not _REVISION.fullmatch(revision)
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
        raise PackageError('Hugging Face 模型的版本、路径或校验资料无效。')
    return {'repo_id': repo, 'filename': filename, 'revision': revision, 'sha256': digest, 'size': size,
            'license': _tag(candidate.get('license')) or _NO_LICENSE,
            'context_length': _positive_int(candidate.get('context_length'), 2**31 - 1)}


def _package(record):
    repo, revision, filename = record['repo_id'], record['revision'], record['filename']
    identity = 'hf-' + hashlib.sha256(f'{repo.casefold()}\n{revision}\n{filename}'.encode()).hexdigest()[:40]
    source = f'{_HUB}/{quote(repo, safe="/")}'
    remote_filename = quote(filename, safe='/')
    title = f'{repo} · {filename.rsplit("/", 1)[-1]}'
    spec = {'id': identity, 'title': title, 'short_name': filename.rsplit('/', 1)[-1][:-5],
            'description': f'Hugging Face · {repo}\n{filename}\n仅用于文字聊天，运行兼容性以实际加载为准。',
            'kind': 'model', 'license': record['license'], 'source': source,
            'quantized_source': f'{source}/blob/{revision}/{remote_filename}', 'version': revision,
            'entrypoint': 'model.gguf', 'dependencies': ['engine-cpu'], 'supports_vision': False,
            'use_cases': ['文字聊天'],
            'assets': [{'filename': 'model.gguf', 'url': f'{source}/resolve/{revision}/{remote_filename}',
                        'size': record['size'], 'sha256': record['sha256'], 'format': 'file'}]}
    if record['context_length']:
        spec['max_context'] = record['context_length']
    return spec


def _read_descriptor(path):
    with path.open('rb') as stream:
        raw = stream.read(_DESCRIPTOR_LIMIT + 1)
    if len(raw) > _DESCRIPTOR_LIMIT:
        raise PackageError('保存的 Hugging Face 模型资料过大。')
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise PackageError('保存的 Hugging Face 模型资料无效。')
    return _descriptor(record)


def load_specs(manager):
    """Restore validated descriptors from the configured model directory without network access."""
    try:
        directory = manager._safe(_DIRECTORY)
        paths = sorted(directory.glob('hf-*.json'))
    except (OSError, PackageError):
        return {}
    specs = {}
    for path in paths:
        if not _IDENTITY.fullmatch(path.stem):
            continue
        try:
            spec = _package(_read_descriptor(manager._safe(f'{_DIRECTORY}/{path.name}')))
        except (OSError, ValueError, RecursionError, PackageError):
            continue
        if spec['id'] == path.stem and spec['id'] not in manager.catalog:
            specs[spec['id']] = spec
    return specs


def add_model(manager, candidate):
    """Persist a selected file, leaving download and selection to their existing flows."""
    if not isinstance(candidate, dict) or candidate.get('available') is not True:
        raise PackageError('请选择可以下载的单文件 GGUF 模型。')
    record = _descriptor(candidate)
    spec = _package(record)
    identity = spec['id']
    try:
        with manager._operation():
            if 'engine-cpu' not in manager.catalog:
                raise PackageError('缺少本地文字运行组件。')
            path = manager._safe(f'{_DIRECTORY}/{identity}.json')
            if path.is_file():
                saved = _read_descriptor(path)
                if saved['size'] != record['size'] or saved['sha256'] != record['sha256']:
                    raise PackageError('同一固定版本的模型校验资料与先前记录不一致。')
                spec = _package(saved)
                if spec['id'] != identity:
                    raise PackageError('保存的 Hugging Face 模型路径与记录不一致。')
            else:
                _atomic_json(path, record)
            manager.catalog = {**manager.catalog, identity: spec}
    except (OSError, ValueError, RecursionError):
        raise PackageError('无法保存 Hugging Face 模型资料。请检查模型目录的权限与可用空间。') from None
    return identity
