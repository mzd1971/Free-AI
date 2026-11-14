"""Optional training environment, kept off the lightweight app's import path."""
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from paths import ARTIFACTS, ROOT

ENV = ARTIFACTS / 'training-tools'
REQUIREMENTS = ['transformers==4.57.6', 'peft==0.18.0', 'accelerate==1.12.0', 'numpy==2.2.6']


class TrainingCancelled(RuntimeError):
    """The user stopped preparation; downloaded caches remain available for retry."""


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise TrainingCancelled('已停止准备训练工具。已有下载和安装文件保留，稍后可以重试。')


class _WindowsJob:
    """A private Windows job owns exactly this command and its descendants."""
    def __init__(self):
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
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        import ctypes
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _spawn_owned(command, cwd, env):
    options = dict(cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   encoding='utf-8', errors='replace')
    if os.name != 'nt':
        return subprocess.Popen(command, start_new_session=True, **options), None
    job = _WindowsJob()
    process = None
    try:
        # The gate prevents pip/venv from spawning children before job assignment.
        # Passing argv as JSON avoids shell parsing and preserves paths with spaces.
        gate = ('import json,subprocess,sys; '
                'token=sys.stdin.buffer.read(1); '
                'sys.exit(subprocess.call(json.loads(sys.argv[1]), stdin=subprocess.DEVNULL) '
                'if token == b"G" else 1)')
        python = str(Path(sys.executable).with_name('python.exe'))
        process = subprocess.Popen([python, '-E', '-s', '-u', '-c', gate, json.dumps(command)],
                                   stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW, **options)
        job.assign(process)
        process.stdin.write('G')
        process.stdin.flush()
        process.stdin.close()
        return process, job
    except BaseException:
        job.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
        raise


def _stop_owned(process, job):
    if job is not None:
        job.close()
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_command(command, emit, cancel_event=None, *, cwd=ROOT, env=None):
    """Drain output off-thread, keeping cancellation responsive even without newlines."""
    _check_cancel(cancel_event)
    process, job = _spawn_owned(command, cwd, env)
    output = queue.Queue()

    def read_output():
        try:
            for line in process.stdout:
                output.put(('line', line.rstrip('\r\n')))
        except Exception as exc:
            output.put(('error', exc))
        finally:
            output.put(('eof', None))

    reader = threading.Thread(target=read_output, name='freeai-training-output', daemon=True)
    reader.start()
    eof = False
    closed = False
    try:
        while True:
            _check_cancel(cancel_event)
            try:
                kind, value = output.get(timeout=.1)
                if kind == 'line':
                    emit(value)
                elif kind == 'error':
                    raise value
                else:
                    eof = True
            except queue.Empty:
                pass
            if process.poll() is not None:
                if not closed:
                    _stop_owned(process, job)
                    closed = True
                if eof:
                    break
        _check_cancel(cancel_event)
        if process.returncode != 0:
            raise RuntimeError('训练工具下载或安装未完成。修复网络后重试，不会将它标记为可用。')
    finally:
        if not closed:
            _stop_owned(process, job)
        reader.join(timeout=2)
        if not reader.is_alive():
            process.stdout.close()

def prepare_starter_data():
    import shutil
    source = ROOT / 'starter_data' / 'teaching'
    target = ARTIFACTS / 'datasets' / 'teaching'
    target.mkdir(parents=True, exist_ok=True)
    for path in source.glob('*'):
        if path.is_file() and not (target / path.name).exists():
            shutil.copy2(path, target / path.name)

def training_python():
    candidate = ENV / 'Scripts/python.exe'
    if (ENV / 'ready.json').is_file() and candidate.is_file():
        return candidate
    # Existing 1.0 installations already contain a full, independent environment.
    base = Path(sys.executable).parent
    if (base / 'Lib/site-packages/torch/__init__.py').is_file() and (base / 'Lib/site-packages/peft/__init__.py').is_file():
        return base / 'python.exe'
    return None

def install_training(emit, cancel_event=None):
    _check_cancel(cancel_event)
    ready = training_python()
    if ready:
        emit('已有训练工具，无需重复下载。')
        return str(ready)
    import shutil
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(ARTIFACTS).free < 12_000_000_000:
        raise RuntimeError('训练工具需要预留至少 12 GB 空间，请先清理数据盘。')
    interpreter = Path(sys.executable).with_name('python.exe')
    cache = ARTIFACTS / 'download-cache'
    temporary = ARTIFACTS / 'training-temp'
    temporary.mkdir(exist_ok=True)
    env = dict(os.environ, PIP_CACHE_DIR=str(cache), TMP=str(temporary), TEMP=str(temporary), PYTHONUTF8='1', PYTHONUNBUFFERED='1')
    def run(command):
        return _run_command(command, emit, cancel_event, cwd=ROOT, env=env)
    emit('正在创建独立训练环境；文件保存在数据盘。')
    run([str(interpreter), '-E', '-s', '-m', 'venv', '--copies', str(ENV)])
    python = ENV / 'Scripts/python.exe'
    emit('正在下载 PyTorch 显卡版，约数 GB，请保持网络连接。')
    run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', 'torch==2.6.0', '--index-url', 'https://download.pytorch.org/whl/cu124'])
    run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--index-url', 'https://pypi.org/simple', *REQUIREMENTS])
    run([str(python), '-c', 'import torch,transformers,peft,accelerate,tkinter; print("训练工具检查通过")'])
    _check_cancel(cancel_event)
    marker = ENV / 'ready.json.tmp'
    try:
        marker.write_text(json.dumps({'torch': '2.6.0', 'requirements': REQUIREMENTS}), encoding='utf-8')
        _check_cancel(cancel_event)
        marker.replace(ENV / 'ready.json')
    finally:
        marker.unlink(missing_ok=True)
    emit('训练环境已准备好。入门训练模型可在能力中心单独下载。')
    return str(python)
