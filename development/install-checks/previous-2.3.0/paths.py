"""Resolve storage and models independently of the machine's original paths."""
import json
import os
from pathlib import Path

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
        for drive in ('D:', 'E:', 'F:'):
            if Path(drive + '/').exists():
                return Path(drive + '/FreeAI-Data')
        raise ValueError('请选择非 C 盘的数据文件夹，再启动 Free AI。')
    return root / 'portable_data'


ARTIFACTS = storage_root()
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
