"""Optional ACP agent runner. Protocol settlement is not task verification."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

from acp_transport import ACPProcess, ACPError, ACPCancelled
from agent_task_state import TaskJournal, load_task, _clean
from harness_settings import checked_executable, child_environment, initialize, supports_resume, launch_directory
from project_agent import _atomic_json, _workspace, _target, _pinned

GATE = '''export const name = 'freeai-approval-gate';
export const inject = ['tools'];
export function apply(ctx) {
  ctx.on('tools/pre-execute', async (_exec, next) => {
    const decision = await next();
    return decision.kind === 'allow'
      ? {kind: 'ask', reason: 'Free AI requires confirmation for every tool operation.'}
      : decision;
  }, {prepend: true, global: true});
  ctx.provide('freeaiApprovalGateReady', true);
}
'''
META_FILE = 'harness.json'
NOTICE = '本轮已结束，执行结果待验收。Harness 的结束信号不等于检查通过；此模式不提供 Free AI 文件撤回。'


def load_harness_run(runs_root, run_dir, workspace):
    """Load only a direct child record and verify workspace before using its session."""
    root, workspace = _workspace(runs_root), _workspace(workspace)
    directory = Path(run_dir)
    if not directory.is_absolute():
        directory = root / directory
    if directory.parent != root:
        raise ValueError('组件任务记录不在当前记录目录。')
    with _pinned(root, (directory.name, META_FILE)):
        path = _target(root, (directory.name, META_FILE))
        if path.stat().st_size > 32768:
            raise ValueError('组件任务记录过大。')
        data = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or data.get('version') != 1 or data.get('engine') != 'harness'
            or data.get('workspace') != os.path.normcase(str(workspace))
            or not isinstance(data.get('home_id'), str) or len(data['home_id']) != 32
            or any(c not in '0123456789abcdef' for c in data['home_id'])
            or not isinstance(data.get('session_id'), str) or not 0 < len(data['session_id']) < 256
            or not isinstance(data.get('route'), str) or data.get('permission_mode') not in ('standard', 'automatic')):
        raise ValueError('组件任务记录无效，不能自动创建替代会话。')
    return data


def validate_selection(executable, permission_mode, allow_write):
    if permission_mode not in ('standard', 'automatic') or type(allow_write) is not bool or not allow_write:
        raise ValueError('Harness 暂不支持“只看项目”或关闭文件修改。请使用内置执行方式；草稿已保留。')
    return checked_executable(executable)


@contextmanager
def model_connection(model, context, cancel_event):
    """Reuse the selected API or hold the existing local inference lock for the whole task."""
    from local_project_model import LocalProjectModel, _loopback_api_url
    if not isinstance(model, LocalProjectModel):
        yield {'url': model.base_url, 'model': model.model, 'key': model._api_key,
               'max_tokens': model.max_output_tokens, 'identity': ['api', model.base_url, model.model]}
        return
    runtime = model._runtime
    if runtime is None:
        from qwen_chat import runtime as shared_runtime
        runtime = shared_runtime()
    if not runtime._request_lock.acquire(blocking=False):
        raise ValueError('另一个本地任务正在进行，请先停止或等待完成。')
    try:
        url = runtime.ensure_server(context, False, cancel_event, model.model_id)
        yield {'url': _loopback_api_url(url), 'model': runtime._alias(model.model_id),
               'key': runtime._token, 'max_tokens': model.max_output_tokens,
               'identity': ['local', model.model_id]}
    finally:
        try:
            if cancel_event.is_set() and model._runtime is None:
                runtime.shutdown()
        finally:
            runtime._request_lock.release()


def write_profile(home, connection, context, permission_mode):
    """An isolated composition, no project plugins or plaintext key in the patch."""
    home.mkdir(parents=True, exist_ok=True)
    _workspace(home)
    patch = [
        {'id': 'llm-pi-ai', 'config': {'providers': {'freeai': {
            'api': 'openai-completions', 'apiKeyEnv': 'FREEAI_DSH_MODEL_KEY',
            'baseURL': connection['url'], 'retryPolicy': {'mode': 'normal', 'maxRetries': 0},
            'models': [{'id': connection['model'], 'contextWindow': context,
                        'maxTokens': connection['max_tokens']}],
        }}}},
        {'id': 'acp', 'config': {'provider': 'freeai', 'model': connection['model']},
         'inject': ['acpAppStartup', 'freeaiApprovalGateReady'] if permission_mode == 'standard' else ['acpAppStartup']},
        {'id': 'approval', 'config': {'policy': 'ask' if permission_mode == 'standard' else 'never'}},
        {'id': 'permission', 'config': {'presets': {'freeai': {
            'sandbox': 'danger-full-access', 'approval': 'ask' if permission_mode == 'standard' else 'never'}},
            'defaultPreset': 'freeai'}},
    ]
    if permission_mode == 'standard':
        from project_agent import _atomic_bytes
        _atomic_bytes(_target(home, ('gate.mjs',)), GATE.encode('utf-8'))
        patch.append({'insert': [{'id': 'freeai-approval-gate', 'name': './gate.mjs', 'required': True}]})
    path = _target(home, ('freeai.patch.yml',))
    _atomic_json(path, patch)  # JSON is valid YAML; paths/model names cannot inject YAML.
    return path


class HarnessAgent:
    def __init__(self, workspace, model, *, executable, runs_root, approve_command=None,
                 on_event=None, cancel_event=None, permission_mode='standard', allow_write=True,
                 context=16384, transport_factory=ACPProcess):
        self.workspace = _workspace(workspace)
        self.executable = validate_selection(executable, permission_mode, allow_write)
        self.model, self.context = model, context
        self.runs_root = Path(runs_root).absolute()
        self.approve = approve_command
        self.on_event = on_event or (lambda event: None)
        self.cancel = cancel_event if cancel_event is not None else threading.Event()
        self.permission_mode = permission_mode
        self.transport_factory = transport_factory
        self.run_dir = None
        self.changes = []
        self.journal = None
        self.metadata = None
        self.tools = {}
        self.answer = ''
        self.secret = getattr(model, '_api_key', '')
        self.denied = False
        self.failed_tools = set()
        self.closed = False

    def clean(self, value, limit=12000):
        text = str(value)
        if self.secret:
            text = text.replace(self.secret, '[已隐藏凭据]')
        return _clean(text, limit)

    def event(self, kind, message, **details):
        event = {'type': kind, 'message': self.clean(message), **details}
        encoded = json.dumps(event, ensure_ascii=False)
        if self.secret:
            encoded = encoded.replace(self.secret, '[已隐藏凭据]')
        with _pinned(self.runs_root, (self.run_dir.name, 'events.jsonl')):
            with _target(self.runs_root, (self.run_dir.name, 'events.jsonl')).open('a', encoding='utf-8') as stream:
                stream.write(encoded + '\n')
        self.on_event(json.loads(encoded))

    def checkpoint(self, status='running', phase='ready'):
        self.journal.save(status=status, phase=phase)
        with _pinned(self.runs_root, (self.run_dir.name, META_FILE)):
            _atomic_json(_target(self.runs_root, (self.run_dir.name, META_FILE)), self.metadata)

    def notification(self, method, params):
        if method != 'session/update' or params.get('sessionId') != self.metadata.get('session_id'):
            return
        update = params.get('update', {})
        kind = update.get('sessionUpdate')
        if kind == 'agent_message_chunk':
            content = update.get('content', {})
            if content.get('type') == 'text':
                chunk = str(content.get('text', ''))[:24000]
                self.answer = (self.answer + chunk)[-64000:]
                # Redact the joined final answer: a credential can span multiple chunks.
                self.event('harness_text', '正在接收组件回复。')
        elif kind in ('tool_call', 'tool_call_update'):
            identity = update.get('toolCallId')
            if not isinstance(identity, str) or len(identity) > 256:
                raise ValueError('组件返回了无效的操作编号。')
            if identity not in self.tools and len(self.tools) >= 512:
                raise ValueError('本轮组件操作过多，请停止并继续任务。')
            item = self.tools.setdefault(identity, {})
            item.update(update)
            title, status = self.clean(item.get('title', '组件操作'), 500), item.get('status', 'pending')
            if status in ('completed', 'failed'):
                if status == 'failed':
                    self.failed_tools.add(identity)
                content = '\n'.join(row.get('content', {}).get('text', '') for row in item.get('content', [])
                    if row.get('type') == 'content' and row.get('content', {}).get('type') == 'text')
                result = {'ok': status == 'completed', 'status': status, 'message': self.clean(content)}
                self.journal.observe('harness_tool', {'path': title}, result)
                self.journal.begin('harness_prompt', {'sessionId': self.metadata['session_id']})
                self.checkpoint(phase='executing')
                self.event('tool_result', title, tool='harness_tool', result=result)
            else:
                self.event('tool', title, tool='harness_tool')
        # Thoughts are not shown or persisted. ACP does not expose trustworthy test exit codes/plans.

    def request(self, method, params):
        if method != 'session/request_permission':
            raise NotImplementedError(method)
        cancelled = {'outcome': {'outcome': 'cancelled'}}
        if self.cancel.is_set() or params.get('sessionId') != self.metadata.get('session_id'):
            return cancelled
        call = params.get('toolCall', {})
        identity = call.get('toolCallId')
        item = {**self.tools.get(identity, {}), **call}
        options = params.get('options', [])
        allow = next((o.get('optionId') for o in options if o.get('kind') == 'allow_once'), None)
        reject = next((o.get('optionId') for o in options if o.get('kind') == 'reject_once'), None)
        # Never approve a request whose operation is missing from the ordered event stream.
        known = identity in self.tools and isinstance(item.get('title'), str)
        allowed = False
        if known and allow:
            if self.permission_mode == 'automatic':
                allowed = True
            elif self.approve:
                preview = self.clean(json.dumps({key: item[key] for key in
                    ('title', 'kind', 'rawInput', 'locations', 'content') if key in item}, ensure_ascii=False), 24000)
                allowed = self.approve({'engine': 'harness', 'executable': 'DeepSeek Harness',
                    'cwd': str(self.workspace), 'timeout': 1800, 'argv': [], 'operation': preview}) is True
        if self.cancel.is_set():
            return cancelled
        self.denied |= not allowed
        self.event('instructions', '组件操作已批准' if allowed else '组件操作已拒绝')
        choice = allow if allowed else reject
        return {'outcome': {'outcome': 'selected', 'optionId': choice}} if choice else cancelled

    def run(self, task, history=None, resume_from=None, **unused):
        if self.run_dir is not None or not isinstance(task, str) or not task.strip():
            raise ValueError('请输入任务，每轮使用新的执行器。')
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.runs_root = _workspace(self.runs_root)
        if self.runs_root == self.workspace or self.runs_root in self.workspace.parents:
            raise ValueError('运行记录不能作为项目目录。')
        previous = load_harness_run(self.runs_root, resume_from, self.workspace) if resume_from else None
        self.run_dir = self.runs_root / uuid.uuid4().hex
        self.run_dir.mkdir()
        self.journal = TaskJournal(self.run_dir, self.workspace, self.clean(task, 8192),
            parent_run=Path(resume_from).name if previous else None)
        if previous:
            self.journal.inherit(load_task(self.runs_root, resume_from, self.workspace))
        self.metadata = deepcopy(previous) if previous else {'version': 1, 'engine': 'harness',
            'workspace': os.path.normcase(str(self.workspace)), 'home_id': self.run_dir.name,
            'session_id': '', 'route': '', 'permission_mode': self.permission_mode}
        self.checkpoint()
        self.event('start', 'DeepSeek Harness 项目任务', run_dir=str(self.run_dir),
            engine='harness', task_state=self.journal.export(), permission_mode=self.permission_mode)
        status, answer = 'failed', '组件未完成任务，请查看记录。'
        home = self.runs_root.parent / 'harness' / 'sessions' / self.metadata['home_id']
        try:
            if previous and previous['permission_mode'] != self.permission_mode:
                raise ValueError('续跑需保留原任务权限；修改权限后请发送新任务。')
            with model_connection(self.model, self.context, self.cancel) as connection:
                self.secret = connection['key']
                route = hashlib.sha256(json.dumps(connection['identity'], ensure_ascii=True).encode()).hexdigest()
                if previous and previous['route'] != route:
                    raise ValueError('续跑需使用原来的模型和服务地址；切换模型后请发送新任务。')
                self.metadata['route'] = route
                patch = write_profile(home, connection, self.context, self.permission_mode)
                env = child_environment(home, {'FREEAI_DSH_MODEL_KEY': self.secret,
                    'DSH_PERMISSION_MODE': 'danger-full-access'})
                with self.transport_factory([self.executable, '--profile', 'acp', '--patch', str(patch)],
                        cwd=launch_directory(home), env=env, on_notification=self.notification, on_request=self.request) as client:
                    initialized = initialize(client, self.cancel)
                    if not supports_resume(initialized):
                        raise ValueError('此组件不支持会话恢复，请更新组件或使用内置执行。')
                    params = {'cwd': str(self.workspace), 'mcpServers': []}
                    if previous:
                        params['sessionId'] = previous['session_id']
                    session = client.request('session/resume' if previous else 'session/new', params,
                                             timeout=45, cancel_event=self.cancel)
                    if not previous:
                        identity = session.get('sessionId')
                        if not isinstance(identity, str) or not 0 < len(identity) < 256:
                            raise ValueError('组件没有返回有效会话编号。')
                        self.metadata['session_id'] = identity
                    self.checkpoint()
                    session_id = self.metadata['session_id']
                    self.event('resume' if previous else 'instructions',
                        '已恢复原组件会话及 Free AI 记录。' if previous else '已建立组件会话。')
                    prompt = self.clean(task, 16000) + '\n\n请检查执行结果并如实说明未完成事项。不要把结束回答等同于测试通过。'
                    if previous:
                        prompt += '\nFree AI 上轮进度：\n' + self.journal.handoff(budget=1800)
                    elif history:
                        recent = [row for row in history[-6:] if row.get('role') in ('user', 'assistant')]
                        prompt += '\n之前的讨论（仅供参考，当前任务优先）：\n' + self.clean(json.dumps(recent, ensure_ascii=False), 6000)
                    self.journal.begin('harness_prompt', {'sessionId': session_id})
                    try:
                        result = client.request('session/prompt', {'sessionId': session_id,
                            'prompt': [{'type': 'text', 'text': prompt}]}, timeout=1800,
                            cancel_event=self.cancel, cancel_params={'sessionId': session_id})
                    except ACPCancelled:
                        # A cooperative cancel settles the prompt; close flushes its persistent state.
                        try:
                            client.request('session/close', {'sessionId': session_id}, timeout=10)
                            self.closed = True
                        except ACPError:
                            pass
                        raise
                    stop = result.get('stopReason')
                    status = 'stopped' if stop == 'cancelled' else 'limit' if stop in ('max_tokens', 'max_turn_requests') else 'review'
                    if stop not in ('cancelled', 'max_tokens', 'max_turn_requests', 'end_turn'):
                        status = 'unfinished'
                    if self.denied or self.failed_tools or any(t.get('status') not in ('completed', 'failed') for t in self.tools.values()):
                        status = 'unfinished' if status == 'review' else status
                    self.journal.observe('harness_prompt', {'sessionId': session_id},
                                         {'ok': stop == 'end_turn', 'status': str(stop), 'message': NOTICE})
                    answer = self.answer or '组件未返回可显示的回答。'
                    answer += '\n\n' + (NOTICE if status == 'review' else '任务尚未完成，可查看执行记录后继续。')
                    client.request('session/close', {'sessionId': session_id}, timeout=15)
                    self.closed = True
        except ACPCancelled:
            status, answer = 'stopped', (self.answer + '\n\n已停止组件任务，已产生的改动保留。').strip()
        except (ValueError, ACPError) as error:
            status = 'failed'
            answer = self.clean(str(error)) if isinstance(error, ValueError) else '组件连接、会话恢复或执行失败。请检查组件连接；不会自动另建会话或切换执行器。'
        except Exception:
            status = 'failed'
            answer = '组件任务失败，已有操作可能已生效，请检查文件与运行记录。'
        answer = self.clean(answer, 72000)
        self.checkpoint(status='unfinished' if status == 'review' else status,
                        phase='ready' if self.closed and status != 'stopped' else self.journal.export()['phase'])
        result = {'status': status, 'engine': 'harness', 'answer': answer, 'run_dir': str(self.run_dir),
                  'changes': [], 'episode_id': None, 'permission_mode': self.permission_mode,
                  'continuation_available': bool(self.metadata['session_id']), 'task_state': self.journal.export()}
        self.event('finish', '组件本轮结束', status=status)
        return result
