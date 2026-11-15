"""Durable progress and observed verification for a single project-agent run."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

from agent_memory import SECRET


STATE_FILE = 'task.json'
STATE_LIMIT = 384 * 1024
TASK_LIMIT = 8192
PLAN_LIMIT = 12
EDIT_LIMIT = 24
CHECK_LIMIT = 16
CHECK_ID_LIMIT = 1600
OBSERVATION_LIMIT = 16
STATUSES = {'running', 'completed', 'failed', 'stopped', 'limit', 'unfinished'}
PHASES = {'ready', 'executing'}
STEP_STATUSES = {'pending', 'in_progress', 'completed'}
HASH = re.compile(r'[0-9a-f]{64}\Z')
PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----', re.IGNORECASE)
NAMED_SECRET = re.compile(
    r"""(?i)(\b[\w-]*(?:api[_ -]?key|access[_ -]?token|secret[_ -]?key|password|passwd|token)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)"""
)
URL_CREDENTIAL = re.compile(r'(\b[\w+.-]+://)[^\s/:@]+:[^\s/@]+@')
SECRET_OPTION = re.compile(
    r'--(?:[\w-]*api[-_]?key|[\w-]*access[-_]?token|secret[-_]?key|password|passwd|token|secret)'
    r'(?:=.*)?\Z', re.IGNORECASE,
)
HIDDEN = '[已隐藏凭据]'


def _clip(value, limit):
    data = value.encode('utf-8', errors='replace')
    if len(data) <= limit:
        return data.decode('utf-8')
    if limit < 3:
        return data[:limit].decode('utf-8', errors='ignore')
    return data[:limit - 3].decode('utf-8', errors='ignore').rstrip() + '…'


def _clean(value, limit, *, strip=True):
    if not isinstance(value, str):
        return ''
    if PRIVATE_KEY.search(value):
        return _clip(HIDDEN, limit)
    value = SECRET.sub(HIDDEN, value)
    value = NAMED_SECRET.sub(lambda match: match[1] + HIDDEN, value)
    value = URL_CREDENTIAL.sub(lambda match: match[1] + HIDDEN + '@', value)
    value = ''.join(char for char in value if ord(char) >= 32 or char in '\t\n')
    return _clip(value.strip() if strip else value, limit)


def _command(arguments, workspace):
    argv = arguments.get('argv')
    if not isinstance(argv, list):
        return '', ''
    parts = []
    hide_next = False
    for argument in argv:
        if hide_next:
            parts.append(HIDDEN)
            hide_next = False
        elif isinstance(argument, str) and SECRET_OPTION.fullmatch(argument):
            option, separator, _ = argument.partition('=')
            parts.append(option + (separator + HIDDEN if separator else ''))
            hide_next = not separator
        else:
            parts.append(_clean(argument, 32768, strip=False))
    cwd = arguments.get('cwd', '.')
    cwd = os.path.normcase(os.path.normpath(Path(workspace) / cwd)) if isinstance(cwd, str) else ''
    cwd = _clean(cwd, 16384, strip=False)
    identity = json.dumps([cwd, parts], ensure_ascii=False, separators=(',', ':'))
    command = ' '.join(json.dumps(arg, ensure_ascii=False) if any(c.isspace() for c in arg) else arg
                       for arg in parts)
    return _clip(command, 520), hashlib.sha256(identity.encode('utf-8')).hexdigest()


def _run_name(value):
    from project_agent import _path_parts

    parts = _path_parts(os.fspath(value))
    if len(parts) != 1:
        raise ValueError('续跑记录必须位于运行记录根目录的直接子目录。')
    return parts[0]


def _parent_name(value):
    supplied = os.fspath(value)
    if '..' in supplied.replace('\\', '/').split('/'):
        raise ValueError('上次运行编号不能包含跨目录片段。')
    path = Path(supplied)
    return _run_name(path.name if path.is_absolute() else path)


def _identity(path):
    from project_agent import _checked_stat

    info = _checked_stat(path)
    if info is None:
        raise ValueError('运行记录目录不存在。')
    return info.st_dev, info.st_ino


def _text(value, limit):
    return (isinstance(value, str) and '\x00' not in value
            and len(value.encode('utf-8')) <= limit)


def _integer(value):
    return type(value) is int and 0 <= value <= 10 ** 9


def _valid_rows(rows, limit, schema):
    return isinstance(rows, list) and len(rows) <= limit and all(
        isinstance(row, dict) and row.keys() == schema.keys()
        and all(check(row[key]) for key, check in schema.items())
        for row in rows
    )


def _hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def _validate(state, run_dir, workspace):
    fields = {'version', 'run_id', 'workspace', 'task', 'parent_run', 'status', 'phase', 'uncertain',
              'plan', 'revision', 'edits', 'checks', 'check_statuses', 'omitted_checks', 'observations'}
    if not isinstance(state, dict) or state.keys() != fields:
        raise ValueError('任务记录字段不完整或版本不受支持。')
    if (type(state['version']) is not int or state['version'] != 1
            or state['run_id'] != run_dir.name or not _text(state['workspace'], 16384)
            or state['workspace'] != os.path.normcase(str(workspace))
            or not _text(state['task'], TASK_LIMIT) or not state['task'].strip()
            or state['status'] not in STATUSES or state['phase'] not in PHASES
            or type(state['uncertain']) is not bool
            or not _integer(state['revision'])):
        raise ValueError('任务记录无效或属于其他工作区。')
    parent = state['parent_run']
    if parent is not None and (_run_name(parent) != parent
                               or os.path.normcase(parent) == os.path.normcase(state['run_id'])):
        raise ValueError('任务记录的上次运行编号无效。')
    revision = state['revision']
    statuses = state['check_statuses']
    if (type(state['omitted_checks']) is not bool or not isinstance(statuses, dict)
            or len(statuses) > CHECK_ID_LIMIT or not all(
                _hash(key) and isinstance(row, list) and len(row) == 2
                and _integer(row[0]) and row[0] <= revision and type(row[1]) is bool
                for key, row in statuses.items()
            )):
        raise ValueError('任务记录的验证版本无效。')
    if not _valid_rows(state['plan'], PLAN_LIMIT, {
            'text': lambda item: _text(item, 700) and bool(item.strip()),
            'status': lambda item: item in STEP_STATUSES,
    }) or not _valid_rows(state['edits'], EDIT_LIMIT, {
            'path': lambda item: _text(item, 1024) and bool(item),
            'sha256': lambda item: item is None or _hash(item),
    }) or not _valid_rows(state['checks'], CHECK_LIMIT, {
            'id': _hash, 'command': lambda item: _text(item, 520),
            'cwd': lambda item: _text(item, 320),
            'status': lambda item: item in ('exited', 'timeout'),
            'returncode': lambda item: type(item) is int,
            'detail': lambda item: _text(item, 560),
            'revision': lambda item: _integer(item) and item <= revision,
    }) or not _valid_rows(state['observations'], OBSERVATION_LIMIT, {
            'tool': lambda item: _text(item, 48), 'subject': lambda item: _text(item, 520),
            'status': lambda item: _text(item, 48), 'detail': lambda item: _text(item, 560),
    }):
        raise ValueError('任务记录包含无效的计划、编辑或观察。')
    for row in state['checks']:
        passed = row['status'] == 'exited' and row['returncode'] == 0
        if statuses.get(row['id']) != [row['revision'], passed]:
            raise ValueError('任务检查结果与验证状态不一致。')


def _unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('任务记录包含重复字段。')
    return result


class TaskJournal:
    """Keep bounded progress while replacing its on-disk checkpoint atomically."""

    def __init__(self, run_dir, workspace, task, parent_run=None):
        from project_agent import _workspace

        self._run_dir = _workspace(run_dir)
        self._identity = _identity(self._run_dir)
        self._writable = True
        workspace = _workspace(workspace)
        parent = _parent_name(parent_run) if parent_run is not None else None
        self._state = {
            'version': 1, 'run_id': self._run_dir.name,
            'workspace': os.path.normcase(str(workspace)),
            'task': _clean(task, TASK_LIMIT), 'parent_run': parent,
            'status': 'running', 'phase': 'ready', 'uncertain': False, 'plan': [], 'revision': 0,
            'edits': [], 'checks': [], 'check_statuses': {}, 'omitted_checks': False, 'observations': [],
        }
        _validate(self._state, self._run_dir, workspace)

    def inherit(self, previous):
        """Carry the original goal and plan with all previous checks marked stale."""
        source = previous._state
        state = self._state
        if (not self._writable or state['parent_run'] != source['run_id']
                or state['workspace'] != source['workspace']):
            raise ValueError('上次运行记录与当前续跑任务不一致。')
        state['task'] = source['task']
        state['plan'] = deepcopy(source['plan'])
        state['uncertain'] = source['phase'] == 'executing' or source['uncertain']
        state['revision'] = 1
        state['checks'] = [{**row, 'revision': 0} for row in source['checks']]
        state['check_statuses'] = {key: [0, value[1]] for key, value in source['check_statuses'].items()}
        state['omitted_checks'] = source['omitted_checks']

    def plan(self, steps):
        """Record model-authored plan entries without inferring their completion."""
        if not self._writable:
            raise ValueError('上次运行的任务记录仅供读取。')
        if not isinstance(steps, list) or len(steps) > PLAN_LIMIT:
            raise ValueError('任务计划步骤无效。')
        plan = []
        for item in steps:
            if isinstance(item, str):
                item = {'text': item, 'status': 'pending'}
            if (not isinstance(item, dict) or item.keys() != {'text', 'status'}
                    or item['status'] not in STEP_STATUSES):
                raise ValueError('任务计划步骤无效。')
            text = _clean(item['text'], 700)
            if not text:
                raise ValueError('任务计划步骤不能为空。')
            plan.append({'text': text, 'status': item['status']})
        self._state['plan'] = plan

    def _record_check(self, check_id, command, arguments, result, detail):
        checks = self._state['checks']
        revision = self._state['revision']
        statuses = self._state['check_statuses']
        if check_id not in statuses and len(statuses) >= CHECK_ID_LIMIT:
            oldest = next(iter(statuses))
            del statuses[oldest]
            checks[:] = [item for item in checks if item['id'] != oldest]
            self._state['omitted_checks'] = True
        passed = result['status'] == 'exited' and result['returncode'] == 0
        if passed:
            self._state['uncertain'] = False
        statuses[check_id] = [revision, passed]
        checks[:] = [item for item in checks if item['id'] != check_id]
        checks.append({
            'id': check_id, 'command': command, 'cwd': _clean(arguments.get('cwd', '.'), 320),
            'status': result['status'], 'returncode': result['returncode'],
            'detail': detail, 'revision': revision,
        })
        del checks[:-CHECK_LIMIT]

    def observe(self, tool, arguments, result):
        """Store metadata and bounded output, excluding file contents and edit payloads."""
        if not self._writable:
            raise ValueError('上次运行的任务记录仅供读取。')
        if tool == 'update_plan' and result.get('ok') is True:
            self.plan(result.get('steps'))
            return
        status = result.get('status') or ('ok' if result.get('ok') is True else 'error')
        subject = _clean(result.get('path') or arguments.get('path'), 520)
        detail = _clean(result.get('message'), 560)
        if tool in ('write_file', 'replace_text') and result.get('changed') is True:
            path = _clean(result.get('path') or arguments.get('path'), 1024)
            sha = result.get('sha256')
            if path:
                edits = self._state['edits']
                edits[:] = [row for row in edits if row['path'] != path]
                edits.append({'path': path, 'sha256': sha if _hash(sha) else None})
                del edits[:-EDIT_LIMIT]
            self._state['revision'] += 1
        if tool == 'run_command':
            subject, check_id = _command(arguments, self._state['workspace'])
            detail = (_clean(result.get('stderr'), 560)
                      or _clean(result.get('stdout'), 560) or detail)
            if type(result.get('returncode')) is int:
                if arguments.get('purpose') != 'check':
                    self._state['revision'] += 1
                elif status in ('exited', 'timeout') and check_id:
                    self._record_check(check_id, subject, arguments, result, detail)
        rows = self._state['observations']
        rows.append({'tool': _clean(tool, 48), 'subject': subject,
                     'status': _clean(status, 48), 'detail': detail})
        del rows[:-OBSERVATION_LIMIT]

    def save(self, status='running', phase='ready'):
        from project_agent import _atomic_bytes, _pinned, _target

        if not self._writable:
            raise ValueError('上次运行的任务记录仅供读取。')
        if status not in STATUSES or phase not in PHASES:
            raise ValueError('任务检查点状态无效。')
        state = {**self._state, 'status': status, 'phase': phase}
        data = (json.dumps(state, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(data) > STATE_LIMIT:
            raise ValueError('任务记录超过大小上限。')
        root = self._run_dir.parent
        parts = (_run_name(self._run_dir.name), STATE_FILE)

        def checked_path():
            path = _target(root, parts)
            if _identity(path.parent) != self._identity:
                raise ValueError('运行记录目录已更改。')
            return path

        with _pinned(root, parts):
            _atomic_bytes(checked_path(), data, validate=checked_path)
        self._state = state

    def export(self):
        """Return progress and validation evidence with bounded command previews."""
        state = self._state
        revision = state['revision']
        checks = []
        for row in state['checks']:
            checks.append({
                **{key: row[key] for key in ('command', 'cwd', 'status', 'returncode', 'detail')},
                'current': state['phase'] == 'ready' and row['revision'] == revision,
            })
        statuses = tuple(state['check_statuses'].values())
        ready = state['phase'] == 'ready'
        if ready and any(rev == revision and not passed for rev, passed in statuses):
            verification = 'failed'
        elif state['omitted_checks'] or (statuses and (not ready or any(rev != revision for rev, _ in statuses))):
            verification = 'stale'
        elif statuses:
            verification = 'passed'
        else:
            verification = 'not_run'
        return {**deepcopy({key: state[key] for key in (
            'run_id', 'task', 'parent_run', 'status', 'phase', 'plan', 'edits', 'observations',
        )}), 'uncertain': state['uncertain'] or not ready,
                'verification': {'status': verification, 'checks': checks}}

    def handoff(self, budget=2500):
        """Summarize progress for a new conversation using a UTF-8 byte budget."""
        if type(budget) is not int or budget < 0:
            raise ValueError('任务摘要容量无效。')
        data = self.export()
        verification = data['verification']
        lines = []
        if data['uncertain']:
            lines.append('上次操作缺少执行结果，是否实际执行及其副作用尚不确定。先核对工作区和结果，再决定是否重新执行。')
        state = {'passed': '已通过记录中的检查', 'failed': '有失败的检查',
                 'stale': '已有检查需要重新确认', 'not_run': '尚无验证结果'}
        lines.extend((f"运行状态 {data['status']}，验证状态 {state[verification['status']]}",
                      '任务目标 ' + _clip(data['task'], min(1000, budget * 2 // 5))))
        checks = list(reversed(verification['checks']))
        checks.sort(key=lambda row: (row['status'] != 'exited' or row['returncode'] != 0,
                                     not row['current']), reverse=True)
        check_lines = []
        for check in checks[:4]:
            valid = '有效' if check['current'] else '历史'
            command = _clip(check['command'], 190)
            detail = _clip(check['detail'], 220)
            check_lines.append(f"检查 {valid} {command}，{check['status']}，退出码 {check['returncode']} {detail}")
        plan_lines = []
        for item in data['plan'][:8]:
            label = {'pending': '待处理', 'in_progress': '处理中', 'completed': '完成'}[item['status']]
            plan_lines.append(f"计划记录 {label} {_clip(item['text'], 190)}")
        edit_lines = []
        for edit in reversed(data['edits'][-6:]):
            edit_lines.append('已改动 ' + _clip(edit['path'], 220)
                              + (' SHA-256 ' + edit['sha256'] if edit['sha256'] else ''))
        recent = [f"观察 {row['tool']} {row['status']} {_clip(row['subject'], 120)} {_clip(row['detail'], 160)}"
                  for row in reversed(data['observations'][-4:])]
        summary = _clip('\n'.join(lines), budget)
        sections = [section for section in (check_lines, plan_lines, edit_lines, recent) if section]
        for index, section in enumerate(sections):
            remaining = budget - len(summary.encode('utf-8'))
            if remaining <= 1:
                break
            share = max(0, remaining // (len(sections) - index) - 1)
            block = _clip('\n'.join(section), share)
            if block:
                summary += '\n' + block
        return summary


def load_task(runs_root, run_dir, workspace):
    """Read a prior run under the configured root and validate its workspace binding."""
    from project_agent import _pinned, _read_bytes, _target, _workspace

    root = _workspace(runs_root)
    workspace = _workspace(workspace)
    supplied = os.fspath(run_dir)
    if '..' in supplied.replace('\\', '/').split('/'):
        raise ValueError('续跑记录路径不能包含跨目录片段。')
    requested = Path(supplied)
    if requested.is_absolute():
        if requested.parent != root:
            raise ValueError('续跑记录必须位于运行记录根目录的直接子目录。')
        requested = Path(requested.name)
    name = _run_name(requested)
    parts = (name, STATE_FILE)
    with _pinned(root, parts):
        path = _target(root, parts)
        raw = _read_bytes(path, limit=STATE_LIMIT)
        if raw is None:
            raise ValueError('此运行没有可续跑的任务记录。')
        try:
            state = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_keys)
            if isinstance(state, dict):
                state.setdefault('uncertain', False)
            _validate(state, path.parent, workspace)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError('续跑记录损坏、版本不受支持或属于其他工作区。') from exc
        snapshot = TaskJournal(path.parent, workspace, state['task'], state['parent_run'])
        snapshot._state = state
        snapshot._writable = False
        return snapshot
