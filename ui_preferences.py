"""Preferences shared by the desktop client and the training workbench."""
from pathlib import Path
from uuid import uuid4

TEXT_SIZES = {'标准': 'normal', '大': 'large', '特大': 'extra_large'}
TEXT_SCALE = {'normal': 1.0, 'large': 1.17, 'extra_large': 1.34}
DEFAULT_TEXT_SIZE = 'normal'


def _preference_path(directory):
    if directory is None:
        from paths import ARTIFACTS
        directory = ARTIFACTS
    return Path(directory) / 'text-size.txt'


def load_text_size(directory=None):
    path = _preference_path(directory)
    try:
        choice = path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return DEFAULT_TEXT_SIZE
    return choice if choice in TEXT_SCALE else DEFAULT_TEXT_SIZE


def save_text_size(choice, directory=None):
    if choice not in TEXT_SCALE:
        raise ValueError('未知字号')
    path = _preference_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f'.{path.name}-{uuid4().hex}.tmp')
    try:
        pending.write_text(choice + '\n', encoding='utf-8')
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
