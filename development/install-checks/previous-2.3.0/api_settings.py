"""Public API preferences and current-user encrypted credentials.

No environment credentials, network, model loading, or third-party packages.
The single atomic file contains preferences and a DPAPI blob, never plaintext
credentials. Its encrypted payload binds the credential to the whole base URL.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
import stat
import tempfile
import threading


DEFAULTS = {
    'provider': 'deepseek',
    'base_url': 'https://api.deepseek.com',
    'model': 'deepseek-flash',
    'remember_key': True,
    'include_memory': False,
    'max_output_tokens': 1024,
    'thinking': False,
}
MAX_FILE_BYTES = 128 * 1024
MAX_KEY_BYTES = 8 * 1024


class APISettingsError(RuntimeError):
    """A safe user-facing error that never includes credential material."""


class WindowsDPAPICipher:
    """Windows DPAPI, scoped to the signed-in user, with UI disabled."""
    @staticmethod
    def _transform(data, decrypt=False):
        if os.name != 'nt':
            raise APISettingsError('当前系统不支持安全保存密钥，请关闭“记住密钥”后使用。')
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        source = ctypes.create_string_buffer(data)
        incoming = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
        outgoing = Blob()
        try:
            # CRYPTPROTECT_UI_FORBIDDEN only; never LOCAL_MACHINE.
            if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
                raise APISettingsError('无法使用当前 Windows 账户保护或读取密钥，请重新填写。')
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            ctypes.memset(source, 0, ctypes.sizeof(source))
            if outgoing.pbData:
                if decrypt:
                    ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
                kernel.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))

    def encrypt(self, data):
        return self._transform(data)

    def decrypt(self, data):
        return self._transform(data, decrypt=True)


def _safe_path(path):
    """Reject symlinks, junctions/reparse points and nonregular config files."""
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise APISettingsError('API 设置路径包含链接，请选择普通数据文件夹。')
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise APISettingsError('API 设置的数据文件夹无效，请重新选择。')
        if part == path and not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise APISettingsError('API 设置文件无效，请重新保存。')
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise APISettingsError('API 设置文件是共享文件链接，请重新选择数据文件夹。')


def _preferences(values):
    from remote_api import normalize_base_url
    if values.get('provider') not in ('deepseek', 'custom'):
        raise APISettingsError('请选择 DeepSeek 或自定义接口。')
    try:
        base_url = normalize_base_url(values.get('base_url'))
    except Exception:
        raise APISettingsError('接口地址无效：请填写 HTTPS 地址，或本机 HTTP 地址；不能包含密钥、查询参数或账号。') from None
    model = values.get('model')
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in model):
        raise APISettingsError('请填写有效的模型名称。')
    limit = values.get('max_output_tokens')
    if type(limit) is not int or not 1 <= limit <= 32768:
        raise APISettingsError('单次回答上限必须是 1 到 32768 的整数。')
    for name in ('remember_key', 'include_memory', 'thinking'):
        if type(values.get(name)) is not bool:
            raise APISettingsError('API 选项格式不正确，请重新保存。')
    return {**{name: values[name] for name in DEFAULTS}, 'base_url': base_url, 'model': model.strip()}


def _valid_key(value):
    if not isinstance(value, str) or len(value) > MAX_KEY_BYTES or any(not 33 <= ord(c) <= 126 for c in value):
        raise APISettingsError('密钥格式不正确，请重新填写，不要包含空格或换行。')
    return value


class APISettings:
    def __init__(self, directory=None, *, cipher=None):
        if directory is None:
            from paths import ARTIFACTS
            directory = ARTIFACTS
        # abspath normalizes '.' without following filesystem links.
        self.directory = Path(os.path.abspath(directory))
        self.path = self.directory / 'api_settings.json'
        self._cipher = cipher if cipher is not None else WindowsDPAPICipher()
        self._memory_key = ''
        self._memory_base_url = None
        self._lock = threading.RLock()

    def _read(self):
        try:
            _safe_path(self.path)
            before = self.path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise APISettingsError('API 设置文件损坏或过大，请重新保存并填写密钥。')
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            descriptor = os.open(self.path, flags)
            with os.fdopen(descriptor, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise APISettingsError('API 设置文件已变化，请重试。')
                _safe_path(self.path)
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError()
            data = json.loads(raw.decode('utf-8'))
            if not isinstance(data, dict) or data.get('version') != 1:
                raise ValueError()
            config = _preferences({**DEFAULTS, **{name: data[name] for name in DEFAULTS if name in data}})
            protected = data.get('encrypted_key')
            if protected is not None and not isinstance(protected, str):
                raise ValueError()
            return config, protected if config['remember_key'] else None, ''
        except FileNotFoundError:
            return dict(DEFAULTS), None, ''
        except APISettingsError as exc:
            return dict(DEFAULTS), None, str(exc)
        except Exception:
            return dict(DEFAULTS), None, 'API 设置文件损坏或无法读取，请重新保存并填写密钥。'

    def _key(self, config, protected, notice):
        if notice:
            raise APISettingsError(notice)
        if not config['remember_key']:
            return self._memory_key if self._memory_base_url == config['base_url'] else ''
        if not protected:
            return ''
        try:
            ciphertext = base64.b64decode(protected, validate=True)
            payload = json.loads(self._cipher.decrypt(ciphertext).decode('utf-8'))
            if (not isinstance(payload, dict) or payload.get('version') != 1
                    or payload.get('base_url') != config['base_url']):
                raise ValueError()
            return _valid_key(payload['key'])
        except Exception:
            raise APISettingsError('已保存的密钥无法在当前账户读取，或连接地址已变化。请重新填写密钥。') from None

    def public(self):
        with self._lock:
            config, protected, notice = self._read()
            available = False
            try:
                available = bool(self._key(config, protected, notice))
            except APISettingsError as exc:
                notice = str(exc)
            if not notice:
                notice = ('密钥已由当前 Windows 账户加密保存。' if protected and available else
                          '密钥仅保留在当前窗口，关闭后需要重新填写。' if available else
                          '尚未填写密钥。')
            return {**config, 'key_saved': bool(protected), 'key_available': available, 'key_notice': notice}

    def get_key(self):
        with self._lock:
            return self._key(*self._read())

    def connection(self):
        """Take one settings snapshot so a key never pairs with another URL."""
        with self._lock:
            config, protected, notice = self._read()
            return dict(config), self._key(config, protected, notice)

    def _write(self, document):
        temporary = None
        try:
            _safe_path(self.path)
            self.directory.mkdir(parents=True, exist_ok=True)
            _safe_path(self.path)
            descriptor, temporary = tempfile.mkstemp(prefix='.api-settings-', suffix='.tmp', dir=self.directory)
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
                _safe_path(Path(temporary))
                json.dump(document, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            _safe_path(self.path)
            os.replace(temporary, self.path)
            temporary = None
        except APISettingsError:
            raise
        except Exception:
            raise APISettingsError('无法保存 API 设置，请检查数据文件夹是否可写后重试。') from None
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except OSError:
                    pass

    def save(self, *, provider, base_url, model, key=None, remember_key=True,
             include_memory=False, max_output_tokens=1024, thinking=False):
        with self._lock:
            config = _preferences(dict(provider=provider, base_url=base_url, model=model,
                remember_key=remember_key, include_memory=include_memory,
                max_output_tokens=max_output_tokens, thinking=thinking))
            previous, protected, notice = self._read()
            if key is None:
                if config['base_url'] == previous['base_url'] and not notice:
                    # An unreadable saved key must be replaced or explicitly cleared.
                    secret = self._key(previous, protected, notice)
                else:
                    secret = ''
            else:
                secret = _valid_key(key)
            document = {'version': 1, **config}
            if remember_key and secret:
                payload = json.dumps({'version': 1, 'base_url': config['base_url'], 'key': secret}, ensure_ascii=False).encode('utf-8')
                try:
                    encrypted = self._cipher.encrypt(payload)
                    if not isinstance(encrypted, bytes) or not encrypted or len(encrypted) > MAX_FILE_BYTES // 2:
                        raise ValueError()
                    document['encrypted_key'] = base64.b64encode(encrypted).decode('ascii')
                except Exception:
                    raise APISettingsError('当前系统无法安全保存密钥，请关闭“记住密钥”后重试。') from None
            self._write(document)
            self._memory_key = secret if not remember_key else ''
            self._memory_base_url = config['base_url'] if self._memory_key else None
            return self.public()

    def clear_key(self):
        with self._lock:
            config, _protected, notice = self._read()
            if notice:
                raise APISettingsError('API 设置无法读取，请重新保存连接并清空密钥。')
            self._write({'version': 1, **config})
            self._memory_key = ''
            self._memory_base_url = None
            return self.public()
