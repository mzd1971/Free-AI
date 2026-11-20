"""User-enabled acceptance command for a single local workspace."""
import hashlib
import os
from pathlib import Path

from project_verification import parse_command


MAX_REPAIR_ATTEMPTS = 2


def _setting(workspace):
    folder = Path(workspace).resolve(strict=True)
    if not folder.is_dir():
        raise ValueError('请先选择项目目录。')
    identity = os.path.normcase(str(folder)).encode('utf-8')
    return 'fixed_project_check.' + hashlib.sha256(identity).hexdigest()


def load_project_check(sessions, workspace):
    command = sessions.get(_setting(workspace), '')
    if command:
        parse_command(command)
    return command


def save_project_check(sessions, workspace, command):
    if not isinstance(command, str):
        raise ValueError('请输入验收命令，或留空停用。')
    command = command.strip()
    if command:
        parse_command(command)
    sessions.set(_setting(workspace), command)
