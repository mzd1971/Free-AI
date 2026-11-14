"""Resolve storage and models independently of the machine's original paths."""
import json
import os
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parent


def storage_root(root=ROOT, override=None):
    override = override if override is not None else os.environ.get('AI_TRAINING_DATA')
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError('AI_TRAINING_DATA 必须是完整文件夹路径。')
        return path
    settings = root / 'storage.json'
    config = json.loads(settings.read_text(encoding='utf-8-sig')) if settings.exists() else {}
    configured = config.get('data_root')
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = root / path
        # Optional training datasets are not required to select storage in v2.
        return path
    if os.name == 'nt' and root.drive.upper() == 'C:':
        raise ValueError('请将 FreeAI 安装到非 C 盘，聊天和训练数据默认与程序保存在一起。')
    return root / 'portable_data'


def _model_directory(value, *, root=None, label='模型位置'):
    """Validate a configured directory without creating or moving anything."""
    value = os.fspath(value)
    if (not isinstance(value, str) or not value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f'{label}不能为空，也不能含换行或控制字符。')
    value = value.strip()
    windows = PureWindowsPath(value)
    path = Path(value).expanduser() if root is None else Path(value)
    if value.startswith(('\\\\?\\', '\\\\.\\')) or any(char in value for char in '<>"|?*'):
        raise ValueError(f'{label}包含无效的目录字符。')
    # Both drive-relative D:models and rooted \\models can silently replace
    # part of the application directory when joined on Windows.
    if (windows.drive or windows.root) and not windows.is_absolute() and not path.is_absolute():
        raise ValueError(f'{label}必须是完整目录，或 Free AI 内部的相对目录。')
    if windows.drive and windows.is_absolute() and not path.is_absolute():
        raise ValueError(f'{label}不是当前系统支持的完整目录。')
    parts = value.replace('\\', '/').split('/')
    if '..' in parts:
        raise ValueError(f'{label}不能使用 .. 跳出目录，请填写明确的模型文件夹。')
    if any(':' in part for part in parts[1:]) or (':' in parts[0] and not windows.drive):
        raise ValueError(f'{label}包含无效的目录字符。')
    if os.name == 'nt':
        devices = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                   *(f'LPT{i}' for i in range(1, 10))}
        for part in windows.parts:
            if part == windows.anchor:
                continue
            if part.rstrip(' .') != part or part.split('.', 1)[0].upper() in devices:
                raise ValueError(f'{label}包含 Windows 不支持的文件夹名称。')
    relative = not path.is_absolute()
    if relative and root is None:
        raise ValueError(f'{label}必须是完整文件夹路径。')
    try:
        base = Path(root).resolve() if root is not None else None
        candidate = (base / path if relative else path).resolve()
        if relative and not candidate.is_relative_to(base):
            raise ValueError(f'{label}的相对路径必须位于 Free AI 文件夹内。')
        if candidate.exists() and not candidate.is_dir():
            raise ValueError(f'{label}指向了文件，请改为文件夹路径。')
        for parent in candidate.parents:
            if parent.exists() and not parent.is_dir():
                raise ValueError(f'{label}的上级路径不是文件夹。')
        return candidate
    except OSError:
        raise ValueError(f'无法访问{label}，请检查目录格式和访问权限。') from None


def models_root(root=ROOT, artifacts=None, override=None):
    """Resolve package storage; this function has no filesystem side effects.

    override is an isolated *data* root, matching AI_TRAINING_DATA: models go
    in its models/ child even if the application points to an external cache.
    With no override, model-location.txt wins over legacy cache discovery.
    The missing-file case remains compatible with pre-location installations.
    """
    selected_override = override if override is not None else os.environ.get('AI_TRAINING_DATA')
    if selected_override is not None and (override is not None or selected_override):
        scratch = _model_directory(selected_override, label='AI_TRAINING_DATA')
        return _model_directory(scratch / 'models', label='自检模型位置')
    root = Path(root).resolve()
    location = root / 'model-location.txt'
    try:
        configured = location.exists() or location.is_symlink()
        if configured:
            content = location.read_text(encoding='utf-8-sig')
            lines = content.splitlines()
            if len(lines) != 1 or not lines[0].strip():
                raise ValueError('model-location.txt 必须只有一行有效目录：填写 models，或完整模型文件夹路径。')
            return _model_directory(lines[0], root=root, label='model-location.txt 中的模型位置')
    except (OSError, UnicodeError):
        raise ValueError('无法读取 model-location.txt，请使用 UTF-8 编码并填写一行模型目录。') from None
    data = Path(artifacts) if artifacts is not None else storage_root(root, override='')
    legacy = data / 'capabilities'
    try:
        if legacy.exists():
            if not legacy.is_dir():
                raise ValueError('旧模型缓存 capabilities 不是文件夹，请检查保存位置。')
            if next(legacy.iterdir(), None) is not None:
                return _model_directory(legacy, label='旧模型缓存')
    except OSError:
        raise ValueError('无法检查旧模型缓存，请检查保存位置和访问权限。') from None
    return _model_directory('models', root=root)


ARTIFACTS = storage_root()
MODELS_ROOT = models_root(artifacts=ARTIFACTS)
DATA_ROOT = ARTIFACTS / 'datasets' / 'teaching'
RUNS_ROOT = ARTIFACTS / 'outputs'


def base_model_path(legacy_snapshot=None):
    bundled = ROOT / 'models' / 'Qwen3-0.6B'
    if (bundled / 'config.json').is_file() and (bundled / 'model.safetensors').is_file():
        return str(bundled)
    if legacy_snapshot and Path(legacy_snapshot).is_dir():
        return str(legacy_snapshot)
    from capability_manager import PackageManager
    manager = PackageManager()
    if manager.ready('legacy-training-model'):
        return str(manager.asset_path('legacy-training-model').parent)
    from huggingface_hub import snapshot_download
    return snapshot_download('Qwen/Qwen3-0.6B', local_files_only=True)


def relocated_path(value):
    normalized = str(value).replace('\\', '/')
    path = Path(normalized)
    # Prefer the selected workspace even if the original machine's copy exists.
    # Limit migration to known artifact roots; never redirect arbitrary files.
    for marker in ('/.codex/artifacts/ai-learning/', '/AI-Training/', '/portable_data/'):
        index = normalized.lower().find(marker.lower())
        if index >= 0:
            suffix = normalized[index + len(marker):]
            migrated = ARTIFACTS / suffix
            if '..' not in Path(suffix).parts and migrated.exists():
                return migrated
    return path
