"""Bounded before/after snapshots for ordinary project text files."""

from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
from threading import Lock
from uuid import uuid4
from zipfile import BadZipFile, ZIP_STORED, ZipFile

from agent_memory import SECRET
from agent_review import MAX_BUDGET, MAX_FILES as PREVIEW_FILES, MAX_FILE_LINES, MAX_INPUT, MAX_TOTAL_LINES, _text_diff
from agent_task_state import NAMED_SECRET, PRIVATE_KEY, URL_CREDENTIAL, _unique_keys
from project_agent import (
    BINARY_EXTENSIONS, IGNORED, ToolError, _atomic_bytes, _checked_stat, _decode, _limit,
    _path_parts, _pinned, _private, _read_bytes, _sha, _target, _workspace,
)


FILE_LIMIT = 256 * 1024
TOTAL_LIMIT = 32 * 1024 * 1024
FILE_COUNT_LIMIT = 2000
ENTRY_LIMIT = 12000
_MANIFEST_LIMIT = 3 * 1024 * 1024
_PATH_BYTES = 1024
_ID = re.compile(r'[0-9a-f]{32}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_IGNORED = IGNORED | {
    '.tox', '.nox', '.gradle', '.cache', 'site-packages', 'models', 'checkpoints',
    'capabilities', 'portable_data', 'training-tools',
}
_BINARY = BINARY_EXTENSIONS | {
    '.7z', '.rar', '.tar', '.xz', '.pyc', '.pyo', '.gguf', '.safetensors', '.bin', '.ckpt',
    '.pt', '.pth', '.onnx', '.tflite', '.npy', '.npz', '.parquet', '.sqlite3',
}
_SCOPE_NOTICE = ('快照覆盖普通 UTF-8 项目文本，每阶段最多 2,000 个文件，每个最多 256 KiB，'
                 '前后内容合计最多 32 MiB。密钥、二进制、模型、忽略目录和链接不在覆盖范围内。')
_NO_AFTER = '缺少任务结束快照，无法核实前后改动。'
_FINISH_LOCK = Lock()


class SnapshotCancelled(ToolError):
    """Capture was cancelled before a complete archive could be saved."""


def _protected(text):
    return any(pattern.search(text) for pattern in (SECRET, NAMED_SECRET, PRIVATE_KEY, URL_CREDENTIAL))


def _excluded(name):
    lowered = name.casefold()
    return (lowered in _IGNORED or lowered.startswith('.freeai-') or _private(name)
            or Path(lowered).suffix in _BINARY or _protected(name))


def _parts(relative):
    parts = _path_parts(relative)
    if (relative != '/'.join(parts) or len(relative.encode('utf-8')) > _PATH_BYTES
            or any(_excluded(part) for part in parts)):
        raise ToolError('项目快照包含不受支持的路径。')
    return parts


def _identity(path):
    info = _checked_stat(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise ToolError('项目工作区已移动或替换。')
    return [info.st_dev, info.st_ino]


def _signature(info):
    return None if info is None else (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)


def _ensure_directory(path):
    existing = path
    while _checked_stat(existing) is None:
        existing = existing.parent
    root = _workspace(existing)
    parts = path.relative_to(existing).parts
    with _pinned(root, parts, directory=True, create=lambda directory, _: directory.mkdir()):
        return _workspace(path)


def _outside_workspace(root, workspace):
    if root == workspace or root in workspace.parents:
        raise ToolError('项目不能位于快照存储目录中。')


@contextmanager
def _opened(snapshots_root, snapshot_dir, workspace):
    root, workspace = _workspace(snapshots_root), _workspace(workspace)
    requested = Path(snapshot_dir)
    if requested.parent != (root if requested.is_absolute() else Path('.')) or not _ID.fullmatch(requested.name):
        raise ToolError('快照必须位于存储目录的直接子目录。')
    _outside_workspace(root, workspace)
    with _pinned(root, (requested.name,), directory=True):
        run = _workspace(_target(root, (requested.name,), directory=True))
        yield root, run, workspace


def _cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise SnapshotCancelled('已取消捕获项目快照。')


def _workspace_key(workspace):
    return _sha(os.path.normcase(str(workspace)).encode('utf-8'))


def _manifest_json(body):
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _capture(root, run, workspace, phase, *, previous=None, before_hash=None,
             expected_identity=None, cancel_event=None):
    initial_identity = _identity(workspace)
    if expected_identity is not None and initial_identity != expected_identity:
        raise ToolError('项目工作区已移动或替换。')
    bundles = previous if previous is not None else {}
    known = {digest: len(data) for digest, data in bundles.items()}
    total = sum(known.values())
    files = {}
    paths = set()
    pending = [()]
    scanned = skipped = filtered = 0
    archive_data = BytesIO()
    with ZipFile(archive_data, 'w', compression=ZIP_STORED) as archive:
        while pending:
            _cancel(cancel_event)
            folder = pending.pop()
            try:
                with _pinned(workspace, folder, directory=True):
                    directory = _target(workspace, folder, directory=True)
                    with os.scandir(directory) as entries:
                        names = []
                        for entry in entries:
                            if scanned == ENTRY_LIMIT:
                                skipped += 1
                                pending.clear()
                                break
                            scanned += 1
                            names.append(entry.name)
                    for name in sorted(names, key=str.casefold):
                        _cancel(cancel_event)
                        if directory / name == root or _excluded(name):
                            filtered += 1
                            continue
                        relative = '/'.join((*folder, name))
                        try:
                            parts = _parts(relative)
                            info = _checked_stat(directory / name)
                            if info is not None and stat.S_ISDIR(info.st_mode):
                                if scanned < ENTRY_LIMIT:
                                    pending.append(parts)
                                else:
                                    skipped += 1
                                continue
                            path = _target(workspace, parts)
                            key = os.path.normcase(relative)
                            if len(files) >= FILE_COUNT_LIMIT or key in paths:
                                skipped += 1
                                continue
                            data = _read_bytes(path, limit=FILE_LIMIT)
                            if data is None or _signature(_checked_stat(path)) != _signature(info):
                                skipped += 1
                                continue
                            if _protected(_decode(data)):
                                skipped += 1
                                continue
                        except (OSError, ToolError, UnicodeError):
                            skipped += 1
                            continue
                        digest = _sha(data)
                        if digest not in known:
                            if total + len(data) > TOTAL_LIMIT:
                                skipped += 1
                                continue
                            known[digest] = len(data)
                            total += len(data)
                            archive.writestr('files/' + digest, data)
                        files[relative] = {'sha256': digest, 'size': len(data),
                                           'mode': stat.S_IMODE(info.st_mode) & 0o777}
                        paths.add(key)
            except SnapshotCancelled:
                raise
            except (OSError, ToolError):
                if not folder:
                    raise
                skipped += 1
        _cancel(cancel_event)
        if _identity(workspace) != initial_identity:
            raise ToolError('项目工作区已移动或替换。')
        body = {'version': 1, 'phase': phase, 'snapshot_id': run.name,
                'workspace': _workspace_key(workspace), 'workspace_identity': initial_identity,
                'before_hash': before_hash, 'files': files, 'skipped': skipped, 'filtered': filtered}
        manifest = _manifest_json(body)
        archive.writestr('manifest.json', _manifest_json({'data': body, 'sha256': _sha(manifest)}))
    with _pinned(run):
        _atomic_bytes(_target(run, (phase + '.zip',)), archive_data.getvalue())


def _bad_archive():
    return ToolError('项目快照损坏、版本不受支持或属于另一工作区。')


def _load_archive(run, workspace, phase, root, *, previous=None, before_hash=None):
    maximum = TOTAL_LIMIT + _MANIFEST_LIMIT + 512 * (FILE_COUNT_LIMIT + 1)
    with _pinned(run):
        raw = _read_bytes(_target(run, (phase + '.zip',)), limit=maximum)
    if raw is None:
        return None
    existing = previous if previous is not None else {}
    try:
        with ZipFile(BytesIO(raw), 'r') as archive:
            infos = archive.infolist()
            members = {info.filename: info for info in infos}
            if (len(members) != len(infos) or len(infos) > FILE_COUNT_LIMIT + 1
                    or any(info.compress_type != ZIP_STORED or info.compress_size != info.file_size
                           or info.flag_bits & 1 for info in infos)
                    or 'manifest.json' not in members or members['manifest.json'].file_size > _MANIFEST_LIMIT):
                raise _bad_archive()
            saved = json.loads(archive.read('manifest.json'), object_pairs_hook=_unique_keys)
            body = saved['data']
            if (set(saved) != {'data', 'sha256'} or _sha(_manifest_json(body)) != saved['sha256']
                    or set(body) != {'version', 'phase', 'snapshot_id', 'workspace', 'workspace_identity',
                                     'before_hash', 'files', 'skipped', 'filtered'}
                    or type(body['version']) is not int or body['version'] != 1 or body['phase'] != phase
                    or body['snapshot_id'] != run.name or body['workspace'] != _workspace_key(workspace)
                    or body['workspace_identity'] != _identity(workspace) or body['before_hash'] != before_hash
                    or any(type(body[key]) is not int or not 0 <= body[key] <= ENTRY_LIMIT + 1
                           for key in ('skipped', 'filtered'))):
                raise _bad_archive()

            files = body['files']
            if not isinstance(files, dict) or len(files) > FILE_COUNT_LIMIT:
                raise _bad_archive()
            blobs = {}
            keys = set()
            expected = {'manifest.json'}
            total = sum(map(len, existing.values()))
            for path, row in files.items():
                parts = _parts(path)
                entry_path = workspace.joinpath(*parts)
                key = os.path.normcase(path)
                if (key in keys or entry_path == root or root in entry_path.parents
                        or set(row) != {'sha256', 'size', 'mode'} or not isinstance(row['sha256'], str)
                        or not _HASH.fullmatch(row['sha256'])
                        or type(row['size']) is not int or not 0 <= row['size'] <= FILE_LIMIT
                        or type(row['mode']) is not int or not 0 <= row['mode'] <= 0o777):
                    raise _bad_archive()
                keys.add(key)
                digest = row['sha256']
                if digest not in existing and digest not in blobs:
                    member = 'files/' + digest
                    total += row['size']
                    if (member not in members or members[member].file_size != row['size']
                            or total > TOTAL_LIMIT):
                        raise _bad_archive()
                    data = archive.read(member)
                    if _sha(data) != digest or _protected(_decode(data)):
                        raise _bad_archive()
                    blobs[digest] = data
                    expected.add(member)
                data = existing[digest] if digest in existing else blobs[digest]
                if len(data) != row['size']:
                    raise _bad_archive()
            if set(members) != expected:
                raise _bad_archive()
    except (BadZipFile, KeyError, TypeError, ValueError, UnicodeError, RuntimeError) as error:
        raise _bad_archive() from error
    return body, blobs, _sha(raw)


def _versions(root, run, workspace):
    before = _load_archive(run, workspace, 'before', root)
    if before is None:
        raise ToolError('缺少任务开始快照。')
    body, blobs, fingerprint = before
    after = _load_archive(run, workspace, 'after', root, previous=blobs, before_hash=fingerprint)
    return body, blobs, after


def _changes(before, after):
    old = {os.path.normcase(path): (path, row) for path, row in before['files'].items()}
    new = {os.path.normcase(path): (path, row) for path, row in after['files'].items()}
    changes = {}
    for key in sorted(old.keys() | new.keys(), key=str.casefold):
        old_path, left = old.get(key, (None, None))
        new_path, right = new.get(key, (None, None))
        if (left is None and before['skipped']) or (right is None and after['skipped']):
            continue
        if left is not None and right is not None and (
                left['sha256'] == right['sha256'] or old_path != new_path):
            continue
        changes[new_path or old_path] = (left, right)
    return changes


def _notice(before, after):
    lines = [_SCOPE_NOTICE]
    if before['skipped'] or after['skipped']:
        lines.append('部分快照受限或无法读取。仅显示可核实的改动，未将未扫描路径推断为新建或删除。')
    return '\n'.join(lines)


def _summary(before, after):
    if after is None:
        return {'status': 'pending', 'files': [], 'count': 0, 'notice': _NO_AFTER}
    paths = list(_changes(before, after))
    status = 'partial' if before['skipped'] or after['skipped'] else 'ready'
    return {'status': status, 'files': paths, 'count': len(paths), 'notice': _notice(before, after)}


def start_snapshot(snapshots_root, workspace, cancel_event=None):
    """Capture a bounded baseline and return its snapshot directory."""
    _cancel(cancel_event)
    workspace = _workspace(workspace)
    requested_root = Path(os.path.abspath(snapshots_root))
    _outside_workspace(requested_root, workspace)
    root = _ensure_directory(requested_root)
    _outside_workspace(root, workspace)
    run = root / uuid4().hex
    with _pinned(root):
        run.mkdir()
    try:
        with _pinned(root, (run.name,), directory=True):
            _capture(root, run, workspace, 'before', cancel_event=cancel_event)
    except (OSError, ToolError):
        try:
            with _pinned(root):
                run.rmdir()
        except OSError:
            pass
        raise
    return str(run)


def finish_snapshot(snapshots_root, snapshot_dir, workspace):
    """Capture the end state once and return the verified list of changed paths."""
    with _FINISH_LOCK, _opened(snapshots_root, snapshot_dir, workspace) as (root, run, workspace):
        before = _load_archive(run, workspace, 'before', root)
        if before is None:
            raise ToolError('缺少任务开始快照。')
        body, blobs, fingerprint = before
        after = _load_archive(run, workspace, 'after', root, previous=blobs, before_hash=fingerprint)
        if after is None:
            _capture(root, run, workspace, 'after', previous=blobs, before_hash=fingerprint,
                     expected_identity=body['workspace_identity'])
            after = _load_archive(run, workspace, 'after', root, previous=blobs, before_hash=fingerprint)
        return _summary(body, after[0])


def describe_snapshot(snapshots_root, snapshot_dir, workspace):
    """Read the saved change summary without capturing the current workspace."""
    with _opened(snapshots_root, snapshot_dir, workspace) as (root, run, workspace):
        before, _, after = _versions(root, run, workspace)
        return _summary(before, after[0] if after is not None else None)


def review_snapshot(snapshots_root, snapshot_dir, workspace, budget=24000):
    """Produce bounded net text diffs, including files changed by commands."""
    remaining = _limit(budget, 0, MAX_BUDGET, '差异预览容量')
    with _opened(snapshots_root, snapshot_dir, workspace) as (root, run, workspace):
        before, old_blobs, after = _versions(root, run, workspace)
        if after is None:
            return {'files': [], 'truncated': False, 'notice': _NO_AFTER}
        end, new_blobs, _ = after
        blobs = {**old_blobs, **new_blobs}
        changes = _changes(before, end)
        files = []
        truncated = len(changes) > PREVIEW_FILES
        input_left, lines_left = MAX_INPUT, MAX_TOTAL_LINES
        for path, (left, right) in list(changes.items())[:PREVIEW_FILES]:
            row = {'path': path, 'diff': '', 'added': 0, 'removed': 0, 'status': 'truncated'}
            files.append(row)
            size = (left['size'] if left else 0) + (right['size'] if right else 0)
            if size > input_left or lines_left <= 0 or remaining <= 0:
                truncated = True
                continue
            input_left -= size
            original = _decode(blobs[left['sha256']]).splitlines(keepends=True) if left else None
            revised = _decode(blobs[right['sha256']]).splitlines(keepends=True) if right else []
            line_count = len(original or ()) + len(revised)
            lines_left -= line_count
            if line_count > MAX_FILE_LINES or lines_left < 0:
                truncated = True
                continue
            diff, added, removed, clipped = _text_diff(path, original, revised, remaining)
            remaining -= len(diff.encode('utf-8'))
            if right is None:
                diff = diff.replace('+++ b/' + path + '\n', '+++ /dev/null\n', 1)
            status = 'new' if left is None else 'deleted' if right is None else 'modified'
            row.update(diff=diff, added=added, removed=removed, status='truncated' if clipped else status)
            truncated |= clipped
        notice = _notice(before, end)
        if not files:
            notice += '\n未发现可核实的文本改动。'
        if truncated:
            notice += '\n部分改动超出差异预览上限。'
        return {'files': files, 'truncated': truncated, 'notice': notice}


def _selected_changes(changes, paths):
    if paths is None:
        return changes
    if (not isinstance(paths, (list, tuple))
            or any(type(path) is not str or path not in changes for path in paths)
            or len(paths) != len(set(paths))):
        raise ToolError('请选择快照中已核实的改动路径，每个路径只能选择一次。')
    return {path: changes[path] for path in paths}


def restore_snapshot(snapshots_root, snapshot_dir, workspace, *, paths=None):
    """Revert selected changes whose live file still matches the recorded end state."""
    with _opened(snapshots_root, snapshot_dir, workspace) as (root, run, workspace):
        before, old_blobs, after = _versions(root, run, workspace)
        end = after[0] if after is not None else None
        changes = _selected_changes(_changes(before, end) if end is not None else {}, paths)
        if end is None:
            return {'restored': [], 'conflicts': [], 'notice': _NO_AFTER}
        restored, conflicts = [], []
        # Remove added files first, then restore prior contents. Preserve directories.
        for path, (left, right) in sorted(changes.items(), key=lambda item: item[1][0] is not None):
            parts = _parts(path)
            expected = right['sha256'] if right else None
            original = left['sha256'] if left else None

            def current():
                if _identity(workspace) != before['workspace_identity']:
                    raise ToolError('项目工作区已移动或替换。')
                target = _target(workspace, parts)
                original_info = _signature(_checked_stat(target))
                data = _read_bytes(target, limit=FILE_LIMIT)
                if original_info != _signature(_checked_stat(target)):
                    raise ToolError('文件正在变化。')
                return target, _sha(data), original_info

            def checked_current():
                target, digest, signature = current()
                if digest != expected:
                    raise ToolError('文件在任务结束后已改变。')
                return target, signature

            try:
                with _pinned(workspace, parts):
                    _, live_hash, _ = current()
                if live_hash == original:
                    continue
                if live_hash != expected:
                    raise ToolError('文件在任务结束后已改变。')
                create = (lambda directory, _: directory.mkdir()) if right is None else None
                with _pinned(workspace, parts, create=create):
                    target, signature = checked_current()
                    if original is None:
                        if signature != _signature(_checked_stat(target)):
                            raise ToolError('文件正在变化。')
                        target.unlink()
                    else:
                        _atomic_bytes(target, old_blobs[original], validate=checked_current, mode=left['mode'])
                restored.append(path)
            except (OSError, ToolError):
                conflicts.append(path)
        notice = _notice(before, end)
        if conflicts:
            notice += '\n已保留任务结束后变化、无权操作或无法核实的文件。'
        if not restored and not conflicts:
            notice += '\n没有需要恢复的已核实改动。'
        return {'restored': restored, 'conflicts': conflicts, 'notice': notice}
