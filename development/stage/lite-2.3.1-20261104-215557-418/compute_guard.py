"""One local model workload per user, across Free AI installations/processes.

The OS file lock is authoritative; owner.json is only a readable explanation.
Never delete/replace compute.lock: that would let another process lock a new
inode. Stale metadata after a crash is harmless because the kernel drops locks.
Only a lock and tiny diagnostic JSON live in the shared coordination directory;
models, conversations and training datasets remain in the selected data folder.
"""
from __future__ import annotations

import errno
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid


def coordination_root():
    """Stable across independent installs and independent data directories."""
    if os.name == 'nt':
        # LOCALAPPDATA is per-user and does not vary with an app's TEMP override.
        base = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir())
        return base / 'FreeAI' / 'compute-coordination'
    identity = str(os.getuid()) if hasattr(os, 'getuid') else getpass.getuser()
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return Path('/tmp') / ('freeai-compute-' + suffix)


def _open(root):
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(root / 'compute.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    os.set_inheritable(descriptor, False)
    return os.fdopen(descriptor, 'r+b', buffering=0)


def _lock(stream):
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        # Windows permits locking a byte beyond EOF, avoiding an initial write
        # racing another process's mandatory byte-range lock.
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream):
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _contended(exc):
    return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(exc, 'winerror', None) == 33


def _owner(root):
    try:
        with (root / 'owner.json').open('r', encoding='utf-8') as stream:
            value = json.loads(stream.read(4096))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _reason(owner):
    if owner.get('kind') == 'training':
        return 'Free AI 正在训练，请等训练完成后再聊天或启动其他任务。'
    if owner.get('kind') == 'inference':
        return 'Free AI 客户端正在使用模型。请先在客户端“设置”中释放模型，或关闭客户端，再开始训练或启动另一个模型。'
    if owner.get('kind') == 'image':
        return 'Free AI 正在画图，请等画图完成，或先在原窗口停止任务。'
    if owner.get('kind') == 'speech':
        return 'Free AI 正在识别录音，请等识别完成，或先在原窗口停止任务。'
    return '另一个 Free AI 窗口正在使用电脑的计算资源，请等任务结束，或在原窗口释放模型后再试。'


class ComputeBusy(RuntimeError):
    def __init__(self, owner):
        self.owner = owner
        super().__init__(_reason(owner))


class WindowsJob:
    """Kill only this object's child processes when its owner disappears.

    A new child starts suspended, joins the private job, and only then resumes.
    It cannot initialize CUDA before crash cleanup ownership is established.
    No interpreter, shell, system-wide process search or process-name kill is
    needed; this also works inside a frozen desktop executable.
    """
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('WindowsJob 只适用于 Windows。')
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                        ('PerJobUserTimeLimit', ctypes.c_longlong),
                        ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t),
                        ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimit),
                        ('IoInfo', ctypes.c_ulonglong * 6),
                        ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def _resume(self, process):
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                        ('th32ThreadID', wintypes.DWORD), ('th32OwnerProcessID', wintypes.DWORD),
                        ('tpBasePri', wintypes.LONG), ('tpDeltaPri', wintypes.LONG),
                        ('dwFlags', wintypes.DWORD)]

        self.api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ('Thread32First', 'Thread32Next'):
            method = getattr(self.api, name)
            method.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
            method.restype = wintypes.BOOL
        self.api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenThread.restype = wintypes.HANDLE
        self.api.ResumeThread.argtypes = [wintypes.HANDLE]
        self.api.ResumeThread.restype = wintypes.DWORD
        snapshot = self.api.CreateToolhelp32Snapshot(0x4, 0)  # SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    # Popen closes its primary thread handle. Reopen only the
                    # suspended thread belonging to this newly created child.
                    thread = self.api.OpenThread(0x2, False, entry.th32ThreadID)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if self.api.ResumeThread(thread) == 0xffffffff:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        self.api.CloseHandle(thread)
                entry.dwSize = ctypes.sizeof(entry)
                found = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            raise RuntimeError('本地模型进程未能安全启动，请重试。')
        finally:
            self.api.CloseHandle(snapshot)

    def spawn(self, command, **options):
        import ctypes
        if not self.handle:
            raise RuntimeError('本地模型进程管理器已关闭。')
        flags = options.pop('creationflags', 0)
        # CREATE_SUSPENDED plus hidden window. Only this child is ever resumed.
        options['creationflags'] = flags | 0x4 | subprocess.CREATE_NO_WINDOW
        process = None
        try:
            process = subprocess.Popen(command, **options)
            if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._resume(process)
            return process
        except BaseException:
            self.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            raise

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class ComputeLease:
    """A nonblocking exclusive lease; acquire() returns self or raises.

    Pass root only for isolated tests. Production callers share the per-user
    default even when their model/data directories differ. A lease is not
    reentrant: do not nest contexts on the same instance.
    """
    def __init__(self, kind, root=None):
        if kind not in ('inference', 'training', 'image', 'speech'):
            raise ValueError('未知的本地计算任务类型。')
        self.kind = kind
        self.root = Path(root) if root is not None else coordination_root()
        self._stream = None
        self._mutex = threading.RLock()

    @property
    def held(self):
        return self._stream is not None

    def acquire(self):
        with self._mutex:
            if self._stream is not None:
                raise RuntimeError('这个计算任务已经持有使用权。')
            stream = _open(self.root)
            try:
                _lock(stream)
            except OSError as exc:
                stream.close()
                if _contended(exc):
                    raise ComputeBusy(_owner(self.root)) from None
                raise RuntimeError('无法检查本地计算任务，请检查协调目录的访问权限。') from exc
            self._stream = stream
            temporary = self.root / ('owner-' + uuid.uuid4().hex + '.tmp')
            try:
                metadata = {'kind': self.kind, 'pid': os.getpid(),
                            'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
                with temporary.open('w', encoding='utf-8') as output:
                    json.dump(metadata, output, ensure_ascii=False)
                os.replace(temporary, self.root / 'owner.json')
            except Exception:
                self.release()
                raise
            finally:
                temporary.unlink(missing_ok=True)
            return self

    def release(self):
        with self._mutex:
            if self._stream is not None:
                stream, self._stream = self._stream, None
                try:
                    _unlock(stream)
                finally:
                    stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False


def busy_reason(root=None):
    """Return a Chinese explanation, or ''. This is only a UI preflight.

    A subsequent worker must still acquire its own lease to avoid TOCTOU races.
    A crashed owner's stale metadata never makes an unlocked resource busy.
    """
    root = Path(root) if root is not None else coordination_root()
    try:
        stream = _open(root)
        try:
            _lock(stream)
        except OSError as exc:
            stream.close()
            if _contended(exc):
                return _reason(_owner(root))
            raise
        try:
            _unlock(stream)
        finally:
            stream.close()
        return ''
    except OSError:
        return '无法检查本地计算任务，请检查协调目录的访问权限后再试。'
