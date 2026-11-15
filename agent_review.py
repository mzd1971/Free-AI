"""Read a bounded, verified diff from one project run's file-tool records."""

from difflib import unified_diff
import os
from pathlib import Path
import re


MAX_FILES = 40
MAX_INPUT = 2 * 1024 * 1024
MAX_FILE_LINES = 1600
MAX_TOTAL_LINES = 6000
MAX_BUDGET = 96 * 1024
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


class _PreviewLimit(ValueError):
    pass


def _group_changes(changes, workspace, runs_root):
    from project_agent import ToolError, _path_parts

    grouped = {}
    for index, change in enumerate(changes):
        if not isinstance(change, dict) or not {'path', 'before_sha256', 'after_sha256', 'backup'} <= change.keys():
            raise ToolError('运行变更记录无效。')
        parts = _path_parts(change['path'])
        try:
            relative = '/'.join(parts)
            relative.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise ToolError('运行变更记录包含无效文件名。') from exc
        path = workspace.joinpath(*parts)
        if path == runs_root or runs_root in path.parents:
            raise ToolError('变更记录引用了运行目录。')
        before = change['before_sha256']
        after = change['after_sha256']
        backup = f'backup-{index:04d}' if before is not None else None
        after_backup = change.get('after_backup')
        applied = change.get('applied', True)
        if (not isinstance(after, str) or SHA256.fullmatch(after) is None
                or (before is not None and (not isinstance(before, str) or SHA256.fullmatch(before) is None))
                or change['backup'] != backup
                or type(applied) is not bool
                or ('after_backup' in change and after_backup != f'after-{index:04d}')):
            raise ToolError('运行变更记录无效。')
        key = os.path.normcase(relative)
        if key in grouped:
            row = grouped[key]
            row['continuous'] = row['continuous'] and row['after'] == before and applied
            row.update(after=after, after_backup=after_backup)
        else:
            grouped[key] = {'path': relative, 'parts': parts, 'backup': backup, 'before': before,
                            'after_backup': after_backup, 'after': after, 'continuous': applied}
    return list(grouped.values())


def _file_versions(row, workspace, run, read):
    from project_agent import ToolError, _sha

    if not row['continuous']:
        raise ToolError('文件改动历史不连续或尚未确认。')
    original = read(run, (row['backup'],)) if row['backup'] else None
    if row['after_backup']:
        revised = read(run, (row['after_backup'],))
    else:
        # Only older formats may use the live file, and only when its hash proves it is the recorded version.
        revised = read(workspace, row['parts'])
    if _sha(original) != row['before'] or _sha(revised) != row['after']:
        raise ToolError('文件版本与运行记录不匹配。')
    return original, revised


def _text_diff(path, original, revised, budget):
    pieces = []
    added = removed = 0
    clipped = False
    lines = unified_diff(original or (), revised, '/dev/null' if original is None else 'a/' + path, 'b/' + path)
    for index, line in enumerate(lines):
        if index >= 2:
            added += line.startswith('+')
            removed += line.startswith('-')
        if not line.endswith('\n'):
            line += '\n\\ 文件末尾没有换行\n'
        size = len(line.encode('utf-8'))
        if not clipped and size <= budget:
            pieces.append(line)
            budget -= size
        else:
            clipped = True
    return ''.join(pieces), added, removed, clipped


def review_changes(runs_root, run_dir, workspace, budget=24000):
    """Report net changes per file. A diff reflects the snapshots taken by the file tools."""
    from project_agent import (FILE_LIMIT, ToolError, _checked_stat, _decode, _limit, _load_changes,
                               _path_parts, _pinned, _read_bytes, _target, _workspace)

    remaining = _limit(budget, 0, MAX_BUDGET, '差异预览容量')
    input_left, lines_left = MAX_INPUT, MAX_TOTAL_LINES

    def read(folder, parts):
        nonlocal input_left
        limit = min(FILE_LIMIT, input_left)
        with _pinned(folder, parts):
            path = _target(folder, parts)
            info = _checked_stat(path)
            if info is not None and info.st_size > limit:
                raise _PreviewLimit
            content = _read_bytes(path, limit=limit)
        input_left -= len(content or b'')
        return content

    workspace, root = _workspace(workspace), _workspace(runs_root)
    supplied = os.fspath(run_dir)
    requested = Path(supplied)
    if '..' in supplied.replace('\\', '/').split('/') or requested.parent != (root if requested.is_absolute() else Path('.')):
        raise ToolError('运行记录必须位于运行记录根目录的直接子目录。')
    parts = _path_parts(requested.name)
    with _pinned(root, parts, directory=True):
        run = _workspace(_target(root, parts, directory=True))
        manifest = _load_changes(run)
        if os.path.normcase(manifest['workspace']) != os.path.normcase(str(workspace)):
            raise ToolError('运行记录与所选工作区不匹配。')
        grouped = _group_changes(manifest['changes'], workspace, root)
        files = []
        trimmed = len(grouped) > MAX_FILES
        for row in grouped[:MAX_FILES]:
            path = row['path']
            file = {'path': path, 'diff': '', 'added': 0, 'removed': 0, 'status': 'unavailable'}
            files.append(file)
            if input_left <= 0 or lines_left <= 0 or remaining <= 0:
                file['status'], trimmed = 'truncated', True
                continue
            try:
                before, after = _file_versions(row, workspace, run, read)
                if before == after:
                    file['status'] = 'unchanged'
                    continue
                original = _decode(before).splitlines(keepends=True) if before is not None else None
                revised = _decode(after).splitlines(keepends=True)
            except _PreviewLimit:
                file['status'], trimmed = 'truncated', True
                continue
            except (OSError, ToolError):
                continue
            line_count = len(original or ()) + len(revised)
            lines_left -= line_count
            if lines_left < 0 or line_count > MAX_FILE_LINES:
                file['status'], trimmed = 'truncated', True
                continue
            diff, added, removed, clipped = _text_diff(path, original, revised, remaining)
            remaining -= len(diff.encode('utf-8'))
            status = 'new' if before is None else 'modified'
            file.update(diff=diff, added=added, removed=removed, status='truncated' if clipped else status)
            trimmed |= clipped
    notices = []
    if not files:
        notices.append('这轮任务没有文件工具修改。')
    if any(file['status'] == 'unavailable' for file in files):
        notices.append('部分文件缺少可验证的前后版本。')
    if trimmed:
        notices.append('部分改动超出预览上限。')
    return {'files': files, 'truncated': trimmed, 'notice': '\n'.join(notices)}
