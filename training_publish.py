"""Publish an evaluated Qwen3 LoRA to the local client's model catalog."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid

from capability_manager import PackageError, _atomic_json, _hash
from training_gguf import AdapterError, read_adapter, write_adapter


BASE_MODEL = 'Qwen/Qwen3-0.6B'
BASE_MODEL_ID = 'text-qwen3-0-6b'
TRAINING_REVISION = 'c1899de289a04d12100db370d81485cdf75e47ca'
TRAINING_WEIGHT_SHA256 = 'f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b'
GGUF_BASE_REVISION = '50968a4468ef4233ed78cd7c3de230dd1d61a56b'
GGUF_BASE_SHA256 = 'ed2dd561f8aa5651cfb198be583269779e26ca6e28c40dba94fc2da4432bde2a'
SOURCE_INPUTS = (
    'run_config.json', 'comparison.json', 'adapter/adapter_config.json', 'adapter/adapter_model.safetensors',
)
_BASE = {'model': BASE_MODEL, 'revision': TRAINING_REVISION, 'weights_sha256': TRAINING_WEIGHT_SHA256,
         'package_id': BASE_MODEL_ID, 'gguf_revision': GGUF_BASE_REVISION, 'gguf_sha256': GGUF_BASE_SHA256}
_CONVERTER = 'freeai-qwen3-lora-f32-gguf-v1'
_DIRECTORY = 'trained'
_ENTRYPOINT = 'adapter.gguf'
_MANIFEST = 'publication.json'
_IDENTITY = re.compile(r'trained-[a-f0-9]{40}')
_SHA256 = re.compile(r'[a-f0-9]{64}')
_JSON_LIMIT = 16 * 1024 * 1024
_WEIGHTS_LIMIT = 8 * 1024 * 1024


class PublishError(PackageError):
    """The training artifact could not be published."""


def _cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise PublishError('已停止发布。')


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _sha256(value):
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _fingerprint(source):
    return _digest(json.dumps(source, sort_keys=True).encode('utf-8'))


def _identity(source_fingerprint):
    return 'trained-' + _fingerprint([source_fingerprint, _BASE, _CONVERTER])[:40]


def _title(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 100
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) < 0xE000 for char in value)):
        raise PublishError('发布名称应为 1–100 个字符，并且不能含换行。')
    return value.strip()


def _compatible_base(manager):
    base = manager.catalog.get(BASE_MODEL_ID, {})
    return (base.get('version') == GGUF_BASE_REVISION
            and any(asset.get('sha256') == GGUF_BASE_SHA256 and asset.get('filename') == base.get('entrypoint')
                    for asset in base.get('assets', [])))


def _bounded_read(path, limit):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise PublishError(f'训练产物文件内容过大：{path.name}')
    return raw


def _read_source(run_dir, relative, limit):
    path = run_dir / relative
    if not path.resolve().is_relative_to(run_dir) or not path.is_file():
        raise PublishError(f'训练产物缺少有效文件：{relative}')
    return _bounded_read(path, limit)


def _parse_json(raw, expected, label):
    def unique_keys(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('duplicate JSON key')
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        raise PublishError(f'{label} 不是有效的 JSON。') from None
    if not isinstance(value, expected):
        raise PublishError(f'{label} 的结构不正确。')
    return value


def _run_snapshot(manager, run_dir, cancel_event=None):
    from training_evaluation import evaluate_comparison

    if not _compatible_base(manager):
        raise PublishError('模型目录缺少受支持的 Qwen3 0.6B GGUF 基座版本。')
    _cancel(cancel_event)
    try:
        run_dir = Path(run_dir).expanduser().resolve(strict=True)
        files = {}
        for name in ('summary.json', *SOURCE_INPUTS):
            _cancel(cancel_event)
            limit = _WEIGHTS_LIMIT if name.endswith('.safetensors') else _JSON_LIMIT
            files[name] = _read_source(run_dir, name, limit)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if isinstance(exc, PublishError):
            raise
        raise PublishError('无法读取完整的训练产物，请检查运行目录。') from exc

    summary = _parse_json(files['summary.json'], dict, 'summary.json')
    if summary.get('completed') is not True or type(summary.get('best_epoch')) is not int or summary['best_epoch'] < 1:
        raise PublishError('请先完成训练和留出评估。')
    source = {name: _digest(raw) for name, raw in files.items()}
    if summary.get('publication_inputs') != {name: source[name] for name in SOURCE_INPUTS}:
        raise PublishError('训练产物缺少评估时的文件校验记录，或相关文件已改变。请重新训练并评估。')

    config = _parse_json(files['run_config.json'], dict, 'run_config.json')
    if (config.get('base_model') != BASE_MODEL or config.get('revision') != TRAINING_REVISION
            or config.get('base_model_sha256') != TRAINING_WEIGHT_SHA256):
        raise PublishError('训练基座版本与可发布的 Qwen3 0.6B 基座不匹配。')

    comparison = _parse_json(files['comparison.json'], list, 'comparison.json')
    if type(summary.get('test_count')) is not int or summary['test_count'] != len(comparison):
        raise PublishError('留出评估结果数与训练摘要不一致。')
    acceptance = evaluate_comparison(comparison, baseline_val_loss=summary.get('baseline_validation_loss'),
                                     best_val_loss=summary.get('best_validation_loss'))

    adapter = _parse_json(files['adapter/adapter_config.json'], dict, 'adapter_config.json')
    adapter_base, snapshot = adapter.get('base_model_name_or_path'), config.get('base_snapshot')
    local_base = (isinstance(adapter_base, str) and isinstance(snapshot, str) and bool(snapshot)
                  and adapter_base.replace('\\', '/').casefold() == snapshot.replace('\\', '/').casefold())
    if ((adapter_base != BASE_MODEL and not local_base)
            or adapter.get('revision') not in (None, TRAINING_REVISION)):
        raise PublishError('LoRA 声明的基座与训练记录不一致。')
    _cancel(cancel_event)
    try:
        alpha, tensors = read_adapter(adapter, files['adapter/adapter_model.safetensors'])
    except AdapterError as exc:
        raise PublishError(str(exc)) from exc

    fingerprint = _fingerprint(source)
    return {'run_dir': str(run_dir), 'run_name': run_dir.name, 'identity': _identity(fingerprint),
            'acceptance': acceptance, 'source_fingerprint': fingerprint, 'source_sha256': source,
            'base': dict(_BASE)}, alpha, tensors


def inspect_run(manager, run_dir, *, cancel_event=None):
    """Recompute the acceptance result and check the exact bound LoRA inputs."""
    result, _, _ = _run_snapshot(manager, run_dir, cancel_event)
    return result


def _directory(manager, identity):
    if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
        raise PublishError('本地训练模型标识无效。')
    return manager._safe(f'{_DIRECTORY}/{identity}')


def _spec(record):
    identity, title = record['id'], record['title']
    return {
        'id': identity, 'title': title, 'short_name': title, 'description': '通过留出评估的本地 LoRA。使用 Qwen3 0.6B GGUF 基座运行。',
        'kind': 'model', 'local_training': True, 'base_model_id': BASE_MODEL_ID,
        'dependencies': [BASE_MODEL_ID], 'target': f'{_DIRECTORY}/{identity}', 'assets': [],
        'entrypoint': _ENTRYPOINT, 'family': 'qwen3', 'version': record['source_fingerprint'],
        'license': 'Apache-2.0（基座）', 'source': f'https://huggingface.co/{BASE_MODEL}',
        'supports_vision': False, 'max_context': 32768, 'recommended_context': 4096,
        'recommended_ram_gb': 8, 'recommended_vram_gb': 2, 'reasoning_style': 'disabled',
        'system_prompt_mode': 'system', 'chat_template_kwargs': {'enable_thinking': False},
        'use_cases': ['文字聊天'], 'validation_status': 'training_acceptance_passed',
    }


def _manifest(manager, identity):
    directory = _directory(manager, identity)
    path = manager._safe(f'{_DIRECTORY}/{directory.name}/{_MANIFEST}')
    record = _parse_json(_bounded_read(path, 64 * 1024), dict, _MANIFEST)
    source, artifact = record.get('source_sha256'), record.get('artifact')
    fingerprint = record.get('source_fingerprint')
    if (
        record.get('schema_version') != 1 or record.get('id') != identity
        or record.get('converter') != _CONVERTER or record.get('base') != _BASE
        or record.get('acceptance_status') != 'passed'
        or not isinstance(source, dict) or set(source) != {'summary.json', *SOURCE_INPUTS}
        or not all(_sha256(value) for value in source.values())
        or fingerprint != _fingerprint(source) or identity != _identity(fingerprint)
        or not isinstance(artifact, dict) or artifact.get('filename') != _ENTRYPOINT
        or type(artifact.get('size')) is not int or not 0 < artifact['size'] <= _WEIGHTS_LIMIT
        or not _sha256(artifact.get('sha256'))
        or _title(record.get('title')) != record['title']
    ):
        raise PublishError('本地训练模型的发布记录损坏。')
    return record


def load_specs(manager):
    """Load catalog entries from successful atomic publications."""
    if not _compatible_base(manager):
        return {}
    try:
        candidates = sorted(manager._safe(_DIRECTORY).glob('trained-*'))
    except (OSError, PackageError, ValueError):
        return {}
    specs = {}
    for path in candidates:
        existing = manager.catalog.get(path.name)
        if existing is None or existing.get('local_training') is True:
            try:
                specs[path.name] = _spec(_manifest(manager, path.name))
            except (OSError, PackageError, ValueError, RecursionError):
                continue
    return specs


def asset_path(manager, identity):
    _directory(manager, identity)
    return manager._safe(f'{_DIRECTORY}/{identity}/{_ENTRYPOINT}')


def ready(manager, identity, *, full=False):
    """Check the local payload; PackageManager also checks the GGUF base dependency."""
    try:
        if not _compatible_base(manager):
            return False
        record = _manifest(manager, identity)
        path = asset_path(manager, identity)
        info = path.stat()
        artifact = record['artifact']
        if info.st_size != artifact['size'] or not path.is_file():
            return False
        signature = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, artifact['sha256'])
        if full or manager._verified.get(str(path)) != signature:
            with path.open('rb') as stream:
                if stream.read(8) != b'GGUF\x03\x00\x00\x00':
                    return False
            if _hash(path) != artifact['sha256']:
                return False
            manager._verified[str(path)] = signature
        return True
    except (OSError, PackageError, ValueError, RecursionError):
        return False


def installed_size(manager, identity):
    try:
        return asset_path(manager, identity).stat().st_size
    except (OSError, PackageError, ValueError):
        return 0


def publish_run(manager, run_dir, *, name=None, progress=None, cancel_event=None):
    """Atomically install the bound GGUF adapter under the configured model directory."""
    def emit(phase, message):
        if progress is not None:
            progress({'phase': phase, 'message': message})

    emit('verifying', '正在复核留出评估和 LoRA 文件。')
    result, alpha, tensors = _run_snapshot(manager, run_dir, cancel_event)
    if result['acceptance']['status'] != 'passed':
        raise PublishError('留出评估尚未通过，当前训练成果无法发布。')
    identity = result['identity']
    title = _title(name if name is not None else f'本地训练 · {result["run_name"][:80]}')
    try:
        with manager._operation():
            _cancel(cancel_event)
            existing = manager.catalog.get(identity)
            if existing is not None and existing.get('local_training') is not True:
                raise PublishError('该模型标识已被模型目录占用。')
            if ready(manager, identity, full=True):
                manager.catalog = {**manager.catalog, identity: _spec(_manifest(manager, identity))}
                return identity
            if identity in manager._active:
                raise PublishError('本地训练模型正在使用，请停止推理后重试。')
            target = _directory(manager, identity)
            staging = manager._safe(f'staging/{identity}-{uuid.uuid4().hex}')
            staging.mkdir(parents=True, exist_ok=False)
            backup = staging / 'old'
            bundle = staging / 'publication'
            try:
                bundle.mkdir()
                emit('converting', '正在将 LoRA 转为客户端使用的 GGUF 适配器。')
                _cancel(cancel_event)
                output = bundle / _ENTRYPOINT
                write_adapter(output, alpha, tensors)
                record = {
                    'schema_version': 1, 'id': identity, 'title': title, 'run_name': result['run_name'],
                    'published_at': datetime.now(timezone.utc).isoformat(),
                    'base': result['base'], 'source_fingerprint': result['source_fingerprint'],
                    'source_sha256': result['source_sha256'], 'acceptance_status': 'passed',
                    'acceptance_schema_version': result['acceptance']['schema_version'],
                    'converter': _CONVERTER,
                    'artifact': {'filename': _ENTRYPOINT, 'size': output.stat().st_size, 'sha256': _hash(output)},
                }
                _atomic_json(bundle / _MANIFEST, record)
                _cancel(cancel_event)
                spec = _spec(record)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target.rename(backup)
                try:
                    bundle.rename(target)
                except OSError:
                    if backup.exists():
                        backup.rename(target)
                    raise
                manager.catalog = {**manager.catalog, identity: spec}
            finally:
                # The staging directory was allocated through PackageManager's path checks.
                # Keep a backup if an interrupted commit could not put it back.
                if not backup.exists() or target.exists():
                    shutil.rmtree(staging, ignore_errors=True)
    except OSError as exc:
        raise PublishError('无法写入本地训练模型，请检查模型盘空间和写入权限后重试。') from exc
    return identity


def remove(manager, identity):
    """Remove the published copy while PackageManager holds its operation lock."""
    target = _directory(manager, identity)
    if manager.catalog.get(identity, {}).get('local_training') is not True:
        raise PublishError('没有找到这个本地训练模型。')
    if identity in manager._active:
        raise PublishError('本地训练模型正在使用，请停止推理后重试。')
    path = str(asset_path(manager, identity))
    try:
        if target.is_dir():
            discarded = manager._safe(f'staging/{identity}-remove-{uuid.uuid4().hex}')
            discarded.parent.mkdir(parents=True, exist_ok=True)
            target.rename(discarded)
            shutil.rmtree(discarded, ignore_errors=True)
    except OSError as exc:
        raise PublishError('无法移除本地训练模型，请停止推理后重试。') from exc
    manager._verified.pop(path, None)
    manager.catalog = {key: spec for key, spec in manager.catalog.items() if key != identity}
