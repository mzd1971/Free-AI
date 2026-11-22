"""Verified, resumable capability downloads. No ML dependency is imported here.

An installed receipt records checksums of every payload file. Lists are cheap:
unchanged files reuse their verified metadata; changed files are rehashed. Call
verify() for a full integrity check. Downloads stay in the selected data folder.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


class PackageError(RuntimeError):
    """A download/install error with a user-facing message."""


class DownloadCancelled(PackageError):
    """Partial downloads are preserved for the next attempt."""


def _hash(path, cancel_event=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            _cancel(cancel_event)
            digest.update(chunk)
    return digest.hexdigest()


def _cancel(event):
    if event is not None and event.is_set():
        raise DownloadCancelled('下载已暂停。下次点击继续下载即可接着完成。')


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _relative(value):
    """Reject Windows paths too, even when tests run on POSIX."""
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise PackageError('能力包包含无效文件路径。')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('..', '.', '') for part in value.split('/')):
        raise PackageError('能力包包含越界文件路径。')
    # Avoid NTFS aliases, alternate streams and special devices.
    for part in path.parts:
        if part.rstrip(' .') != part or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?', part):
            raise PackageError('能力包包含不受支持的 Windows 文件名。')
    return path


class PackageManager:
    def __init__(self, root=None, catalog=None):
        if root is None:
            from paths import MODELS_ROOT
            root = MODELS_ROOT
        self.root = Path(root).absolute()
        default_catalog = catalog is None
        if default_catalog:
            catalog = json.loads(Path(__file__).with_name('capability_catalog.json').read_text(encoding='utf-8'))
        if 'packages' in catalog:
            catalog = {item['id']: item for item in catalog['packages']}
        self.catalog = dict(catalog)
        if default_catalog and 'engine-cpu' in self.catalog:
            from hf_models import load_specs
            self.catalog.update(load_specs(self))
            from training_publish import load_specs as load_trained_specs
            self.catalog.update(load_trained_specs(self))
        self._validate_catalog()
        self._verified = {}
        self._active = set()
        self._thread_lock = threading.RLock()
        self._states = {}

    def _validate_catalog(self):
        for identity, spec in self.catalog.items():
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', identity):
                raise PackageError('能力包名称无效。')
            _relative(spec['entrypoint'])
            if spec.get('id', identity) != identity:
                raise PackageError('能力包名称不一致。')
            previous = spec.get('legacy_fingerprints', [])
            if not isinstance(previous, list) or any(not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value) for value in previous):
                raise PackageError('旧版本能力包校验信息无效。')
            for dep in spec.get('dependencies', []):
                if dep not in self.catalog:
                    raise PackageError('缺少能力包依赖定义。')
            if spec.get('local_training') is True:
                if (not identity.startswith('trained-') or spec.get('kind') != 'model'
                        or spec.get('assets') != []
                        or spec.get('base_model_id') not in spec.get('dependencies', [])):
                    raise PackageError('本地训练成果的模型资料无效。')
            elif not spec.get('assets'):
                raise PackageError('能力包没有下载文件。')
            for asset in spec['assets']:
                _relative(asset['filename'])
                if '/' in asset['filename'] or asset.get('format') not in ('file', 'zip'):
                    raise PackageError('下载文件定义无效。')
                if not isinstance(asset.get('size'), int) or asset['size'] <= 0:
                    raise PackageError('下载文件大小无效。')
                if not re.fullmatch('[a-fA-F0-9]{64}', asset.get('sha256', '')):
                    raise PackageError('下载文件缺少 SHA256 校验。')
                url = urllib.parse.urlsplit(asset['url'])
                # HTTP is only permitted for the local test server.
                if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')):
                    raise PackageError('下载地址必须使用 HTTPS。')
        for identity in self.catalog:
            self._dependency_order(identity)

    def _spec(self, identity):
        try:
            return self.catalog[identity]
        except KeyError:
            raise PackageError('没有找到这个能力包。') from None

    def _dependency_order(self, identity):
        order, visiting = [], set()
        def visit(item):
            if item in visiting:
                raise PackageError('能力包依赖形成循环。')
            if item in order:
                return
            visiting.add(item)
            for dep in self._spec(item).get('dependencies', []):
                visit(dep)
            visiting.remove(item)
            order.append(item)
        visit(identity)
        return order

    def _safe(self, relative):
        relative = str(_relative(str(relative).replace(os.sep, '/')))
        result = self.root / relative
        # No directory junction or symlink may turn our managed subtree outward.
        if not result.resolve().is_relative_to(self.root.resolve()):
            raise PackageError('能力包路径超出了数据文件夹。')
        for node in [self.root, *self.root.parents, result, *result.parents]:
            if node.exists() and (node.is_symlink() or getattr(node.lstat(), 'st_file_attributes', 0) & 0x400):
                raise PackageError('能力包文件夹不能使用链接或目录联接。')
        return result

    def _fingerprint(self, identity):
        return hashlib.sha256(json.dumps(self._spec(identity), sort_keys=True).encode()).hexdigest()

    def _receipt(self, identity):
        try:
            value = json.loads(self._safe(f'installed/{identity}/receipt.json').read_text(encoding='utf-8'))
            # A catalog release may explicitly recognize an older receipt when
            # only presentation metadata changed and the payload is identical.
            accepted = [self._fingerprint(identity), *self._spec(identity).get('legacy_fingerprints', [])]
            if value.get('fingerprint') not in accepted or not value.get('files'):
                return None
            return value
        except (OSError, ValueError, TypeError):
            return None

    def ready(self, identity, *, full=False):
        spec = self._spec(identity)
        if spec.get('local_training') is True:
            from training_publish import ready
            return ready(self, identity, full=full) and all(
                self.ready(dep, full=full) for dep in spec.get('dependencies', []))
        receipt = self._receipt(identity)
        if receipt is None:
            return False
        try:
            main = self._safe(f'installed/{identity}/{receipt["entrypoint"]}')
            if not main.is_file() or receipt['entrypoint'] not in receipt['files']:
                return False
            for name, info in receipt['files'].items():
                path = self._safe(f'installed/{identity}/{name}')
                meta = path.stat()
                if meta.st_size != info['size']:
                    return False
                signature = (meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)
                cached = self._verified.get(str(path))
                # A receipt is written only after installation checksum checks.
                saved = tuple(info.get('stat', ()))
                if full or (cached != signature and saved != signature):
                    if _hash(path) != info['sha256']:
                        return False
                self._verified[str(path)] = signature
            return all(self.ready(dep, full=full) for dep in self._spec(identity).get('dependencies', []))
        except (OSError, KeyError, ValueError, TypeError, PackageError):
            return False

    def verify(self, identity):
        return self.ready(identity, full=True)

    def asset_path(self, identity):
        if not self.ready(identity):
            raise PackageError(f'请先在“能力中心”下载或修复“{self._spec(identity)["title"]}”。')
        if self._spec(identity).get('local_training') is True:
            from training_publish import asset_path
            return asset_path(self, identity)
        return self._safe(f'installed/{identity}/{self._receipt(identity)["entrypoint"]}')

    def refresh_trained_models(self):
        """Discover newly published results without interrupting a loaded model."""
        from training_publish import load_specs
        with self._thread_lock:
            keep = {key: spec for key, spec in self.catalog.items()
                    if not spec.get('local_training') or key in self._active}
            self.catalog = {**keep, **load_specs(self)}

    def list_packages(self):
        result = []
        for identity, spec in self.catalog.items():
            installed = self.ready(identity)
            if spec.get('local_training') is True:
                from training_publish import installed_size
                result.append({**spec, 'size': installed_size(self, identity),
                               'installed': installed, 'status': 'installed' if installed else 'repair',
                               'downloaded': 0, 'installed_size': installed_size(self, identity),
                               'download_size': 0})
                continue
            partial = sum(self._safe(f'downloads/{identity}/{asset["filename"]}.part').stat().st_size
                          for asset in spec['assets']
                          if self._safe(f'downloads/{identity}/{asset["filename"]}.part').is_file())
            receipt = self._receipt(identity)
            status = 'installed' if installed else ('repair' if receipt else ('paused' if partial else 'not_installed'))
            if self._states.get(identity) in ('downloading', 'verifying', 'extracting'):
                status = self._states[identity]
            result.append({**spec, 'size': sum(asset['size'] for asset in spec['assets']),
                           'installed': installed, 'status': status, 'downloaded': partial,
                           'installed_size': sum(info['size'] for info in receipt['files'].values()) if receipt else 0,
                           'download_size': self.download_size(identity)})
        return result

    def download_size(self, identity):
        return sum(sum(a['size'] for a in self._spec(item)['assets']) for item in self._dependency_order(identity)
                   if not self.ready(item))

    def set_in_use(self, identity, in_use=True):
        """Runtime holds an in-process lease while a component is loaded."""
        self._spec(identity)
        if in_use:
            self._active.add(identity)
        else:
            self._active.discard(identity)

    def _emit(self, progress, identity, phase, downloaded=0, total=0, speed=0, message=''):
        self._states[identity] = phase
        if progress:
            progress({'package_id': identity, 'phase': phase, 'downloaded': downloaded,
                      'total': total, 'speed': speed, 'message': message})

    @contextmanager
    def _operation(self):
        with self._thread_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            lock = self._safe('operation.lock')
            stream = lock.open('a+b')
            try:
                if lock.stat().st_size == 0:
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError:
                        raise PackageError('另一个 Free AI 正在准备能力包，请稍后重试。') from None
                else:
                    import fcntl
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        raise PackageError('另一个 Free AI 正在准备能力包，请稍后重试。') from None
                try:
                    yield
                finally:
                    stream.seek(0)
                    if os.name == 'nt':
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    def install(self, identity, progress=None, cancel_event=None):
        if self._spec(identity).get('local_training') is True:
            raise PackageError('这是本机训练成果。请在训练工作台重新验收并加入客户端，不能联网下载。')
        with self._operation():
            for item in self._dependency_order(identity):
                _cancel(cancel_event)
                if self.ready(item):
                    self._emit(progress, item, 'installed', message='已经准备好，无需重复下载。')
                    continue
                try:
                    self._install_one(item, progress, cancel_event)
                finally:
                    self._states.pop(item, None)
        return self.asset_path(identity)

    def _download(self, identity, asset, progress, cancel_event):
        part = self._safe(f'downloads/{identity}/{asset["filename"]}.part')
        part.parent.mkdir(parents=True, exist_ok=True)
        total = asset['size']
        downloaded = part.stat().st_size if part.exists() else 0
        if downloaded > total:
            part.unlink()
            downloaded = 0
        if downloaded == total:
            return part
        headers = {'User-Agent': 'FreeAI/2.0 capability-downloader', 'Accept-Encoding': 'identity'}
        if downloaded:
            headers['Range'] = f'bytes={downloaded}-'
        request = urllib.request.Request(asset['url'], headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status == 206:
                    content_range = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if not content_range or tuple(map(int, content_range.groups())) != (downloaded, total - 1, total):
                        raise PackageError('下载服务器返回了错误的续传范围，已保留下载进度。')
                elif response.status == 200:
                    downloaded = 0  # Server ignored Range: replace, never append.
                else:
                    raise PackageError(f'下载服务器返回状态 {response.status}。')
                declared = response.headers.get('Content-Length')
                if declared is not None and int(declared) != total - downloaded:
                    raise PackageError('下载文件大小与发布清单不一致，已停止下载。')
                start, initial, last = time.monotonic(), downloaded, 0
                mode = 'ab' if downloaded else 'wb'
                with part.open(mode) as stream:
                    while True:
                        _cancel(cancel_event)
                        chunk = response.read(512 * 1024)
                        if not chunk:
                            break
                        if downloaded + len(chunk) > total:
                            raise PackageError('下载文件超过发布清单大小，已停止下载。')
                        stream.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last >= .15 or downloaded == total:
                            self._emit(progress, identity, 'downloading', downloaded, total,
                                       (downloaded - initial) / max(.001, now - start), '正在下载，可暂停后继续。')
                            last = now
                    stream.flush()
                if downloaded != total:
                    raise PackageError('连接提前结束，已保存进度。请点击继续下载。')
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise PackageError(f'下载连接中断，进度已保存，可稍后重试。{error}') from error
        return part

    def _extract(self, archive, asset, staging, cancel_event):
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            limit = asset.get('max_unpacked_size', asset['size'] * 15)
            if sum(member.file_size for member in members) > limit or len(members) > 15000:
                raise PackageError('压缩包解压大小超过允许范围。')
            targets = set()
            for member in members:
                raw = member.filename.rstrip('/')
                if not raw:
                    continue
                safe_name = _relative(raw)
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise PackageError('压缩包不能包含符号链接。')
                if member.is_dir():
                    continue
                name = safe_name.name if asset.get('flatten') else str(safe_name)
                target = staging / name
                if name.casefold() in targets or target.exists():
                    raise PackageError('压缩包包含重复文件名。')
                targets.add(name.casefold())
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as incoming, target.open('wb') as outgoing:
                    written = 0
                    while chunk := incoming.read(1024 * 1024):
                        _cancel(cancel_event)
                        written += len(chunk)
                        if written > member.file_size:
                            raise PackageError('压缩包成员大小异常。')
                        outgoing.write(chunk)

    def _install_one(self, identity, progress, cancel_event):
        spec = self._spec(identity)
        if identity in self._active:
            raise PackageError('这个能力包正在使用，请先停止相关任务。')
        staging = self._safe(f'staging/{identity}-{uuid.uuid4().hex}')
        staging.mkdir(parents=True, exist_ok=False)
        parts = []
        try:
            required = 64 * 1024 * 1024
            for asset in spec['assets']:
                part = self._safe(f'downloads/{identity}/{asset["filename"]}.part')
                remaining = max(0, asset['size'] - (part.stat().st_size if part.exists() else 0))
                required += remaining + (asset.get('max_unpacked_size', asset['size'] * 15) if asset['format'] == 'zip' else 0)
            if shutil.disk_usage(self.root).free < required:
                raise PackageError(f'数据盘空间不足，请至少空出 {required / 1e9:.2f} GB 后继续。')
            for asset in spec['assets']:
                _cancel(cancel_event)
                part = self._download(identity, asset, progress, cancel_event)
                parts.append(part)
                self._emit(progress, identity, 'verifying', asset['size'], asset['size'], message='正在核验文件完整性。')
                if _hash(part, cancel_event).lower() != asset['sha256'].lower():
                    part.unlink(missing_ok=True)
                    raise PackageError('文件校验不通过，已丢弃损坏下载。请重试。')
                if asset['format'] == 'zip':
                    self._emit(progress, identity, 'extracting', message='正在准备运行组件。')
                    self._extract(part, asset, staging, cancel_event)
                else:
                    # Preserve resumable source until commit. Hard links use no
                    # additional disk space and are confined to our staging area.
                    try:
                        os.link(part, staging / asset['filename'])
                    except OSError:
                        if shutil.disk_usage(self.root).free < asset['size'] + 64 * 1024 * 1024:
                            raise PackageError('文件系统不支持快速安装，请额外空出模型大小的空间后重试。')
                        shutil.copyfile(part, staging / asset['filename'])
            candidates = [path for path in staging.rglob(spec['entrypoint']) if path.is_file()]
            if len(candidates) != 1:
                raise PackageError('能力包中缺少唯一的运行文件，请检查发布清单。')
            records = {}
            for path in staging.rglob('*'):
                if path.is_file():
                    _cancel(cancel_event)
                    meta = path.stat()
                    records[path.relative_to(staging).as_posix()] = {'size': meta.st_size,
                        'sha256': _hash(path, cancel_event), 'stat': [meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns]}
            receipt = {'fingerprint': self._fingerprint(identity), 'version': spec['version'],
                       'entrypoint': candidates[0].relative_to(staging).as_posix(), 'files': records,
                       'installed_at': time.time()}
            _atomic_json(staging / 'receipt.json', receipt)
            _cancel(cancel_event)
            final = self._safe(f'installed/{identity}')
            final.parent.mkdir(parents=True, exist_ok=True)
            backup = self._safe(f'staging/previous-{identity}-{uuid.uuid4().hex}')
            if final.exists():
                os.replace(final, backup)
            try:
                os.replace(staging, final)
            except Exception:
                if backup.exists():
                    os.replace(backup, final)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            for part in parts:
                part.unlink(missing_ok=True)
            self._emit(progress, identity, 'installed', sum(a['size'] for a in spec['assets']),
                       sum(a['size'] for a in spec['assets']), message='已准备好，可以使用。')
        finally:
            if staging.exists():
                # Only our freshly created, checked staging path is removed.
                shutil.rmtree(staging)

    def uninstall(self, identity):
        self._spec(identity)
        with self._operation():
            if identity in self._active:
                raise PackageError('这个能力包正在使用，请先停止相关任务。')
            dependents = [spec['title'] for other, spec in self.catalog.items()
                          if other != identity and identity in self._dependency_order(other)
                          and (spec.get('local_training') is True or self._receipt(other))]
            if dependents:
                raise PackageError('请先移除依赖它的能力：' + '、'.join(dependents))
            if self._spec(identity).get('local_training') is True:
                from training_publish import remove
                remove(self, identity)
                self.catalog.pop(identity, None)
                self._verified.clear()
                return
            for relative in (f'installed/{identity}', f'downloads/{identity}'):
                target = self._safe(relative)
                if target.exists():
                    # Check descendants before a recursive operation too.
                    for path in target.rglob('*'):
                        self._safe(path.relative_to(self.root).as_posix())
                    shutil.rmtree(target)
            self._states.pop(identity, None)
            self._verified.clear()
