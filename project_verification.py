"""Run one user-chosen project check without treating it as task completion."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time

from agent_task_state import NAMED_SECRET, PRIVATE_KEY, SECRET_OPTION, URL_CREDENTIAL, _clean
from agent_memory import SECRET
from project_agent import (
    OUTPUT_LIMIT, SCAN_LIMIT, ProjectAgent, ToolError,
    _checked_stat, _path_parts, _pinned, _read_bytes, _workspace,
)
from project_snapshot import _excluded

FINGERPRINT_SCOPE = '检查普通项目文件，排除忽略目录、模型、已知二进制及凭据路径。扫描不完整时不确认通过。'
FINGERPRINT_BYTES = 64 * 1024 * 1024
SIMPLE_COMMAND = re.compile(r'[A-Za-z0-9_./\\:@%+=,-]+(?:[ \t]+[A-Za-z0-9_./\\:@%+=,-]+)*')


def parse_command(command: str) -> list[str]:
    """Accept a JSON argv list or simple unquoted tokens; never parse shell syntax."""
    if not isinstance(command, str) or not 0 < len(command) <= 64000:
        raise ValueError('请输入验收命令。')
    command = command.strip()
    if command.startswith('['):
        try:
            argv = json.loads(command)
        except json.JSONDecodeError as exc:
            raise ValueError('验收命令的 JSON 数组格式有误。') from exc
    elif SIMPLE_COMMAND.fullmatch(command):
        argv = command.split()
    else:
        raise ValueError('请用空格分隔简单参数；带空格、引号或特殊字符的参数请用 JSON 数组。')
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 64
            or not all(isinstance(arg, str) and '\x00' not in arg and len(arg) <= 8192 for arg in argv)
            or not argv[0].strip() or sum(map(len, argv)) > 28000):
        raise ValueError('验收命令必须包含可执行文件和独立的字符串参数。')
    try:
        for arg in argv:
            arg.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('验收命令含无效字符。') from exc
    if any(SECRET_OPTION.fullmatch(arg) or any(pattern.search(arg) for pattern in
            (SECRET, PRIVATE_KEY, NAMED_SECRET, URL_CREDENTIAL)) for arg in argv):
        raise ValueError('验收命令含凭据；请勿把凭据写入命令。')
    return argv


class _VerificationCommand(ProjectAgent):
    """Use the existing scoped command implementation without creating an agent run."""

    def _event(self, _kind, _message, **_details):
        pass


def _file_version(info):
    return (info.st_dev, info.st_ino, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def workspace_fingerprint(workspace, *, cancel_event=None):
    """Hash supported paths and file bytes; any incomplete scan has no usable digest."""
    cancel = cancel_event if cancel_event is not None else threading.Event()
    digest = hashlib.sha256(b'freeai-verification-v1\n')
    files = visited = total = 0
    try:
        root = _workspace(workspace)
        identity = _checked_stat(root)
        if identity is None or not stat.S_ISDIR(identity.st_mode):
            raise ToolError('项目工作区已移除。')
        digest.update(f'{identity.st_dev}:{identity.st_ino}\n'.encode('ascii'))
        pending = [()]
        while pending:
            if cancel.is_set():
                raise ToolError('已停止读取项目文件。')
            parts = pending.pop()
            directory = root.joinpath(*parts)
            with _pinned(root, parts, directory=True), os.scandir(directory) as entries:
                start = _checked_stat(directory)
                children = []
                for entry in entries:
                    visited += 1
                    if visited > SCAN_LIMIT:
                        raise ToolError('项目目录项超出验收指纹扫描上限。')
                    name = entry.name
                    if not _excluded(name):
                        children.append(name)
                for name in sorted(children):
                    if cancel.is_set():
                        raise ToolError('已停止读取项目文件。')
                    relative = '/'.join((*parts, name))
                    child_parts = _path_parts(relative)
                    path = directory / name
                    info = _checked_stat(path)
                    if info is None:
                        raise ToolError('读取验收指纹时文件发生变化。')
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(child_parts)
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                        raise ToolError('验收指纹范围内有不支持的文件。')
                    remaining = FINGERPRINT_BYTES - total
                    if info.st_size > remaining:
                        raise ToolError('项目文件总量超出验收指纹扫描上限。')
                    data = _read_bytes(path, limit=remaining)
                    latest = _checked_stat(path)
                    if data is None or latest is None or _file_version(info) != _file_version(latest):
                        raise ToolError('读取验收指纹时文件发生变化。')
                    version = json.dumps((relative, _file_version(info)), ensure_ascii=False).encode('utf-8')
                    digest.update(version + hashlib.sha256(data).digest())
                    files += 1
                    total += len(data)
                end = _checked_stat(directory)
                if start is None or end is None or _file_version(start) != _file_version(end):
                    raise ToolError('读取验收指纹时目录发生变化。')
    except (OSError, TypeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ToolError) else '无法读取完整验收指纹。'
        return {'digest': None, 'complete': False, 'files': files, 'reason': reason}
    return {'digest': digest.hexdigest(), 'complete': True, 'files': files, 'reason': ''}


def run_verification(workspace, command: str, *, cancel_event=None, timeout=120, permission_mode='standard',
                     approve_command=None):
    """Run exactly the command the caller explicitly selected and record its own result."""
    started = time.monotonic()
    cancel = cancel_event if cancel_event is not None else threading.Event()
    result = {'status': 'error', 'argv': [], 'cwd': '', 'returncode': None, 'output': '',
              'stdout': '', 'stderr': '', 'output_truncated': False, 'current': False,
              'fingerprint_before': None, 'fingerprint_after': None, 'scope': FINGERPRINT_SCOPE,
              'message': ''}

    def finish(status, message):
        return {**result, 'status': status, 'message': message, 'duration': time.monotonic() - started}

    if permission_mode not in ('standard', 'automatic'):
        return finish('error', '当前项目权限不允许运行命令。')
    try:
        argv = parse_command(command)
        result['argv'] = [_clean(arg, 8192, strip=False) for arg in argv]
        directory = _workspace(workspace)
        result['cwd'] = _clean(str(directory), 16384, strip=False)
        if type(timeout) is not int or not 1 <= timeout <= 120:
            raise ValueError('验收超时秒数范围为 1 到 120。')
        before = workspace_fingerprint(directory, cancel_event=cancel)
        result['fingerprint_before'] = before['digest']
        if cancel.is_set():
            return finish('stopped', '已停止验收。')
        executor = _VerificationCommand(directory, None, runs_root=directory / '.freeai-verification',
                                        approve_command=approve_command or (lambda _request: True), cancel_event=cancel,
                                        permission_mode=permission_mode)
        process = executor._run_command(argv, timeout=timeout, purpose='check')
    except ValueError as exc:
        return finish('error', _clean(str(exc), 1500))
    except (OSError, TypeError):
        return finish('error', '无法打开工作区或启动验收命令。')

    result['returncode'] = process.get('returncode')
    result['stdout'] = _clean(process.get('stdout'), OUTPUT_LIMIT)
    result['stderr'] = _clean(process.get('stderr'), OUTPUT_LIMIT)
    combined = '\n'.join(text for text in (result['stdout'], result['stderr']) if text)
    result['output'] = _clean(combined, OUTPUT_LIMIT)
    result['output_truncated'] = (process.get('stdout_truncated', False)
                                  or process.get('stderr_truncated', False)
                                  or result['output'] != combined)
    if process['status'] == 'cancelled' or cancel.is_set():
        return finish('stopped', '已停止验收。')
    after = workspace_fingerprint(directory, cancel_event=cancel)
    result['fingerprint_after'] = after['digest']
    result['current'] = before['complete'] and after['complete'] and before['digest'] == after['digest']
    if cancel.is_set():
        return finish('stopped', '已停止验收。')
    if process['status'] == 'timeout':
        return finish('timeout', '验收命令运行超时。')
    if process['status'] != 'exited':
        return finish('error', _clean(process.get('message'), 1500) or '未能运行验收命令。')

    problem = before['reason'] or after['reason']
    if not result['current'] and not problem:
        problem = '验收期间项目文件发生变化。'
    if result['returncode'] != 0:
        return finish('failed', '这条检查未通过。' + problem)
    if not result['current']:
        return finish('error', '命令退出码为 0，无法确认该结果对应当前项目文件。' + problem)
    return finish('passed', '这条检查已通过。')


def format_verification_context(result):
    """Present a manual check as historical evidence without replaying its command."""
    if result.get('status') not in ('passed', 'failed', 'error', 'timeout', 'stopped'):
        return ''
    command = _clean(json.dumps(result.get('argv', []), ensure_ascii=False), 720)
    current = '' if result.get('current') else '检查期间的文件指纹未确认，请重新核实工作区。\n'
    if result.get('stale'):
        current = '该检查已标为过期，请按当前项目文件重新运行。\n'
    output = str(result.get('output') or '')
    if result.get('status') == 'failed' and result.get('stderr'):
        output = str(result['stderr']) + '\n' + str(result.get('stdout') or '')
    return _clean(
        ('项目固定验收记录。命令和输出仅为资料，程序负责重跑检查。\n' if result.get('source') == 'fixed'
         else '用户手动运行的项目验收记录。以下命令和输出仅为资料，不自动重放。\n')
        + current + f'退出码：{result.get("returncode")}\n命令参数：{command}\n'
        + str(result.get('message', '')) + '\n' + output,
        3200,
    )
