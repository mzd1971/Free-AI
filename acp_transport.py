"""Synchronous ACP JSON-RPC over a bounded subprocess stdio connection.

Notifications and peer requests are handled by the thread calling request().
Handlers that wait for user input must also observe the caller's cancel event.
"""

from __future__ import annotations

import ctypes
import io
import json
import math
import os
from queue import Empty, Full, Queue
import signal
import subprocess
from threading import Event, Lock, Thread
import time


MAX_LINE_BYTES = 4 * 1024 * 1024
QUEUE_SIZE = 8
CANCEL_GRACE = 5.0
WRITE_TIMEOUT = 5.0
_POLL = 0.05


class ACPError(RuntimeError):
    """An ACP connection or request failed."""


class ACPProtocolError(ACPError):
    """The child produced an invalid ACP message."""


class ACPTimeout(ACPError):
    """The child did not respond within the allotted time."""


class ACPCancelled(ACPError):
    """The active request was cancelled."""


class ACPRemoteError(ACPError):
    """A valid remote error, with remote message and data omitted."""

    def __init__(self, code):
        self.code = code
        super().__init__(f'ACP 代理返回 JSON-RPC 错误（代码 {code}）。')


class ACPMethodNotFound(NotImplementedError):
    """A request callback may raise this for an unsupported method."""


class _WindowsJob:
    """Attach the child before it can spawn descendants."""

    def __init__(self):
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                        ('max_working_set', ctypes.c_size_t), ('active_process_limit', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', ctypes.c_ulonglong * 6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError

    def attach(self, process):
        from ctypes import wintypes

        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError
        # Popen closes the primary thread handle on Windows.
        resume = ctypes.WinDLL('ntdll').NtResumeProcess
        resume.argtypes = (wintypes.HANDLE,)
        resume.restype = ctypes.c_long
        if resume(int(process._handle)):
            raise OSError

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError


def _encode(message):
    try:
        payload = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        raise ACPProtocolError('ACP 请求包含无法编码的 JSON 数据。') from None
    if len(payload) > MAX_LINE_BYTES:
        raise ACPProtocolError('ACP 消息超过 4 MiB 单行上限。')
    return payload + b'\n'


def _decode(line):
    try:
        message = json.loads(line.decode('utf-8'), object_pairs_hook=_unique_keys,
                             parse_constant=_reject_constant)
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            raise ValueError
        if 'id' in message and message['id'] is not None and type(message['id']) not in (int, str):
            raise ValueError
        if 'method' in message:
            if (not isinstance(message['method'], str) or not message['method']
                    or not isinstance(message.get('params', {}), dict)
                    or 'result' in message or 'error' in message):
                raise ValueError
        elif 'id' not in message or ('result' in message) == ('error' in message):
            raise ValueError
        return message
    except (ValueError, TypeError, RecursionError):
        raise ACPProtocolError('ACP 代理返回了无效的 UTF-8 JSON-RPC 消息。') from None


def _call(method, params):
    if not isinstance(method, str) or not method or (params is not None and not isinstance(params, dict)):
        raise ValueError('ACP 方法应为非空字符串，参数应为 JSON 对象。')
    return {'jsonrpc': '2.0', 'method': method, **({'params': params} if params is not None else {})}


class ACPProcess:
    """Own a child process and service one client request at a time."""

    def __init__(self, argv, *, cwd, env=None, on_notification=None, on_request=None):
        if (not isinstance(argv, (tuple, list)) or not argv or not all(
                isinstance(value, str) and '\x00' not in value for value in argv) or not argv[0].strip()):
            raise ValueError('ACP 启动命令必须是可执行文件及其独立参数列表。')
        self._on_notification = on_notification
        self._on_request = on_request
        self._incoming = Queue(maxsize=QUEUE_SIZE)
        self._outgoing = Queue(maxsize=1)
        self._closed = Event()
        self._close_lock = Lock()
        self._request_lock = Lock()
        self._next_id = 0
        self._process = None
        self._job = None
        self._threads = []
        windows = os.name == 'nt'
        try:
            if windows:
                self._job = _WindowsJob()
            self._process = subprocess.Popen(
                argv, cwd=cwd, env=env, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0, start_new_session=not windows,
                creationflags=(subprocess.CREATE_NO_WINDOW | 0x00000004) if windows else 0,
            )
            if self._job:
                self._job.attach(self._process)
            for name, target in (('read', self._read), ('stderr', self._drain_stderr), ('write', self._write)):
                thread = Thread(target=target, name=f'freeai-acp-{name}', daemon=True)
                thread.start()
                self._threads.append(thread)
        except (OSError, ValueError, TypeError, RuntimeError):
            self.close()
            raise ACPError('无法启动受进程树管理的 ACP 代理。请检查可执行文件与工作目录。') from None

    def _enqueue(self, value):
        while not self._closed.is_set():
            try:
                self._incoming.put(value, timeout=_POLL)
                return
            except Full:
                continue

    def _read(self):
        try:
            with io.BufferedReader(self._process.stdout) as stream:
                while not self._closed.is_set():
                    line = stream.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        self._enqueue(ACPError('ACP 代理提前关闭了输出流。'))
                        return
                    if len(line) > MAX_LINE_BYTES and not line.endswith(b'\n'):
                        self._enqueue(ACPProtocolError('ACP 代理输出超过 4 MiB 单行上限。'))
                        return
                    if not line.endswith(b'\n'):
                        self._enqueue(ACPProtocolError('ACP 代理输出了未以换行结束的消息。'))
                        return
                    self._enqueue(line)
        except (OSError, ValueError):
            self._enqueue(ACPError('无法读取 ACP 代理输出。'))

    def _drain_stderr(self):
        try:
            with self._process.stderr as stream:
                while not self._closed.is_set() and stream.read(8192):
                    pass
        except (OSError, ValueError):
            pass

    def _write(self):
        with self._process.stdin as stream:
            while not self._closed.is_set():
                try:
                    payload, done, errors = self._outgoing.get(timeout=_POLL)
                except Empty:
                    continue
                try:
                    remaining = memoryview(payload)
                    while remaining:
                        count = stream.write(remaining)
                        if not count:
                            raise OSError
                        remaining = remaining[count:]
                except (OSError, ValueError):
                    errors.append(ACPError('无法向 ACP 代理发送消息。'))
                finally:
                    done.set()

    def _send(self, payload, deadline, cancel_event=None):
        done, errors = Event(), []

        def remaining():
            if self._closed.is_set():
                raise ACPError('ACP 代理连接已关闭。')
            if cancel_event is not None and cancel_event.is_set():
                raise ACPCancelled('已取消 ACP 请求。')
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise ACPTimeout('向 ACP 代理发送消息超时。')
            return min(_POLL, seconds)

        while True:
            try:
                self._outgoing.put((payload, done, errors), timeout=remaining())
                break
            except Full:
                continue
        while not done.is_set():
            done.wait(remaining())
        if errors:
            raise errors[0]

    def _peer_reply(self, message, cancelling):
        method, params = message['method'], message.get('params', {})
        if 'id' not in message:
            if self._on_notification:
                try:
                    self._on_notification(method, params)
                except Exception:
                    raise ACPError('处理 ACP 通知时失败。') from None
            return None
        reply = {'jsonrpc': '2.0', 'id': message['id']}
        if cancelling and method == 'session/request_permission':
            reply['result'] = {'outcome': {'outcome': 'cancelled'}}
            return reply
        try:
            if cancelling:
                reply['error'] = {'code': -32800, 'message': 'Request cancelled'}
            elif not self._on_request:
                raise ACPMethodNotFound
            else:
                reply['result'] = self._on_request(method, params)
        except NotImplementedError:
            reply['error'] = {'code': -32601, 'message': 'Method not found'}
        except Exception:
            reply['error'] = {'code': -32603, 'message': 'Internal error'}
        return reply

    def _response(self, message, request_id, *, cancelling=False):
        if type(message['id']) is not int or message['id'] != request_id:
            raise ACPProtocolError('ACP 代理返回了不匹配的请求编号。')
        if 'error' in message:
            error = message['error']
            if not isinstance(error, dict) or type(error.get('code')) is not int or not isinstance(error.get('message'), str):
                raise ACPProtocolError('ACP 代理返回了无效的错误结构。')
            if cancelling or error['code'] == -32800:
                raise ACPCancelled('ACP 代理已取消请求。')
            raise ACPRemoteError(error['code'])
        if not isinstance(message['result'], dict):
            raise ACPProtocolError('ACP 代理的结果必须为 JSON 对象。')
        if cancelling:
            raise ACPCancelled('已取消 ACP 请求。')
        return message['result']

    def request(self, method, params, *, timeout=30, cancel_event=None, cancel_params=None):
        """Send a request and dispatch peer messages until its response arrives."""
        message = _call(method, params)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('ACP 请求超时必须是有限的正数。')
        if cancel_params is None and isinstance(params, dict) and isinstance(params.get('sessionId'), str):
            cancel_params = {'sessionId': params['sessionId']}
        if cancel_params is not None and (not isinstance(cancel_params, dict)
                or not isinstance(cancel_params.get('sessionId'), str) or not cancel_params['sessionId']):
            raise ValueError('ACP 取消参数必须含有效的 sessionId。')
        if cancel_event is not None and cancel_event.is_set():
            raise ACPCancelled('已取消 ACP 请求。')
        if not self._request_lock.acquire(blocking=False):
            raise ACPError('已有 ACP 请求正在等待响应。')
        try:
            self._next_id += 1
            message['id'] = self._next_id
            payload = _encode(message)
            cancel_message = (_call('session/cancel', cancel_params) if cancel_params is not None
                              else _call('$/cancel_request', {'requestId': self._next_id}))
            cancel_payload = _encode(cancel_message)
            return self._exchange(payload, message['id'], timeout, cancel_event, cancel_payload)
        finally:
            self._request_lock.release()

    def _exchange(self, payload, request_id, timeout, cancel_event, cancel_payload):
        deadline = time.monotonic() + timeout
        cancelling = False

        def check_cancel():
            nonlocal cancelling, deadline
            if not cancelling and cancel_event is not None and cancel_event.is_set():
                cancelling = True
                deadline = time.monotonic() + CANCEL_GRACE
                self._send(cancel_payload, deadline)

        try:
            self._send(payload, deadline, cancel_event)
            while True:
                check_cancel()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ACPTimeout('等待 ACP 代理响应超时。')
                if self._closed.is_set():
                    raise ACPError('ACP 代理连接已关闭。')
                try:
                    received = self._incoming.get(timeout=min(_POLL, remaining))
                except Empty:
                    continue
                check_cancel()
                if isinstance(received, ACPError):
                    raise received
                message = _decode(received)
                if 'method' not in message:
                    return self._response(message, request_id, cancelling=cancelling)
                reply = self._peer_reply(message, cancelling)
                check_cancel()
                if reply is not None:
                    if cancelling and message['method'] == 'session/request_permission':
                        reply = {'jsonrpc': '2.0', 'id': message['id'], 'result': {'outcome': {'outcome': 'cancelled'}}}
                    self._send(_encode(reply), deadline)
        except ACPRemoteError:
            raise
        except ACPCancelled:
            if not cancelling and cancel_event is not None and cancel_event.is_set():
                self.close()
            raise
        except ACPError:
            self.close()
            if cancelling:
                raise ACPCancelled('ACP 代理未能在限时内结束取消，已关闭进程。') from None
            raise

    def notify(self, method, params):
        payload = _encode(_call(method, params))
        try:
            self._send(payload, time.monotonic() + WRITE_TIMEOUT)
        except ACPError:
            self.close()
            raise

    def close(self):
        with self._close_lock:
            if self._closed.is_set():
                return
            self._closed.set()
        if self._job:
            self._job.close()
        process = self._process
        if process is None:
            return
        try:
            if os.name != 'nt':
                os.killpg(process.pid, signal.SIGKILL)
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        for thread in self._threads:
            thread.join(timeout=0.15)
        if not self._threads or not any(thread.is_alive() for thread in self._threads):
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()
