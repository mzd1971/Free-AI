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
OPERATION_LIMIT = 16
CHECK_STATES = {'pending', 'exited', 'timeout', 'denied', 'cancelled', 'error', 'limit'}
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
        invalid = json.dumps(arguments, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        return '无效命令参数', hashlib.sha256(invalid.encode('utf-8')).hexdigest()
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
    # Match the actual request, not its redacted display (different credentials must not alias).
    identity = json.dumps([cwd, argv], ensure_ascii=False, separators=(',', ':'))
    command = ' '.join(json.dumps(arg, ensure_ascii=False) if any(c.isspace() for c in arg) else arg
                       for arg in parts)
    return _clip(command, 520), hashlib.sha256(identity.encode('utf-8', errors='backslashreplace')).hexdigest()


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


def _operation(tool, arguments, workspace):
    """Persist identity and a safe preview, never file content or executable arguments."""
    subject = _clean(arguments.get('path'), 520)
    if tool == 'run_command':
        subject, identity = _command(arguments, workspace)
        identity = [identity, arguments.get('purpose', 'task')]
    else:
        identity = arguments
    digest = hashlib.sha256(json.dumps([tool, identity], ensure_ascii=True,
                                      sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'id': digest, 'tool': _clean(tool, 48), 'subject': subject,
            'path': _clean(arguments.get('path'), 1024).replace('\\', '/'), 'expected_sha256': None}


def _merge_rows(older, newer, key, limit):
    rows = {row[key]: deepcopy(row) for row in older}
    for row in newer:
        rows.pop(row[key], None)
        rows[row[key]] = deepcopy(row)
    return list(rows.values())[-limit:]


def _validate(state, run_dir, workspace):
    fields = {'version', 'run_id', 'workspace', 'task', 'parent_run', 'status', 'phase', 'uncertain',
              'plan', 'revision', 'edits', 'checks', 'check_statuses', 'omitted_checks', 'observations',
              'inflight', 'unresolved', 'failed_writes'}
    if not isinstance(state, dict) or state.keys() != fields:
        raise ValueError('任务记录字段不完整或版本不受支持。')
    if (type(state['version']) is not int or state['version'] != 2
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
                and _integer(row[0]) and row[0] <= revision and (row[1] is None or type(row[1]) is bool)
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
            'id': _hash, 'argv_id': lambda item: item == '' or _hash(item),
            'command': lambda item: _text(item, 520),
            'cwd': lambda item: _text(item, 320),
            'status': lambda item: item in CHECK_STATES,
            'returncode': lambda item: item is None or type(item) is int,
            'detail': lambda item: _text(item, 560),
            'revision': lambda item: _integer(item) and item <= revision,
    }) or not _valid_rows(state['observations'], OBSERVATION_LIMIT, {
            'tool': lambda item: _text(item, 48), 'subject': lambda item: _text(item, 520),
            'status': lambda item: _text(item, 48), 'detail': lambda item: _text(item, 560),
    }):
        raise ValueError('任务记录包含无效的计划、编辑或观察。')
    operation_schema = {'id': _hash, 'tool': lambda item: _text(item, 48),
                        'subject': lambda item: _text(item, 520), 'path': lambda item: _text(item, 1024),
                        'expected_sha256': lambda item: item is None or _hash(item)}
    if (not _valid_rows(state['unresolved'], OPERATION_LIMIT, operation_schema)
            or (state['inflight'] is not None
                and not _valid_rows([state['inflight']], 1, operation_schema))):
        raise ValueError('任务记录的中断操作无效。')
    if (not isinstance(state['failed_writes'], list) or len(state['failed_writes']) > EDIT_LIMIT
            or not all(_text(path, 1024) for path in state['failed_writes'])):
        raise ValueError('未完成文件修改记录无效。')
    for row in state['checks']:
        terminal = row['status'] in ('exited', 'timeout')
        if terminal != (type(row['returncode']) is int):
            raise ValueError('任务检查的执行状态与退出码不一致。')
        passed = (row['status'] == 'exited' and row['returncode'] == 0) if terminal else None
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
            'version': 2, 'run_id': self._run_dir.name,
            'workspace': os.path.normcase(str(workspace)),
            'task': _clean(task, TASK_LIMIT), 'parent_run': parent,
            'status': 'running', 'phase': 'ready', 'uncertain': False, 'plan': [], 'revision': 0,
            'edits': [], 'checks': [], 'check_statuses': {}, 'omitted_checks': False, 'observations': [],
            'inflight': None, 'unresolved': [], 'failed_writes': [],
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
        state['failed_writes'] = deepcopy(source['failed_writes'])
        state['edits'] = deepcopy(source['edits'])
        state['observations'] = deepcopy(source['observations'])
        state['unresolved'] = deepcopy(source['unresolved'])
        state['uncertain'] = source['uncertain']
        if source['phase'] == 'executing':
            if source['inflight'] is None:
                # Legacy checkpoints did not save which operation was interrupted.
                state['uncertain'] = True
            else:
                pending = _merge_rows(state['unresolved'], [source['inflight']], 'id', OPERATION_LIMIT + 1)
                if len(pending) > OPERATION_LIMIT:
                    state['uncertain'] = True
                state['unresolved'] = pending[-OPERATION_LIMIT:]
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
        open_steps = {item['text'] for item in self._state['plan'] if item['status'] != 'completed'}
        if open_steps - {item['text'] for item in plan}:
            raise ValueError('仍有未完成步骤，不能直接删除；请先完成或说明阻碍。')
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
        status = result.get('status', 'error')
        code = result.get('returncode')
        if status not in ('exited', 'timeout') or type(code) is not int:
            status = status if status in CHECK_STATES - {'exited', 'timeout'} else 'error'
            code = None
        passed = (status == 'exited' and code == 0) if code is not None else None
        argv_id = (hashlib.sha256(json.dumps(arguments['argv'], ensure_ascii=True,
                                            separators=(',', ':')).encode('utf-8')).hexdigest()
                   if isinstance(arguments.get('argv'), list) else '')
        if argv_id:
            malformed = {row['id'] for row in checks if row['status'] == 'error' and not row['argv_id']}
            for identity in malformed:
                statuses.pop(identity, None)
            checks[:] = [row for row in checks if row['id'] not in malformed]
        if passed:
            # A corrected cwd may replace a preflight error for the same argv.
            # Refusal, interrupted execution and observed failures are never waived this way.
            replaced = {row['id'] for row in checks
                        if row['status'] == 'error' and row['argv_id'] == argv_id}
            for identity in replaced:
                statuses.pop(identity, None)
            checks[:] = [row for row in checks if row['id'] not in replaced]
        statuses[check_id] = [revision, passed]
        checks[:] = [item for item in checks if item['id'] != check_id]
        checks.append({
            'id': check_id, 'argv_id': argv_id, 'command': command, 'cwd': _clean(arguments.get('cwd', '.'), 320),
            'status': status, 'returncode': code,
            'detail': detail, 'revision': revision,
        })
        del checks[:-CHECK_LIMIT]

    def begin(self, tool, arguments):
        """Save intent before execution, so a crash retains the exact pending operation."""
        if not self._writable:
            raise ValueError('上次运行的任务记录仅供读取。')
        self._state['inflight'] = _operation(tool, arguments, self._state['workspace'])
        if tool == 'run_command' and arguments.get('purpose') == 'check':
            command, check_id = _command(arguments, self._state['workspace'])
            if check_id:
                self._record_check(check_id, command, arguments, {'status': 'pending'}, '尚未取得执行结果。')
        self.save(phase='executing')

    def write_intent(self, path, digest):
        operation = self._state['inflight']
        if operation and operation['tool'] in ('write_file', 'replace_text'):
            operation.update(path=path, expected_sha256=digest)
            self.save(phase='executing')

    def confirm_recovery(self):
        """Called only after an explicit user confirmation, never exposed as a model tool."""
        self._state['uncertain'] = False
        self._state['unresolved'] = []
        self._state['observations'].append({'tool': 'user_confirmation', 'subject': '核实中断操作',
            'status': 'confirmed', 'detail': '用户确认已检查现场；未执行或失败的检查仍需处理。'})
        del self._state['observations'][:-OBSERVATION_LIMIT]

    def observe(self, tool, arguments, result):
        """Store metadata and bounded output, excluding file contents and edit payloads."""
        if not self._writable:
            raise ValueError('上次运行的任务记录仅供读取。')
        operation = _operation(tool, arguments, self._state['workspace'])
        successful = (result.get('status') == 'exited' and result.get('returncode') == 0
                      if tool == 'run_command' else result.get('ok') is True)
        if successful:
            self._state['unresolved'] = [row for row in self._state['unresolved']
                                         if row['id'] != operation['id']]
        if tool == 'read_file' and result.get('ok') is True and _hash(result.get('sha256')):
            path = _clean(result.get('path') or arguments.get('path'), 1024).replace('\\', '/')
            recovered = [row for row in self._state['unresolved']
                         if row['tool'] in ('write_file', 'replace_text')
                         and os.path.normcase(row['path']) == os.path.normcase(path)
                         and row['expected_sha256'] == result['sha256']]
            if recovered:
                self._state['unresolved'] = [row for row in self._state['unresolved'] if row not in recovered]
                self._state['edits'] = _merge_rows(self._state['edits'],
                    [{'path': path, 'sha256': result['sha256']}], 'path', EDIT_LIMIT)
                self._state['revision'] += 1
                self._state['failed_writes'] = [item for item in self._state['failed_writes']
                    if os.path.normcase(item) != os.path.normcase(path)]
        self._state['inflight'] = None
        if tool in ('write_file', 'replace_text') and (not successful or result.get('changed') is True):
            path = _clean(result.get('path') or arguments.get('path'), 1024).replace('\\', '/')
            pending = self._state['failed_writes']
            pending[:] = [item for item in pending if os.path.normcase(item) != os.path.normcase(path)]
            if not successful:
                pending.append(path)
                if len(pending) > EDIT_LIMIT:
                    self._state['uncertain'] = True
                    del pending[:-EDIT_LIMIT]
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
            if arguments.get('purpose') == 'check' and check_id:
                self._record_check(check_id, subject, arguments, result, detail)
            elif type(result.get('returncode')) is int:
                self._state['revision'] += 1
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
        if ready and any(rev == revision and passed is False for rev, passed in statuses):
            verification = 'failed'
        elif any(passed is None for _, passed in statuses):
            verification = 'blocked'
        elif state['omitted_checks'] or (statuses and (not ready or any(rev != revision for rev, _ in statuses))):
            verification = 'stale'
        elif statuses:
            verification = 'passed'
        else:
            verification = 'not_run'
        unresolved = deepcopy(state['unresolved'])
        if not ready and state['inflight']:
            unresolved = _merge_rows(unresolved, [state['inflight']], 'id', OPERATION_LIMIT)
        return {**deepcopy({key: state[key] for key in (
            'run_id', 'task', 'parent_run', 'status', 'phase', 'plan', 'edits', 'observations', 'failed_writes',
        )}), 'unresolved': unresolved, 'uncertain': state['uncertain'] or not ready or bool(unresolved),
                'verification': {'status': verification, 'checks': checks}}

    def completion_blockers(self):
        """Only observed obligations gate completion; an ordinary answer needs no test."""
        data = self.export()
        blockers = []
        if data['uncertain']:
            blockers.append('中断操作尚未取得对应结果；无关命令不能代替核实。')
        verification = data['verification']['status']
        if verification in ('failed', 'stale', 'blocked'):
            blockers.append({'failed': '已申请的检查仍有失败。',
                             'stale': '修改或恢复后，原检查尚未重新验证。',
                             'blocked': '已申请的检查被拒绝、未能启动或尚未执行。'}[verification])
        if data['failed_writes']:
            blockers.append('仍有未完成的文件修改：' + '、'.join(data['failed_writes'][:3]))
        if any(step['status'] != 'completed' for step in data['plan']):
            blockers.append('计划中仍有待处理或进行中的步骤。')
        return blockers

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
                 'stale': '已有检查需要重新确认', 'not_run': '尚无验证结果', 'blocked': '检查尚未执行或被阻止'}
        lines.extend((f"运行状态 {data['status']}，验证状态 {state[verification['status']]}",
                      '任务目标 ' + _clip(data['task'], min(1000, budget * 2 // 5))))
        if data['failed_writes']:
            lines.append('待完成文件修改 ' + '、'.join(data['failed_writes'][:3]))
        for operation in data['unresolved'][:3]:
            lines.append('待核实操作 ' + operation['tool'] + ' ' + _clip(operation['subject'], 240))
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


def load_task(runs_root, run_dir, workspace, *, _visited=()):
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
    if name in _visited or len(_visited) >= 64:
        raise ValueError('旧任务的续跑记录存在循环或层数过多。')
    parts = (name, STATE_FILE)
    with _pinned(root, parts):
        path = _target(root, parts)
        raw = _read_bytes(path, limit=STATE_LIMIT)
        if raw is None:
            raise ValueError('此运行没有可续跑的任务记录。')
        try:
            state = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_keys)
            legacy = isinstance(state, dict) and type(state.get('version')) is int and state['version'] == 1
            if legacy:
                if any(key in state for key in ('inflight', 'unresolved', 'failed_writes')):
                    raise ValueError('旧任务包含不受支持的字段。')
                state.setdefault('uncertain', False)
                state.update(version=2, inflight=None, unresolved=[], failed_writes=[])
                for check in state.get('checks', []):
                    check['argv_id'] = ''
            _validate(state, path.parent, workspace)
            if legacy and state['parent_run']:
                try:
                    parent = load_task(root, state['parent_run'], workspace, _visited=(*_visited, name))._state
                except (OSError, ValueError):
                    state['uncertain'] = True
                    state['observations'] = (state['observations'] + [{
                        'tool': 'history', 'subject': state['parent_run'], 'status': 'unavailable',
                        'detail': '上游记录缺失或无法读取；本轮记录已保留，须核实现场后继续。'}])[-OBSERVATION_LIMIT:]
                else:
                    state['uncertain'] = state['uncertain'] or parent['uncertain']
                    state['edits'] = _merge_rows(parent['edits'], state['edits'], 'path', EDIT_LIMIT)
                    state['observations'] = (deepcopy(parent['observations']) + state['observations'])[-OBSERVATION_LIMIT:]
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError('续跑记录损坏、版本不受支持或属于其他工作区。') from exc
        snapshot = TaskJournal(path.parent, workspace, state['task'], state['parent_run'])
        snapshot._state = state
        snapshot._writable = False
        return snapshot
