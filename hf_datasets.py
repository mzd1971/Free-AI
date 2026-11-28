"""Browse public Hugging Face datasets and import text examples for Free AI."""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import tempfile
import time
import unicodedata
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from hf_dataset_catalog import dataset_match, hub_search_parameters, query_terms, recommendation_details
from paths import ARTIFACTS, ROOT
from training_desktop import FILES, inspect_data


_HUB = 'https://huggingface.co'
_VIEWER = 'https://datasets-server.huggingface.co'
_REPO_TOKEN = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}')
_API_LIMIT = 8 * 1024 * 1024
_TEXT_LIMIT = 32_000
_PAGE_SIZE = 100
MIN_IMPORT_ROWS = 20
MAX_IMPORT_ROWS = 1000
_IMPORT_ROOT = ARTIFACTS / 'datasets' / 'huggingface'
_GENERAL_FILE = ROOT / 'starter_data' / 'teaching' / FILES[-1]


class DatasetError(ValueError):
    """The dataset request or import could not be completed."""


class DatasetCancelled(DatasetError):
    """A caller cancelled a dataset request or import."""


class _DatasetRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        source, target = urlsplit(request.full_url), urlsplit(new_url)
        if (target.scheme != 'https' or source.netloc != target.netloc
                or not target.path.startswith('/api/datasets/')
                or target.netloc != 'huggingface.co'):
            raise DatasetError('Hugging Face 返回了不受支持的跳转。')
        return super().redirect_request(request, response, code, message, headers, new_url)


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise DatasetCancelled('已取消 Hugging Face 数据集操作。')


def _get_json(base, path, params, cancel):
    _check_cancel(cancel)
    request = Request(f'{base}{path}?{urlencode(params, doseq=True)}', headers={
        'Accept': 'application/json',
        'Accept-Encoding': 'identity',
        'User-Agent': 'FreeAI/dataset-browser',
    })
    deadline = time.monotonic() + 25
    try:
        with build_opener(_DatasetRedirect()).open(request, timeout=10) as response:
            raw = bytearray()
            while True:
                _check_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise TimeoutError
                chunk = response.read1(min(65536, _API_LIMIT + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > _API_LIMIT:
                    raise DatasetError('Hugging Face 返回的数据超出大小限制，请尝试较小的数据集。')
        _check_cancel(cancel)
        return json.loads(raw)
    except DatasetError:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        _check_cancel(cancel)
        if error.code in (401, 403, 404):
            raise DatasetError('无法读取 Hugging Face 数据集，请确认仓库公开可访问且支持数据集查看器。') from None
        if error.code == 429:
            raise DatasetError('Hugging Face 请求过于频繁，请稍后重试。') from None
        raise DatasetError(f'Hugging Face 暂时无法返回数据（HTTP {error.code}）。') from None
    except (OSError, http.client.HTTPException):
        _check_cancel(cancel)
        raise DatasetError('连接 Hugging Face 失败或超时，请检查网络后重试。') from None
    except (ValueError, RecursionError):
        raise DatasetError('Hugging Face 返回了无法识别的数据。') from None


def _valid_repo(dataset_id):
    if not isinstance(dataset_id, str):
        return False
    parts = dataset_id.split('/')
    return 1 <= len(parts) <= 2 and all(
        _REPO_TOKEN.fullmatch(part) and '..' not in part and '--' not in part
        and not part.endswith(('.', '-'))
        for part in parts
    )


def _repo_path(dataset_id):
    if not _valid_repo(dataset_id):
        raise DatasetError('请输入有效的 Hugging Face 数据集名称，例如 yahma/alpaca-cleaned。')
    return quote(dataset_id, safe='/')


def _valid_name(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and bool(value.strip()) and value.isprintable()


def _public(item):
    return not (item.get('private') or item.get('gated') or item.get('disabled'))


def _license(item):
    tags = item.get('tags')
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and tag.startswith('license:') and _valid_name(tag[8:]):
                return tag[8:]
    return ''


def _counter(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def search_datasets(query, *, limit=20, cancel=None):
    """Search the Hub for public datasets and rank by relevance to the query."""
    if not isinstance(query, str) or len(query) > 200:
        raise DatasetError('请输入不超过 200 字的数据集搜索词。')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise DatasetError('一次可搜索 1 至 50 个数据集。')
    _check_cancel(cancel)
    terms = query_terms(query)
    candidates = {}
    for params in hub_search_parameters(query, terms):
        _check_cancel(cancel)
        data = _get_json(_HUB, '/api/datasets', {
            **params, 'limit': limit, 'sort': 'downloads', 'direction': -1,
            'expand': ['downloads', 'likes', 'private', 'gated', 'disabled', 'tags'],
        }, cancel)
        _check_cancel(cancel)
        if not isinstance(data, list):
            raise DatasetError('Hugging Face 返回了无法识别的数据集搜索结果。')
        for item in data[:limit]:
            if not isinstance(item, dict) or not _public(item):
                continue
            repository = item.get('id')
            if not _valid_repo(repository):
                continue
            tags = item.get('tags')
            score = dataset_match(query, terms, repository, tags if isinstance(tags, list) else ())
            if score == 0:
                continue
            result = {
                'id': repository, 'license': _license(item),
                'downloads': _counter(item.get('downloads')) or 0, 'likes': _counter(item.get('likes')) or 0,
                **recommendation_details(repository),
            }
            canonical = repository.casefold()
            if canonical not in candidates or score > candidates[canonical][0]:
                candidates[canonical] = score, result
    ranked = sorted(
        candidates.values(),
        key=lambda candidate: (candidate[0], bool(candidate[1].get('title')), candidate[1]['downloads']),
        reverse=True,
    )
    return [result for _, result in ranked[:limit]]


def dataset_splits(dataset_id, *, cancel=None):
    """List available Viewer configuration and split pairs."""
    _repo_path(dataset_id)
    data = _get_json(_VIEWER, '/splits', {'dataset': dataset_id}, cancel)
    if not isinstance(data, dict) or not isinstance(data.get('splits'), list):
        raise DatasetError('Hugging Face 返回了无法识别的配置和划分。')
    seen = set()
    for item in data['splits']:
        if isinstance(item, dict) and _valid_name(item.get('config')) and _valid_name(item.get('split')):
            seen.add((item['config'], item['split']))
    if not seen and data.get('pending'):
        raise DatasetError('Hugging Face 正在准备数据集查看器，请稍后重试。')
    if not seen and data.get('failed'):
        raise DatasetError('Hugging Face 未能生成这个数据集的查看器数据。')
    return [{'config': config, 'split': split} for config, split in sorted(seen)]


def is_training_split(split):
    return isinstance(split, str) and split.casefold() == 'train'


def _rows_page(dataset_id, config, split, offset, length, cancel):
    _repo_path(dataset_id)
    if not _valid_name(config) or not _valid_name(split):
        raise DatasetError('请选择有效的 Hugging Face 配置和划分。')
    data = _get_json(_VIEWER, '/rows', {
        'dataset': dataset_id, 'config': config, 'split': split, 'offset': offset, 'length': length,
    }, cancel)
    if (not isinstance(data, dict) or not isinstance(data.get('rows'), list)
            or len(data['rows']) > length or not isinstance(data.get('features'), list)):
        raise DatasetError('Hugging Face 返回了无法识别的数据行。')
    return data


def _columns(data):
    return list(dict.fromkeys(
        feature['name'] for feature in data['features']
        if isinstance(feature, dict) and _valid_name(feature.get('name'))
    ))


def preview_rows(dataset_id, config, split, *, cancel=None):
    """Read the Viewer schema and up to five rows without storing them."""
    data = _rows_page(dataset_id, config, split, 0, 5, cancel)
    rows, truncation = [], []
    for item in data['rows']:
        if isinstance(item, dict) and isinstance(item.get('row'), dict):
            rows.append(item['row'])
            truncated = item.get('truncated_cells')
            truncation.append(
                [cell for cell in truncated if _valid_name(cell)] if isinstance(truncated, list) else []
            )
    return {
        'columns': _columns(data), 'rows': rows, 'truncated_cells': truncation,
        'total': _counter(data.get('num_rows_total')), 'partial': data.get('partial') is True,
    }


def _repo_info(dataset_id, cancel):
    data = _get_json(_HUB, f'/api/datasets/{_repo_path(dataset_id)}', {
        'expand': ['sha', 'private', 'gated', 'disabled', 'tags'],
    }, cancel)
    if not isinstance(data, dict) or not _valid_repo(data.get('id')):
        raise DatasetError('Hugging Face 返回了无法识别的仓库资料。')
    if not _public(data):
        raise DatasetError('这个 Hugging Face 数据集需要登录或目前不可访问。')
    revision = data.get('sha')
    return {
        'id': data['id'], 'license': _license(data),
        'hub_revision': revision if isinstance(revision, str) and re.fullmatch(r'[a-f0-9]{40}', revision) else None,
    }


def _mapping_columns(mapping):
    if not isinstance(mapping, dict):
        raise DatasetError('请选择训练数据的字段映射。')
    if mapping.keys() == {'messages'} and _valid_name(mapping['messages']):
        return {mapping['messages']}
    if {'prompt', 'response'} <= mapping.keys() <= {'prompt', 'input', 'response'}:
        columns = [mapping['prompt'], mapping['response']]
        if mapping.get('input') not in ('', None):
            columns.append(mapping['input'])
        if all(_valid_name(name) for name in columns) and len(set(columns)) == len(columns):
            return set(columns)
    raise DatasetError('请选择不同的提示词列和回答列（可选补充输入列），或选择一个对话列。')


def _read_text(value, *, allow_empty=False):
    if not isinstance(value, str) or len(value) > _TEXT_LIMIT or '\x00' in value:
        raise ValueError
    # JSON can encode unpaired surrogates, but our UTF-8 files cannot.
    value.encode('utf-8')
    value = value.strip()
    if not value and not allow_empty:
        raise ValueError
    return value


def _chat_pair(messages):
    if not isinstance(messages, list) or len(messages) < 2:
        raise ValueError
    turns = []
    expected_role = 'user'
    for index, message in enumerate(messages):
        if (not isinstance(message, dict)
                or message.get('tool_calls') or message.get('function_call')):
            raise ValueError
        role = message.get('role')
        if role == expected_role:
            expected_role = 'assistant' if role == 'user' else 'user'
        elif role != 'system' or index != 0:
            raise ValueError
        turns.append((role, _read_text(message.get('content'))))
    if turns[-1][0] != 'assistant':
        raise ValueError
    # The SFT bridge takes a single user prompt. Preserve earlier turns within it.
    history = turns[:-1]
    prompt = history[0][1] if len(history) == 1 else '\n\n'.join(
        f'{role}: {content}' for role, content in history
    )
    return _read_text(prompt), turns[-1][1]



def has_chat_samples(rows, column):
    """Check whether a preview column contains a supported dialogue."""
    for row in rows:
        try:
            _chat_pair(row.get(column))
        except ValueError:
            continue
        return True
    return False


def _pair(row, mapping):
    if 'messages' in mapping:
        return _chat_pair(row[mapping['messages']])
    prompt = _read_text(row[mapping['prompt']])
    response = _read_text(row[mapping['response']])
    if mapping.get('input'):
        extra = row[mapping['input']]
        extra = '' if extra is None else _read_text(extra, allow_empty=True)
        if extra:
            prompt = _read_text(f'{prompt}\n\n{extra}')
    return prompt, response


def _normalize(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def _take_rows(data, offset, mapping, accepted, skipped):
    for index, item in enumerate(data['rows'], start=offset):
        if not isinstance(item, dict) or type(item.get('row_idx')) is not int or item['row_idx'] != index:
            raise DatasetError('Hugging Face 数据行顺序异常，请重试。')
        row, truncated = item.get('row'), item.get('truncated_cells')
        if not isinstance(row, dict) or not isinstance(truncated, list) or not all(isinstance(cell, str) for cell in truncated):
            skipped['invalid'] += 1
            continue
        if truncated:
            skipped['truncated'] += 1
            continue
        try:
            prompt, response = _pair(row, mapping)
        except (KeyError, ValueError):
            skipped['invalid'] += 1
            continue
        key = _normalize(prompt)
        if key in accepted:
            category = 'duplicate' if _normalize(accepted[key]['response']) == _normalize(response) else 'conflicting'
            skipped[category] += 1
            continue
        accepted[key] = {'id': f'hf_{index:06d}', 'prompt': prompt, 'response': response}


def _partition(accepted):
    keys = sorted(accepted, key=lambda key: hashlib.sha256(key.encode('utf-8')).digest())
    ordered = [accepted[key] for key in keys]
    holdout_size = max(1, len(ordered) // 10)
    return ordered[2 * holdout_size:], ordered[:holdout_size], ordered[holdout_size:2 * holdout_size]


def _general_test(accepted):
    seen = set(accepted)
    rows = []
    overlap = 0
    try:
        original = _GENERAL_FILE.read_bytes()
        for line in original.decode('utf-8-sig').splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = _normalize(row['prompt'])
            if key in seen:
                overlap += 1
                continue
            seen.add(key)
            rows.append(row)
    except (OSError, ValueError, KeyError, TypeError):
        raise DatasetError('应用自带的检查示例缺失或无法读取。') from None
    if not rows:
        raise DatasetError('导入数据与应用自带的检查示例全部重合，请更换训练数据。')
    return rows, {
        'source': f'starter_data/teaching/{FILES[-1]}',
        'source_sha256': hashlib.sha256(original).hexdigest(),
        'overlaps_excluded': overlap,
        'scope': '应用自带的少量检查示例，仅用于观察这些题目上的表现。',
    }


def _jsonl(rows):
    # Keep JSONL compatible with the trainer's Unicode-aware splitlines reader.
    return ''.join(
        json.dumps(row, ensure_ascii=False).replace('\u0085', '\\u0085').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029') + '\n'
        for row in rows
    ).encode('utf-8')


def _save_import(output_root, subsets, report, cancel):
    _check_cancel(cancel)
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    pending = Path(tempfile.mkdtemp(prefix='.hf-import-', dir=root))
    destination = root / f'hf-{uuid4().hex}'
    report_name = 'hf_import.json'
    try:
        files = {}
        for name, rows in zip(FILES, subsets):
            _check_cancel(cancel)
            payload = _jsonl(rows)
            (pending / name).write_bytes(payload)
            files[name] = {'rows': len(rows), 'sha256': hashlib.sha256(payload).hexdigest()}
        report['files'] = files
        (pending / report_name).write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        inspect_data(pending)
        _check_cancel(cancel)
        if destination.exists():
            raise DatasetError('导入目标目录已存在，请重新导入。')
        pending.rename(destination)
    finally:
        if pending.exists():
            for name in (*FILES, report_name):
                (pending / name).unlink(missing_ok=True)
            pending.rmdir()
    return destination, destination / report_name


def import_dataset(dataset_id, config, split, count, mapping, *, cancel=None, progress=None, output_root=None):
    """Import up to count source rows from a public train split into an isolated SFT folder."""
    if not is_training_split(split):
        raise DatasetError('目前可导入名为 train 的训练划分。')
    if type(count) is not int or not MIN_IMPORT_ROWS <= count <= MAX_IMPORT_ROWS:
        raise DatasetError(f'请输入 {MIN_IMPORT_ROWS} 至 {MAX_IMPORT_ROWS} 行的导入数量。')
    columns = _mapping_columns(mapping)
    source = _repo_info(dataset_id, cancel)
    canonical = source['id']
    source.update({
        'config': config, 'split': split, 'url': f'{_HUB}/datasets/{_repo_path(canonical)}',
        'viewer_revision_pinned': False,
        'revision_note': '记录的 Hub 提交仅供溯源。Viewer 行接口未固定到此提交。',
    })
    fetched = 0
    seen_partial_page = False
    accepted = {}
    skipped = dict.fromkeys(('invalid', 'truncated', 'duplicate', 'conflicting'), 0)
    while fetched < count:
        length = min(_PAGE_SIZE, count - fetched)
        data = _rows_page(canonical, config, split, fetched, length, cancel)
        if not columns <= set(_columns(data)):
            raise DatasetError('映射字段与数据集列不匹配，请重新预览后选择字段。')
        seen_partial_page |= data.get('partial') is True
        _take_rows(data, fetched, mapping, accepted, skipped)
        page_size = len(data['rows'])
        fetched += page_size
        if progress is not None:
            progress(fetched, count)
        _check_cancel(cancel)
        total = _counter(data.get('num_rows_total'))
        if not page_size or (total is not None and fetched >= total) or (total is None and page_size < length):
            break

    if len(accepted) < 3:
        raise DatasetError(
            f'读取 {fetched} 行后只有 {len(accepted)} 条可用且不重复的文字样本。'
            '至少需要 3 条，请检查字段映射或扩大读取行数。'
        )

    general, general_report = _general_test(accepted)
    subsets = (*_partition(accepted), general)
    counts = dict(zip(('train', 'validation', 'test', 'general_test'), map(len, subsets)))
    report_data = {
        'source': source, 'mapping': mapping, 'fetched_at_utc': datetime.now(timezone.utc).isoformat(),
        'requested_source_rows': count, 'fetched_source_rows': fetched, 'viewer_partial': seen_partial_page,
        'skipped': skipped, 'counts': counts,
        'deduplication': '规范化提示词的 Unicode 形式和空白，保留出现最早的样本。不同答案的后续样本计入冲突。',
        'split_method': '按规范化提示词的 SHA-256 排序，约 80% 训练、10% 验证、10% 测试。',
        'imported_evaluation_scope': '验证和测试样本来自此次导入的同一个源训练划分。',
        'general_test': general_report,
    }
    path, report = _save_import(_IMPORT_ROOT if output_root is None else output_root, subsets, report_data, cancel)
    return {'path': path, 'counts': counts, 'skipped': skipped, 'report': report, 'fetched': fetched}
