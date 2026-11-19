"""Optional Harness configuration; no startup downloads or model requests."""
from __future__ import annotations

import os
import hashlib
import shutil
import stat
import threading
import urllib.request
import zipfile
from pathlib import Path
import tempfile

from paths import ARTIFACTS

SETTINGS_KEY = 'project_execution'
RUNTIME_VERSION = '0.2.2rc2'
WHEEL_URL = 'https://files.pythonhosted.org/packages/4e/87/cc1080f32f3835637d93d265ff94d2b24a1ec6b96780894a7e7eeb0a0b5a/deepseek_harness_runtime_bin-0.2.2rc2-py3-none-win_amd64.whl'
WHEEL_SHA256 = 'a791af95a97be205a73354c6819e95b5fe2ff40a57ef8a78d53c188ee33e9aea'
WHEEL_BYTES = 86959936
_INSTALL_LOCK = threading.Lock()


def default_executable():
    return ARTIFACTS / 'harness' / 'component' / RUNTIME_VERSION / 'deepseek_harness_runtime' / 'runtime' / 'deepseek-harness-sdk-runtime-win-x64.exe'


def load_preferences(sessions):
    values = sessions.get(SETTINGS_KEY, {})
    if not isinstance(values, dict):
        values = {}
    return {'engine': values.get('engine') if values.get('engine') in ('builtin', 'harness') else 'builtin',
            'executable': values.get('executable') if isinstance(values.get('executable'), str) else ''}


def save_preferences(sessions, values):
    if not isinstance(values, dict) or values.get('engine') not in ('builtin', 'harness'):
        raise ValueError('请选择项目执行方式。')
    executable = values.get('executable', '')
    if not isinstance(executable, str) or len(executable) > 4096 or any(ord(c) < 32 for c in executable):
        raise ValueError('请填写有效的组件程序路径。')
    executable = executable.strip()
    if executable and not Path(executable).is_absolute():
        raise ValueError('组件程序需要完整路径，不能填写命令或参数。')
    sessions.set(SETTINGS_KEY, {'engine': values['engine'], 'executable': executable})


def checked_executable(value):
    from project_agent import _workspace, _checked_stat
    value = value or default_executable()
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or (os.name == 'nt' and path.suffix.lower() != '.exe'):
        raise ValueError('执行组件尚未准备好。请到设置 → 编程工具选择组件并检查连接；草稿已保留。')
    _workspace(path.parent)
    _checked_stat(path)
    return str(path)


def child_environment(home, additions=None):
    from project_agent import _environment
    env = _environment()
    scratch = Path(home).parent / 'tmp'
    scratch.mkdir(parents=True, exist_ok=True)
    # Explicit whitelist from the built-in runner; unrelated provider credentials stay private.
    env.update(DSH_HOME=str(home), DSH_RESOURCE_CACHE=str(Path(home).parent / 'resources'),
               DSH_TELEMETRY_MODE='DISABLED', TEMP=str(scratch), TMP=str(scratch))
    if additions:
        env.update(additions)
    return env


def launch_directory(home):
    if os.name != 'nt':
        return Path(home)
    # Pinned pkg runtime resolves koffi under <cwd drive>:\snapshot on Windows.
    # Use the system directory to load its native addon; all owned state remains under DSH_HOME.
    from project_agent import _workspace
    system = Path(os.environ.get('SystemRoot', ''))
    if not system.is_absolute():
        raise ValueError('找不到 Windows 系统目录，无法启动执行组件。')
    return _workspace(system)


def initialize(client, cancel_event=None):
    result = client.request('initialize', {'protocolVersion': 1,
        'clientInfo': {'name': 'free-ai', 'version': '2.8.0'},
        'clientCapabilities': {}}, timeout=40, cancel_event=cancel_event)
    if result.get('protocolVersion') != 1:
        raise ValueError('执行组件协议版本不兼容，请更新或切回 Free AI 内置。')
    return result


def supports_resume(initialized):
    capabilities = initialized.get('agentCapabilities', {})
    session = capabilities.get('sessionCapabilities', {}) if isinstance(capabilities, dict) else {}
    return isinstance(session, dict) and isinstance(session.get('resume'), dict)


def probe(executable, cancel_event=None):
    from acp_transport import ACPProcess, ACPError, ACPCancelled
    executable = checked_executable(executable)
    base = ARTIFACTS / 'harness' / 'probes'
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='connection-', dir=base) as scratch:
        folder = Path(scratch)
        workspace = folder / 'workspace'
        workspace.mkdir()
        try:
            with ACPProcess([executable, '--profile', 'acp'], cwd=launch_directory(folder),
                            env=child_environment(folder / 'home')) as client:
                initialized = initialize(client, cancel_event)
                session = client.request('session/new', {'cwd': str(workspace), 'mcpServers': []},
                                         timeout=40, cancel_event=cancel_event)
                identity = session.get('sessionId')
                if not isinstance(identity, str) or not identity:
                    raise ValueError('执行组件没有返回有效会话。')
                client.request('session/close', {'sessionId': identity}, timeout=15,
                               cancel_event=cancel_event)
                resumable = supports_resume(initialized)
                if resumable:
                    client.request('session/resume', {'sessionId': identity, 'cwd': str(workspace), 'mcpServers': []},
                                   timeout=30, cancel_event=cancel_event)
                    client.request('session/close', {'sessionId': identity}, timeout=15, cancel_event=cancel_event)
                return {'version': str(initialized.get('agentInfo', {}).get('version', '未知'))[:80],
                        'resume': resumable, 'message': '已验证新建、关闭及支持的恢复流程；未调用模型。'}
        except ACPCancelled:
            raise
        except ACPError:
            raise ValueError('组件未通过新建或恢复会话检查。请查看官方说明或切回内置执行。') from None


def install_component(cancel_event=None):
    """Install a pinned, hash-checked native wheel without pip or global dependencies."""
    from acp_transport import ACPCancelled
    from project_agent import _workspace
    if os.name != 'nt':
        raise ValueError('自动安装目前支持 Windows x64；其他平台可手动选择组件。')
    import platform
    if platform.machine().lower() not in ('amd64', 'x86_64'):
        raise ValueError('此组件仅适用于 Windows x64。')
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise ValueError('组件正在安装，请等待当前操作结束。')
    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise ACPCancelled('已停止安装执行组件。')
    target = default_executable()
    component = ARTIFACTS / 'harness' / 'component'
    try:
        check_cancel()
        if target.is_file():
            checked = probe(target, cancel_event)
            check_cancel()
            if not isinstance(checked, dict) or checked.get('resume') is not True:
                raise ValueError('已有组件不支持恢复会话，请更换组件。')
            return target
        existing = component
        while not existing.exists():
            existing = existing.parent
        _workspace(existing)
        component.mkdir(parents=True, exist_ok=True)
        _workspace(component)
        destination = component / RUNTIME_VERSION
        if destination.exists():
            raise ValueError('已有未完成的组件目录，请先检查该目录；不会覆盖现有文件。')
        if shutil.disk_usage(component).free < 350 * 1024 * 1024:
            raise ValueError('安装需要至少 350 MB 可用空间（完成后约 153 MB）。')
        with tempfile.TemporaryDirectory(prefix='install-', dir=component) as scratch:
            scratch = Path(scratch)
            archive = scratch / 'runtime.whl'
            digest, size = hashlib.sha256(), 0
            request = urllib.request.Request(WHEEL_URL, headers={'User-Agent': 'FreeAI/2.8'})
            try:
                with urllib.request.urlopen(request, timeout=25) as response, archive.open('wb') as output:
                    while True:
                        check_cancel()
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > WHEEL_BYTES:
                            raise ValueError('组件下载大小不匹配。')
                        digest.update(chunk)
                        output.write(chunk)
            except OSError:
                raise ValueError('无法下载执行组件，请检查网络后重试。') from None
            if size != WHEEL_BYTES or digest.hexdigest() != WHEEL_SHA256:
                raise ValueError('组件校验失败，下载未安装。')
            payload = scratch / 'payload'
            payload.mkdir()
            extract_runtime(archive, payload, check_cancel)
            check_cancel()
            candidate = payload / target.relative_to(destination)
            checked = probe(candidate, cancel_event)
            check_cancel()
            if not isinstance(checked, dict) or checked.get('resume') is not True:
                raise ValueError('组件不支持恢复会话，下载未安装。')
            check_cancel()
            os.rename(payload, destination)
        return target
    finally:
        _INSTALL_LOCK.release()


def extract_runtime(archive, destination, check_cancel=lambda: None):
    """Extract a verified archive, retaining licenses and native sidecars."""
    from project_agent import _path_parts, _target
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if len(members) > 4000 or sum(m.file_size for m in members) > 200 * 1024 * 1024:
            raise ValueError('组件解包大小超出上限。')
        for member in members:
            check_cancel()
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError('组件包不能包含文件链接。')
            parts = _path_parts(member.filename.rstrip('/'))
            path = _target(destination, parts)
            if member.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with package.open(member) as source, path.open('xb') as output:
                    shutil.copyfileobj(source, output, length=1024 * 256)
