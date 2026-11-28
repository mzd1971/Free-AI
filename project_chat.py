"""Assistant and project work embedded in the conversation and composer."""
import json
import logging
import os
import sqlite3
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from uuid import UUID

from app_state import CONTEXTS, PROJECT_PERMISSION_MODES
from keyboard_shortcuts import shortcut_hint
from paths import ARTIFACTS
from project_checks import MAX_REPAIR_ATTEMPTS, load_project_check, save_project_check
from ui_kit import COLORS as C, FlatButton, label, ui_font

TITLES = {'remember_memory': '保存长期记忆', 'list_files': '查看项目', 'search_text': '搜索项目',
          'read_file': '读取文件', 'write_file': '写入文件', 'replace_text': '修改文件',
          'run_command': '运行命令', 'update_plan': '整理计划',
          'web_search': '搜索公开网页', 'web_fetch': '读取公开网页',
          'calculate': '计算', 'check_json': '检查 JSON', 'current_time': '读取系统时间',
          'summarize_csv': '统计 CSV'}
WEB_SOURCE_LABELS = {'search': '搜索结果', 'page': '已读取网页'}
STATES = {'running': '正在执行项目任务', 'executing': '正在执行项目任务', 'checking': '正在验证任务结果',
          'completed': '本轮已结束', 'stopped': '已停止', 'limit': '达到本轮上限', 'failed': '未完成',
          'unfinished': '仍需处理', 'needs_work': '仍需处理', 'interrupted': '上次任务中断',
          'review': '本轮结束 · 待验收'}
ACTIVE_STATES = {'running', 'executing', 'checking'}
CONTINUABLE_STATES = {'stopped', 'limit', 'failed', 'unfinished', 'needs_work', 'interrupted', 'review'}
COMMAND_STATES = {'denied': '已拒绝', 'cancelled': '已停止', 'timeout': '已超时',
                  'pending': '尚未执行', 'error': '未能执行', 'limit': '尚未执行'}
PLAN_STATES = {'completed': '已完成', 'in_progress': '进行中', 'pending': '待处理'}
VERIFICATION_STATES = {'passed': '已通过', 'failed': '未通过', 'stale': '需要重新验证', 'not_run': '尚未验证', 'blocked': '检查尚未执行'}
PERMISSION_LABELS = {'standard': '逐条确认命令', 'readonly': '只看项目', 'automatic': '自动执行命令'}
PERMISSION_HINTS = {
    'standard': '命令逐条确认',
    'readonly': '可查看项目文件，文件修改和命令执行关闭',
    'automatic': '命令以本机账户自动运行，可访问项目目录之外的文件和网络',
}
CONTINUE_TASK = '继续上次未完成的任务，先检查当前文件和执行结果。'
INTERRUPTED_ANSWER = '上次项目任务中断，可以从这张任务卡继续。'
ASSISTANT_CONTINUE_TASK = '继续上次未完成的任务，先检查已有的工具结果。'


def assistant_workspace(identity, *, create=False):
    from project_agent import _pinned, _workspace

    if not isinstance(identity, str):
        raise ValueError('智能助手会话编号无效。')
    try:
        session = UUID(hex=identity)
    except ValueError as error:
        raise ValueError('智能助手会话编号无效。') from error
    if session.hex != identity or session.version != 4:
        raise ValueError('智能助手会话编号无效。')
    root = _workspace(ARTIFACTS)
    parts = ('assistant_workspaces', identity)
    if create:
        with _pinned(root, parts, directory=True, create=lambda path, _: path.mkdir(exist_ok=True)):
            return _workspace(root.joinpath(*parts))
    return _workspace(root.joinpath(*parts))


def is_assistant_workspace(folder):
    internal = ARTIFACTS / 'assistant_workspaces'
    selected = Path(folder)
    return selected.absolute().is_relative_to(internal.absolute()) or selected.resolve().is_relative_to(internal.resolve())


def apply_task_state(result, state):
    if not isinstance(state, dict):
        return result
    return {**result, 'task_state': state}


def acceptance_summary(result):
    check = result.get('acceptance') or {}
    if not check:
        return ''
    if check.get('stale'):
        return '此前的手动检查需重跑确认'
    return {'passed': '这条检查已通过（对应当时的项目文件）', 'failed': '这条检查未通过，可继续修复',
            'stopped': '检查已停止', 'timeout': '检查超时', 'error': '检查未能执行',
            'stale': '文件或覆盖范围发生变化，需重新检查'}.get(check.get('status'), '检查尚未确认')


def recorded_run_changes(run_dir, workspace):
    """Recover this card's file list from its own rollback manifest."""
    from agent_review import _group_changes
    from project_agent import _load_changes, _path_parts, _pinned, _target, _workspace

    try:
        root = _workspace(ARTIFACTS / 'project_runs')
        project = _workspace(workspace)
        run = Path(run_dir)
        if run.parent != root:
            return []
        parts = _path_parts(run.name)
        with _pinned(root, parts, directory=True):
            manifest = _load_changes(_target(root, parts, directory=True))
            if os.path.normcase(manifest['workspace']) != os.path.normcase(str(project)):
                return []
            return [{'path': change['path']} for change in _group_changes(manifest['changes'], project, root)]
    except (OSError, ValueError, RuntimeError):
        return []


class ActivityCard:
    def __init__(self, controller, workspace, record=None, *, assistant=False):
        self.controller, self.app = controller, controller.app
        self.workspace, self.result, self.assistant_index = workspace, None, None
        self.identity = self.app.session_id
        self.assistant = record['result'].get('capability_mode') == 'assistant' if record else assistant
        self.activity, self.expanded, self.request, self.steps = '', False, None, 0
        self.frame = tk.Frame(self.app.transcript, bg=C['side'], padx=12, pady=8, bd=0, highlightthickness=0)
        self.inner = tk.Frame(self.frame, bg=C['side'])
        self.inner.pack(fill='both', expand=True)
        self.header = tk.Frame(self.inner, bg=C['side'])
        self.header.pack(fill='x')
        self.status = tk.StringVar(value='正在准备任务' if self.assistant else '正在准备项目任务')
        self.status_label = label(self.header, textvariable=self.status, size=11, bold=True, anchor='w', justify='left')
        self.expand_button = FlatButton(self.header, '详情', self.toggle, variant='ghost', height=26, font_size=10,
                                        icon='chevron_down', icon_side='right')
        self.expand_button.pack(side='right', padx=(8, 0))
        self.status_label.pack(side='left', fill='x', expand=True)
        self.progress = label(self.inner, '', size=10, color=C['muted'], anchor='w', justify='left')
        from reasoning_view import ReasoningPanel
        self.reasoning_panel = ReasoningPanel(self.inner, on_resize=self.resize)
        self.resume_button = FlatButton(self.inner, '继续任务', lambda: self.controller.resume(self),
                                        variant='primary', height=30, font_size=10)
        self.recovery_button = FlatButton(self.inner, '核实中断后继续', self.confirm_recovery,
                                          variant='ghost', height=30, font_size=10)
        self.detail = tk.Text(self.inner, bg=C['surface'], fg=C['text'], height=7, width=1,
                              wrap='word', font=ui_font(self.app.root, 10), bd=0, padx=10, pady=8,
                              spacing3=2, state='disabled', highlightbackground=C['surface'])
        self.detail.tag_configure('heading', font=ui_font(self.app.root, 10, bold=True))
        self.approval_box = tk.Frame(self.inner, bg=C['tint'], padx=10, pady=9)
        self.command_label = label(self.approval_box, '', size=10, anchor='w', justify='left')
        self.command_label.pack(fill='x')
        arguments_box = tk.Frame(self.approval_box, bg=C['surface'])
        arguments_box.pack(fill='x', pady=(6, 0))
        self.arguments = tk.Text(arguments_box, height=4, width=1, wrap='word',
            bg=C['surface'], fg=C['text'], bd=0, padx=7, pady=6, font=ui_font(self.app.root, 10), state='disabled')
        scroll = tk.Scrollbar(arguments_box, command=self.arguments.yview)
        scroll.pack(side='right', fill='y')
        self.arguments.configure(yscrollcommand=scroll.set)
        self.arguments.pack(side='left', fill='x', expand=True)
        actions = tk.Frame(self.approval_box, bg=C['tint'])
        actions.pack(fill='x', pady=(7, 0))
        self.approve_button = FlatButton(actions, '执行这条命令', lambda: self.decide(True), variant='primary', height=32, font_size=10)
        self.approve_button.pack(side='left')
        FlatButton(actions, '跳过', lambda: self.decide(False), variant='ghost', height=32, font_size=10).pack(side='left', padx=10)
        self.tools = tk.Frame(self.inner, bg=C['side'])
        self.review_button = FlatButton(self.tools, '查看改动', self.toggle_review, variant='ghost',
                                        height=30, font_size=10, state='disabled')
        self.undo_button = FlatButton(self.tools, '撤回文件修改', self.confirm_undo, variant='ghost', height=30, font_size=10)
        if not self.assistant:
            self.review_button.pack(side='left')
            self.undo_button.pack(side='left', padx=(4, 0))
        FlatButton(self.tools, '打开记录文件夹', self.open_records, variant='ghost', height=30,
                   font_size=10).pack(side='left', padx=(0 if self.assistant else 4, 0))
        self.check_tools = tk.Frame(self.inner, bg=C['side'])
        self.check_button = FlatButton(self.check_tools, '检查项目结果', self.toggle_check,
                                      variant='ghost', height=30, font_size=10)
        self.check_button.pack(side='left')
        self.check_box = tk.Frame(self.inner, bg=C['surface'], padx=9, pady=8)
        self.check_hint = label(self.check_box, '填写项目的测试命令，点击后在本机运行。通过只代表这条检查，不代表所有需求已完成。',
                                size=10, anchor='w', justify='left')
        self.check_hint.pack(fill='x')
        self.check_command = tk.StringVar(value='python -m unittest discover')
        self.check_entry = tk.Entry(self.check_box, textvariable=self.check_command, bg=C['input'],
                                    fg=C['text'], insertbackground=C['text'], font=ui_font(self.app.root, 10))
        self.check_entry.pack(fill='x', pady=7)
        self.run_check_button = FlatButton(self.check_box, '运行这条检查', lambda: self.controller.verify(self),
                                          variant='primary', height=30, font_size=10)
        self.run_check_button.pack(anchor='w')
        self.review_box = tk.Frame(self.inner, bg=C['side'])
        self.review_label = label(self.review_box, '本轮项目文件变化', size=10, color=C['muted'], anchor='w')
        self.review_label.pack(fill='x', pady=(3, 4))
        self.review_text = tk.Text(self.review_box, bg=C['surface'], fg=C['text'], height=10, width=1,
                                   wrap='word', font=ui_font(self.app.root, 10), bd=0, padx=8, pady=6, state='disabled')
        review_scroll = tk.Scrollbar(self.review_box, command=self.review_text.yview)
        review_scroll.pack(side='right', fill='y')
        self.review_text.configure(yscrollcommand=review_scroll.set)
        self.review_text.tag_configure('file', font=ui_font(self.app.root, 10, bold=True))
        self.review_text.tag_configure('metadata', foreground=C['muted'])
        self.review_text.tag_configure('added', foreground=C['success'])
        self.review_text.tag_configure('removed', foreground='#a13243' if self.app.theme == 'light' else '#ff8c9b')
        self.review_text.pack(side='left', fill='x', expand=True)
        self.undo_box = tk.Frame(self.inner, bg=C['tint'], padx=9, pady=8)
        self.undo_label = label(self.undo_box, '恢复文件工具修改前的内容？后来有改动的文件会跳过。命令产生的改动无法自动撤回。',
                               size=10, anchor='w', justify='left')
        self.undo_label.pack(fill='x')
        self.undo_files = tk.Listbox(self.undo_box, height=6, selectmode='multiple', exportselection=False,
            bg=C['surface'], fg=C['text'], selectbackground=C['tint'], selectforeground=C['text'],
            font=ui_font(self.app.root, 10), bd=0)
        self.undo_select_all = FlatButton(self.undo_box, '选择全部列出的文件',
            lambda: self.undo_files.selection_set(0, 'end'), variant='ghost', height=28, font_size=10)
        undo_actions = tk.Frame(self.undo_box, bg=C['tint'])
        undo_actions.pack(fill='x', pady=(7, 0))
        FlatButton(undo_actions, '确认撤回', lambda: self.controller.undo(self), height=30, font_size=10).pack(side='left')
        FlatButton(undo_actions, '取消', self.cancel_undo, variant='ghost', height=30, font_size=10).pack(side='left', padx=8)
        text = self.app.transcript
        text.configure(state='normal')
        text.window_create('end', window=self.frame)
        text.insert('end', '\n')
        text.configure(state='disabled')
        if record:
            self.assistant_index, self.activity = record['assistant_index'], record['activity']
            self.finish(record['result'])
        self.resize()

    def resize(self):
        if not self.frame.winfo_exists() or getattr(self, '_resizing', False):
            return
        self._resizing = True
        try:
            available = max(250, self.app.transcript.winfo_width() - 42)
            width = available if (self.expanded or self.request or self.undo_box.winfo_manager()
                                  or self.reasoning_panel.text and self.reasoning_panel.expanded) else min(available, 420)
            inner_width = width - 2 * int(self.frame.cget('padx'))
            self.status_label.configure(wraplength=inner_width - self.expand_button.winfo_reqwidth() - 8)
            self.progress.configure(wraplength=inner_width)
            for widget in (self.command_label, self.undo_label, self.check_hint):
                widget.configure(wraplength=inner_width - 2 * int(widget.master.cget('padx')))
            self.frame.pack_propagate(False)
            self.frame.configure(width=width)
            self.inner.update_idletasks()
            self.frame.configure(height=self.inner.winfo_reqheight() + 2 * int(self.frame.cget('pady')))
        finally:
            self._resizing = False

    def toggle(self):
        self.expanded = not self.expanded
        if self.expanded:
            self.refresh_detail()
            anchor = self.progress if self.progress.winfo_manager() else self.header
            self.detail.pack(fill='x', pady=(8, 0), after=anchor)
            if self.result and self.result.get('run_dir'):
                self.tools.pack(fill='x', pady=(6, 0))
                if not self.assistant:
                    self.check_tools.pack(fill='x')
        else:
            self.detail.pack_forget()
            self.tools.pack_forget()
            self.check_tools.pack_forget()
            self.check_box.pack_forget()
            self.close_review()
            self.cancel_undo()
        self.expand_button.configure(text='收起' if self.expanded else '详情',
                                     icon=None if self.expanded else 'chevron_down')
        self.resize()

    def close_review(self):
        self.review_box.pack_forget()
        self.review_button.configure(text='查看改动')

    def toggle_check(self):
        if self.assistant:
            return
        if self.check_box.winfo_manager():
            self.check_box.pack_forget()
        else:
            if not self.expanded:
                self.toggle()
            self.check_box.pack(fill='x', pady=(6, 0), after=self.check_tools)
            self.check_entry.focus_set()
        self.resize()

    def toggle_review(self):
        if self.assistant:
            return
        if self.review_box.winfo_manager():
            self.close_review()
            self.refresh_detail()
            anchor = self.progress if self.progress.winfo_manager() else self.header
            self.detail.pack(fill='x', pady=(8, 0), after=anchor)
            self.resize()
            return
        if not self.result or not self.result.get('run_dir') or self.controller.active is self:
            return
        if not self.expanded:
            self.toggle()
        from agent_review import review_changes
        try:
            if self.result.get('snapshot_dir') and (self.result.get('snapshot') or {}).get('status') in ('ready', 'partial'):
                from project_snapshot import review_snapshot
                review = review_snapshot(ARTIFACTS / 'project_snapshots', self.result['snapshot_dir'], self.workspace)
            else:
                review = review_changes(ARTIFACTS / 'project_runs', self.result['run_dir'], self.workspace)
        except (OSError, ValueError, RuntimeError):
            review = {'files': [], 'notice': '无法读取这轮文件修改，请检查运行记录。'}
        self.cancel_undo()
        self.detail.pack_forget()
        self.review_text.configure(state='normal')
        self.review_text.delete('1.0', 'end')
        if review['notice']:
            self.review_text.insert('end', review['notice'] + '\n\n', 'metadata')
        for file in review['files']:
            self.render_review_file(file)
        self.review_text.configure(state='disabled')
        self.review_text.yview_moveto(0)
        self.review_box.pack(fill='x', pady=(6, 0), after=self.tools)
        self.review_button.configure(text='关闭改动')
        self.resize()

    def render_review_file(self, file):
        status = {'unchanged': '最终内容与改动前相同', 'unavailable': '缺少可验证的前后版本',
                  'truncated': '超出预览上限'}
        summary = status.get(file['status'], f'+{file["added"]}  -{file["removed"]}')
        self.review_text.insert('end', file['path'] + ' · ' + summary + '\n', 'file')
        for index, line in enumerate(file['diff'].splitlines(keepends=True)):
            if index < 2 or line.startswith('@@'):
                tag = 'metadata'
            else:
                tag = {'+': 'added', '-': 'removed'}.get(line[:1], '')
            self.review_text.insert('end', line, tag)
        self.review_text.insert('end', '\n')

    def refresh_detail(self):
        result = self.result or {}
        state = result.get('task_state') or {}
        sections = []
        if result.get('web_sources'):
            sources = []
            for source in result['web_sources']:
                title = ' '.join(source.get('title', '').split())
                heading = WEB_SOURCE_LABELS[source['kind']]
                sources.append((heading + ' · ' + title if title else heading) + '\n' + source['url'])
            sections.append('本轮联网来源\n' + '\n'.join(sources))
        if state.get('plan'):
            sections.append('执行计划\n' + '\n'.join(
                f'{PLAN_STATES.get(step.get("status"), "待处理")} · {step["text"]}'
                for step in state['plan']))
        if state.get('edits'):
            sections.append('任务累计编辑的文件\n' + '\n'.join(row['path'] for row in state['edits']))
        if state.get('failed_writes'):
            sections.append('待完成的文件修改\n' + '\n'.join(state['failed_writes']))
        if state.get('failed_utilities'):
            sections.append('待完成的工具调用\n' + '\n'.join(
                TITLES.get(tool, tool) for tool in state['failed_utilities']))
        verification = state.get('verification', {})
        summary = '' if self.assistant else self.verification_summary(state)
        if summary:
            checks = []
            for check in verification.get('checks', []):
                outcome = COMMAND_STATES.get(check.get('status'), '')
                if check.get('returncode') is not None:
                    outcome = '退出码 ' + str(check['returncode'])
                if not check.get('current'):
                    outcome += '（需重新验证）'
                checks.append(' · '.join(part for part in (check.get('command'), outcome) if part))
            sections.append('\n'.join([summary, *checks]))
        check = (self.result or {}).get('acceptance')
        if check:
            from project_verification import format_verification_context
            sections.append(acceptance_summary(self.result) + '\n' + format_verification_context(check))
        snapshot = (self.result or {}).get('snapshot')
        if snapshot:
            sections.append('项目文件快照\n' + snapshot.get('notice', ''))
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        for section in sections:
            heading, newline, body = section.partition('\n')
            self.detail.insert('end', heading + newline, 'heading')
            self.detail.insert('end', body + '\n\n')
        self.detail.insert('end', self.activity[-50000:] or '暂无执行记录。')
        self.detail.configure(state='disabled')

    def record_web_result(self, tool, result, outcome):
        lines = [TITLES[tool] + ' · ' + outcome]
        if result.get('query'):
            lines.append('搜索词 · ' + result['query'])
        if result.get('url'):
            lines.append('网页地址 · ' + result['url'])
        if not result.get('ok') and result.get('message'):
            lines.append('原因 · ' + str(result['message'])[:1000])
        elif tool == 'web_search' and result.get('ok'):
            matches = result.get('results', [])
            lines.append(f'找到 {len(matches)} 条搜索结果')
            lines.extend(row['url'] for row in matches)
        elif result.get('truncated'):
            lines.append('网页文字已达到读取上限。')
        if result.get('ok'):
            sources = {(source['url'], source['kind']): source
                       for source in self.result.get('web_sources', [])}
            kind = 'search' if tool == 'web_search' else 'page'
            rows = result.get('results', []) if kind == 'search' else [result]
            for row in rows:
                url = row['url']
                key = (url, kind)
                sources.pop(key, None)
                sources[key] = {'url': url, 'title': row.get('title', ''), 'kind': kind,
                                'fetched_at': result.get('fetched_at', '')}
            self.result = {**self.result, 'web_sources': list(sources.values())[-20:]}
            self.show_progress()
        self.append('\n'.join(lines))

    def append(self, text):
        self.activity = (self.activity + text + '\n').encode('utf-8')[-280000:].decode('utf-8', errors='ignore')
        if self.expanded:
            self.refresh_detail()

    def event(self, event):
        if self.controller.active is not self:
            return
        kind, tool = event.get('type'), event.get('tool', '')
        title = TITLES.get(tool, event.get('message', '正在处理'))
        mode = event.get('permission_mode') or (self.result or {}).get('permission_mode')
        if tool == 'run_command' and mode == 'automatic':
            title = '自动运行命令'
        if kind == 'start':
            if event.get('snapshot_dir'):
                self.result = {**self.result, 'snapshot_dir': event['snapshot_dir']}
            if event.get('engine') == 'harness':
                self.result = {**self.result, 'engine': 'harness'}
            if event.get('run_dir'):
                self.result = {**self.result, 'run_dir': event['run_dir'], 'continuation_available': True}
            if not self.assistant and mode in PROJECT_PERMISSION_MODES:
                self.result = {**self.result, 'permission_mode': mode}
                self.append('权限 · ' + PERMISSION_LABELS[mode])
            self.result = apply_task_state(self.result, event.get('task_state'))
            self.status.set('智能助手正在处理' if self.assistant else STATES['running'])
            self.show_progress()
        elif kind == 'model_start':
            self.status.set('正在分析任务 · 第 ' + str(event.get('round', 1)) + ' 轮')
        elif kind == 'reasoning':
            text = event.get('text')
            if isinstance(text, str) and text:
                self.result = {**(self.result or {}), 'reasoning': text[:24000]}
                self.reasoning_panel.set_text(text)
                self.reasoning_panel.pack(fill='x', pady=(6, 0), after=self.header)
                self.status.set('模型正在思考…')
        elif kind == 'model_end':
            self.reasoning_panel.finish()
            self.status.set('正在整理结果…')
        elif kind == 'progress':
            task_state = event.get('task_state')
            if isinstance(task_state, dict):
                self.result = apply_task_state(self.result, task_state)
                self.show_progress()
                if task_state.get('phase') == 'checking':
                    self.status.set(STATES['checking'])
        elif kind == 'tool_result':
            self.steps += 1
            result = event.get('result', {})
            outcome = ('完成' if result.get('ok') else '未完成')
            if tool == 'run_command':
                outcome = COMMAND_STATES.get(result.get('status'), outcome)
                if result.get('status') == 'exited' and 'returncode' in result:
                    outcome = '退出码 ' + str(result['returncode'])
            self.status.set(f'{title} · {outcome} · {self.steps} 步')
            if tool in ('web_search', 'web_fetch'):
                self.record_web_result(tool, result, outcome)
            else:
                self.append(title)
                if tool in ('calculate', 'check_json', 'current_time', 'summarize_csv'):
                    evidence = result.get('evidence')
                    if isinstance(evidence, dict):
                        self.append(json.dumps(evidence, ensure_ascii=False)[:6000])
                if 'returncode' in result:
                    self.append('退出码：' + str(result['returncode']))
                for key in ('path', 'content', 'stdout', 'stderr', 'message'):
                    if result.get(key):
                        self.append(str(result[key])[:6000])
                if result.get('files'):
                    self.append('\n'.join(result['files']))
                if result.get('matches'):
                    self.append('\n'.join(f'{match["path"]}:{match["line"]}' for match in result['matches']))
                if result.get('instruction_files'):
                    self.append('读取项目规范 · ' + '、'.join(result['instruction_files']))
        elif kind == 'plan':
            steps = event.get('steps', [])
            if all(isinstance(step, str) for step in steps):
                self.append('计划：\n' + '\n'.join(steps))
            self.status.set('已整理执行计划')
        elif kind == 'harness_text':
            self.append(event.get('message', ''))
            self.status.set('组件正在回复')
        elif kind in ('instructions', 'context', 'resume'):
            self.status.set(title)
            files = event.get('files', [])
            self.append(title + ('\n' + '\n'.join(files) if files else ''))
        elif kind != 'finish':
            self.status.set(title + '…')
        if kind in ('start', 'progress', 'tool_result', 'plan', 'instructions', 'context', 'resume', 'harness_text', 'reasoning'):
            self.controller.save_card(self)
        self.resize()
        self.controller.refresh_summary()

    def show_approval(self, request):
        if self.assistant or self.app.generation_cancel.is_set() or self.controller.active is not self:
            request['ready'].set()
            return
        self.request = request
        details = request['details']
        self.approve_button.configure(text='允许此操作' if details.get('engine') == 'harness' else '执行这条命令')
        self.command_label.configure(text=('需要你确认组件操作\n程序：' if details.get('engine') == 'harness'
            else '需要你确认这条命令\n程序：') + str(details['executable']) +
            '\n目录：' + str(details['cwd']) +
            '\n最长 ' + str(details['timeout']) + ' 秒。命令以本机账户运行，可能读写项目外的文件。')
        self.arguments.configure(state='normal')
        self.arguments.delete('1.0', 'end')
        self.arguments.insert('1.0', '操作详情（可滚动和复制）：\n' + details['operation']
                              if details.get('engine') == 'harness'
                              else '完整参数（可滚动和复制）：\n' + json.dumps(details['argv'], ensure_ascii=False))
        self.arguments.configure(state='disabled')
        self.approval_box.pack(fill='x', pady=(8, 0))
        self.status.set('等待确认组件操作' if details.get('engine') == 'harness'
                        else '等待确认验证命令' if details.get('purpose') == 'check' else '等待确认命令')
        self.app.select_page(0)
        self.resize()
        self.app.transcript.see('end')
        self.app.root.after_idle(lambda: self.app.transcript.yview_moveto(1.0))

    def decide(self, allowed):
        request, self.request = self.request, None
        self.approval_box.pack_forget()
        if request:
            request['allowed'] = bool(allowed)
            request['ready'].set()
            if self.app.generation_cancel.is_set():
                self.status.set('正在停止项目任务')
            elif request['details'].get('engine') == 'harness':
                self.status.set('操作已批准，等待执行' if allowed else '操作已拒绝')
            elif allowed and request['details'].get('purpose') == 'check':
                self.status.set(STATES['checking'])
            else:
                self.status.set('命令已批准，等待执行' if allowed else '已跳过命令')
        self.resize()

    def finish(self, result):
        self.decide(False)
        if self.reasoning_panel.text and not result.get('reasoning'):
            result = {**result, 'reasoning': self.reasoning_panel.text}
        self.result = result
        if result.get('reasoning'):
            self.reasoning_panel.set_text(result['reasoning'])
            self.reasoning_panel.pack(fill='x', pady=(6, 0), after=self.header)
        self.reasoning_panel.finish()
        snapshot = result.get('snapshot') or {}
        supported = bool(result.get('snapshot_dir') and snapshot.get('status') in ('ready', 'partial'))
        count = 0 if self.assistant else (
            snapshot.get('count', 0) if supported else len({row['path'] for row in result.get('changes', [])}))
        self.status.set(self.status_text(result.get('status')) + (f' · 修改 {count} 个文件' if count else ''))
        self.show_progress()
        if '[已撤回文件修改]' in self.activity:
            self.status.set('已执行文件撤回 · 展开过程查看结果')
        if self.expanded and result.get('run_dir'):
            self.tools.pack(fill='x', pady=(6, 0))
            if not self.assistant:
                self.check_tools.pack(fill='x')
        legacy = not self.assistant and result.get('engine') != 'harness' and not supported
        self.review_button.configure(state='normal' if result.get('run_dir') and (supported or legacy) else 'disabled')
        self.undo_button.configure(state='normal' if (supported or legacy) and count and '[已撤回文件修改]' not in self.activity else 'disabled')
        if result.get('acceptance', {}).get('argv'):
            self.check_command.set(json.dumps(result['acceptance']['argv'], ensure_ascii=False))
        self.sync_resume()
        self.resize()

    def status_text(self, status):
        if self.assistant and status == 'completed':
            return '已回复'
        return STATES.get(status, '任务结束')

    def show_progress(self):
        result = self.result or {}
        state = result.get('task_state') or {}
        plan = state.get('plan', [])
        lines = []
        if plan:
            complete = sum(step.get('status') == 'completed' for step in plan)
            current = next((step['text'] for step in plan if step.get('status') == 'in_progress'), '')
            lines.append(f'计划进度 {complete}/{len(plan)}' + (' · ' + current if current else ''))
        check_summary = '' if self.assistant else self.verification_summary(state)
        if check_summary:
            lines.append(check_summary)
        if state.get('uncertain') or (result.get('status') == 'interrupted' and state.get('phase') == 'executing'):
            lines.append('上次操作结果仍需核实')
        if result.get('engine') == 'harness':
            lines.append('DeepSeek Harness · 执行结果需验收')
        if result.get('web_sources'):
            counts = []
            for kind, source_label in WEB_SOURCE_LABELS.items():
                count = sum(source['kind'] == kind for source in result['web_sources'])
                if count:
                    counts.append(f'{source_label} {count} 条')
            lines.append('联网来源 · ' + ' · '.join(counts))
        snapshot = result.get('snapshot') or {}
        if snapshot.get('status') in ('ready', 'partial'):
            lines.append('已保存本轮项目文件快照' if snapshot.get('status') == 'ready' else '项目文件快照仅覆盖部分内容，详情可查看范围')
        elif result.get('snapshot_dir'):
            lines.append('结束快照未确认；内置文件工具仍可使用原有修改记录')
        check = acceptance_summary(result)
        if check:
            lines.append(check)
        if result.get('fixed_check'):
            fixed = result['fixed_check']
            summary = acceptance_summary({'acceptance': {**fixed, 'stale': False}})
            if fixed.get('stale'):
                summary += '（历史结果）'
            lines.append('项目固定检查 · ' + summary)
        mode = result.get('permission_mode')
        if not self.assistant and mode in ('readonly', 'automatic'):
            lines.append('权限 · ' + PERMISSION_LABELS[mode])
        self.progress.configure(text='\n'.join(lines))
        if lines:
            self.progress.pack(fill='x', pady=(2, 0), after=self.header)
        else:
            self.progress.pack_forget()
        if self.expanded:
            self.refresh_detail()

    @staticmethod
    def verification_summary(state):
        verification = state.get('verification', {})
        status = verification.get('status')
        if status not in VERIFICATION_STATES:
            return ''
        checks = sum(bool(check.get('current')) for check in verification.get('checks', []))
        return '验证 · ' + VERIFICATION_STATES[status] + (f' · {checks} 项命令检查' if checks else '')

    def can_resume(self):
        result = self.result or {}
        return (result.get('status') in CONTINUABLE_STATES and result.get('continuation_available') is True
                and bool(result.get('run_dir')) and '[已撤回文件修改]' not in self.activity)

    def sync_resume(self):
        if self.can_resume():
            self.resume_button.pack(anchor='w', pady=(6, 0))
            self.resume_button.configure(state='disabled' if self.app.busy else 'normal')
        else:
            self.resume_button.pack_forget()
        if self.can_resume() and (self.result.get('task_state') or {}).get('uncertain'):
            self.recovery_button.pack(anchor='w', pady=(4, 0))
            self.recovery_button.configure(state='disabled' if self.app.busy else 'normal')
        else:
            self.recovery_button.pack_forget()
        self.run_check_button.configure(state='disabled' if self.assistant or self.app.busy
            or self.controller.permission_mode.get() == 'readonly' else 'normal')

    def confirm_recovery(self):
        state = (self.result or {}).get('task_state') or {}
        if self.app.busy or not self.can_resume() or not state.get('uncertain'):
            return
        operations = [TITLES.get(row['tool'], row['tool']) + ' · ' + row['subject']
                      for row in state.get('unresolved', [])]
        details = '\n'.join(operations) or '上次记录未提供具体操作，请查看运行记录。'
        instructions = ('请先核实这些工具是否已执行。确认继续任务吗？' if self.assistant else
            '请先检查相关文件、命令是否已执行及其副作用。'
            '你已核实这些操作的实际结果，确认继续任务吗？未完成的检查仍需执行。')
        confirmed = messagebox.askyesno('核实中断后继续',
            '待核实的操作\n' + details + '\n\n' + instructions,
            parent=self.app.root, default=messagebox.NO)
        if confirmed:
            self.controller.resume(self, recovery_confirmed=True)

    def confirm_undo(self):
        if not self.assistant and not self.app.busy:
            if ((self.result or {}).get('snapshot_dir')
                    and ((self.result or {}).get('snapshot') or {}).get('status') in ('ready', 'partial')):
                self.undo_label.configure(text='选择要恢复到任务开始前的文件。任务运行期间你自己的编辑也可能列在这里，请逐项核对。任务结束后又改过的文件会保留。未列出的文件和外部操作不受此撤回影响。')
                self.undo_files.delete(0, 'end')
                for path in self.result['snapshot']['files']:
                    self.undo_files.insert('end', path)
                self.undo_files.pack(fill='x', pady=6, after=self.undo_label)
                self.undo_select_all.pack(anchor='w', after=self.undo_files)
            else:
                self.undo_files.pack_forget()
                self.undo_select_all.pack_forget()
            if not self.expanded:
                self.toggle()
            if self.review_box.winfo_manager():
                self.toggle_review()
            self.undo_box.pack(fill='x', pady=(7, 0))
            self.resize()
            self.app.transcript.see('end')

    def cancel_undo(self):
        self.undo_box.pack_forget()
        self.resize()

    def open_records(self):
        if self.result and self.result.get('run_dir'):
            try:
                os.startfile(self.result['run_dir'])
            except OSError:
                self.app.status.set('无法打开这条任务的记录文件夹。')

    def destroy(self):
        # Release callbacks that retain the card so its Tk variables are freed on the UI thread.
        widgets = self.frame.winfo_children()
        while widgets:
            widget = widgets.pop()
            if isinstance(widget, FlatButton):
                widget.command = None
            widgets.extend(widget.winfo_children())
        self.frame.destroy()


class ProjectConversation:
    def __init__(self, app):
        self.app, self.cards, self.active = app, [], None
        self.enabled = tk.BooleanVar(value=False)
        self.workspace = tk.StringVar(value='')
        self.permission_mode = tk.StringVar(value='standard')
        self.menu_permission_mode = tk.StringVar(value='standard')
        self.allow_write = tk.BooleanVar(value=True)
        self.allow_web = tk.BooleanVar(value=False)
        self.menu_allow_web = tk.BooleanVar(value=False)
        self.assistant_enabled = tk.BooleanVar(value=True)
        self.menu_assistant_enabled = tk.BooleanVar(value=True)
        self.use_memory = tk.BooleanVar(value=False)
        self.bar = tk.Frame(app.context_bar, bg=C['bg'])
        self.bar.pack(fill='x')
        self.folder_button = FlatButton(self.bar, '选择项目', self.choose_workspace,
                                        variant='ghost', height=32, max_width=200, font_size=11, icon='folder')
        self.chat_button = FlatButton(self.bar, '智能助手', lambda: self.set_mode(False),
                                      variant='selected', height=32, font_size=11)
        self.project_button = FlatButton(self.bar, '项目助手', lambda: self.set_mode(True),
                                         variant='ghost', height=32, font_size=11)
        self.permissions_button = FlatButton(app.composer_tools, '助手能力', self.show_permissions,
                                             variant='ghost', height=30, font_size=10,
                                             icon='chevron_down', icon_side='right')
        self.permissions_menu = tk.Menu(app.root, tearoff=0, bg=C['surface'], fg=C['text'],
                                        activebackground=C['tint'], activeforeground=C['text'])
        for mode, title in PERMISSION_LABELS.items():
            self.permissions_menu.add_radiobutton(label=title, value=mode, variable=self.menu_permission_mode,
                                                  command=lambda mode=mode: self.set_permission_mode(mode))
        self.permissions_menu.add_separator()
        self.permissions_menu.add_checkbutton(label='允许文件工具修改', variable=self.allow_write,
                                             command=self.save_file_permission)
        self.write_permission_option = self.permissions_menu.index('end')
        self.permissions_menu.add_checkbutton(label='允许联网查资料', variable=self.menu_allow_web,
                                             command=self.save_web_permission)
        self.web_permission_option = self.permissions_menu.index('end')
        self.permissions_menu.add_checkbutton(label='自动使用工具', variable=self.menu_assistant_enabled,
                                             command=self.save_assistant_permission)
        self.assistant_permission_option = self.permissions_menu.index('end')
        self.permissions_menu.add_checkbutton(label='参考经验', variable=self.use_memory, command=self.save_settings)
        self.memory_permission_option = self.permissions_menu.index('end')
        self.permissions_menu.add_separator()
        self.project_menu_options = list(range(len(PERMISSION_LABELS)))
        for title, command in (('在编辑器中打开项目', self.open_project_in_editor),
                               ('固定项目检查…', self.configure_fixed_check),
                               ('在终端中打开项目', self.open_project_terminal)):
            self.permissions_menu.add_command(label=title, command=command)
            self.project_menu_options.append(self.permissions_menu.index('end'))
        self.location = label(self.bar, '', size=9, color=C['muted'])
        app.transcript.bind('<Configure>', lambda event: self.resize_cards(), add='+')
        self.bar.bind('<Configure>', lambda event: self.layout_bar())

    def layout_bar(self):
        buttons = (self.folder_button, self.chat_button, self.project_button)
        compact = self.bar.winfo_width() < sum(button.winfo_reqwidth() for button in buttons) + 12
        if getattr(self, '_bar_compact', None) == compact:
            return
        self._bar_compact = compact
        for button in buttons:
            button.grid_forget()
        if compact:
            self.folder_button.grid(row=0, column=0, columnspan=2, sticky='w')
            self.chat_button.grid(row=1, column=0, sticky='w', pady=(4, 0))
            self.project_button.grid(row=1, column=1, sticky='w', padx=(4, 0), pady=(4, 0))
        else:
            self.folder_button.grid(row=0, column=0, sticky='w')
            self.chat_button.grid(row=0, column=1, sticky='w', padx=(8, 0))
            self.project_button.grid(row=0, column=2, sticky='w', padx=(4, 0))

    def resize_cards(self):
        for card in self.cards:
            card.resize()

    def refresh_summary(self):
        refresh = getattr(self.app, 'refresh_project_summary', None)
        if refresh:
            refresh()
        if hasattr(self.app, 'memories'):
            self.app.memories.refresh()

    def show_permissions(self):
        if self.app.busy:
            return
        self.menu_assistant_enabled.set(self.assistant_enabled.get())
        button = self.permissions_button
        try:
            self.permissions_menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            self.permissions_menu.grab_release()

    def refresh(self):
        enabled = self.enabled.get()
        self.chat_button.configure(variant='ghost' if enabled else 'selected')
        self.project_button.configure(variant='selected' if enabled else 'ghost')
        workspace = self.workspace.get()
        name = Path(workspace).name or workspace
        self.folder_button.configure(text=name or '选择项目')
        self.location.configure(text='项目：' + (workspace or '尚未选择'))
        hints = [shortcut_hint(self.app.send_key)]
        if enabled:
            mode = self.permission_mode.get()
            self.permissions_button.configure(text=PERMISSION_LABELS[mode])
            if self.app.backend != 'local':
                hints.append('任务和读取的文件将发往已选 API')
            if mode != 'standard':
                hints.append(PERMISSION_HINTS[mode])
            if mode == 'automatic':
                hints.append('文件工具限于所选项目')
            if mode != 'readonly':
                hints.append('文件工具修改可撤回' if self.allow_write.get() else '文件工具修改关闭')
            hints.append('内置执行可自动搜索和读取公开网页' if self.allow_web.get() else '联网查资料关闭')
            self.app.skill_button.configure(state='disabled')
        else:
            self.permissions_button.configure(text='助手能力')
            self.app.skill_button.configure(state='normal')
            if self.assistant_enabled.get():
                hints.append('允许联网查资料' if self.allow_web.get() else '联网查资料关闭')
            else:
                hints.append('自动使用工具关闭')
        self.app.chat_hint.set(' · '.join(hints))
        self.menu_assistant_enabled.set(self.assistant_enabled.get())
        self.layout_bar()
        self.sync_busy()
        self.app.layout_composer_tools()
        self.refresh_summary()

    def set_mode(self, enabled):
        if self.app.busy:
            self.app.status.set('先停止当前任务，再切换模式。')
            return
        self.enabled.set(bool(enabled))
        if enabled:
            self.app.tool_mode.set(False)
        self.save_settings()
        self.refresh()
        self.app.select_page(0)
        self.app.input.focus_set()

    def set_permission_mode(self, mode):
        current = self.permission_mode.get()
        if self.app.busy or not isinstance(mode, str) or mode not in PROJECT_PERMISSION_MODES:
            self.menu_permission_mode.set(current)
            self.app.status.set('先停止当前任务，再切换权限。' if self.app.busy else '项目权限模式无效。')
            return False
        allow_write = mode != 'readonly' and (current == 'readonly' or self.allow_write.get())
        try:
            self.save_settings(permission_mode=mode, allow_write=allow_write)
        except Exception:
            logging.exception('Project permission update failed')
            self.menu_permission_mode.set(current)
            self.app.status.set('未能保存项目权限。')
            return False
        self.apply_permission(mode, allow_write)
        self.refresh()
        return True

    def apply_permission(self, mode, allow_write):
        self.allow_write.set(allow_write and mode != 'readonly')
        self.permission_mode.set(mode)
        self.menu_permission_mode.set(mode)

    def save_file_permission(self):
        self.save_settings()
        self.refresh()

    def save_web_permission(self):
        current = self.allow_web.get()
        if self.app.busy:
            self.menu_allow_web.set(current)
            self.app.status.set('先停止当前任务，再更改联网权限。')
            return
        allowed = self.menu_allow_web.get()
        try:
            self.save_settings(allow_web=allowed)
        except Exception:
            logging.exception('Web permission update failed')
            self.menu_allow_web.set(current)
            self.app.status.set('未能保存联网权限。')
            return
        self.allow_web.set(allowed)
        self.refresh()

    def save_assistant_permission(self):
        current = self.assistant_enabled.get()
        if self.app.busy or self.enabled.get():
            self.menu_assistant_enabled.set(current)
            return
        enabled = self.menu_assistant_enabled.get()
        try:
            self.save_settings(assistant_enabled=enabled)
        except Exception:
            logging.exception('Assistant tool permission update failed')
            self.menu_assistant_enabled.set(current)
            self.app.status.set('未能保存助手工具设置。')
            return
        self.assistant_enabled.set(enabled)
        self.refresh()

    def configure_fixed_check(self):
        if self.app.busy:
            return
        folder = self.workspace.get().strip()
        if not folder:
            self.app.status.set('请先选择项目文件夹。')
            return
        try:
            try:
                current = load_project_check(self.app.sessions, folder)
            except ValueError:
                # Permit replacement of a saved command that no longer parses.
                current = ''
            command = simpledialog.askstring('固定项目检查',
                f'项目：{folder}\n填写测试命令或 JSON 参数数组，保存后在可写任务结束时运行。'
                '检查失败时使用本轮模型自动续修，最多 2 次。命令沿用本轮权限。\n留空保存可停用。',
                initialvalue=current, parent=self.app.root)
            if command is None:
                return
            save_project_check(self.app.sessions, folder, command)
            self.app.status.set('已为当前项目启用固定检查。' if command.strip() else '已停用当前项目的固定检查。')
        except (ValueError, OSError, RuntimeError) as error:
            self.app.status.set(str(error))

    def save_settings(self, *, session_id=None, **overrides):
        identity = session_id if session_id is not None else self.app.session_id
        if identity:
            self.app.sessions.set_project_settings(identity, {
                'enabled': self.enabled.get(), 'workspace': self.workspace.get(),
                'allow_write': self.allow_write.get(), 'use_memory': self.use_memory.get(),
                'permission_mode': self.permission_mode.get(), 'allow_web': self.allow_web.get(),
                'assistant_enabled': self.assistant_enabled.get(), **overrides})

    def choose_workspace(self):
        if self.app.busy:
            return
        folder = filedialog.askdirectory(parent=self.app.root, title='选择这段对话要处理的项目')
        if not folder:
            return
        folder = str(Path(folder).absolute())
        try:
            if is_assistant_workspace(folder):
                self.app.status.set('智能助手的记录目录不能作为项目。')
                return
        except (OSError, ValueError, RuntimeError) as error:
            self.app.status.set(str(error))
            return
        if Path(folder) == Path(self.workspace.get()):
            if not self.enabled.get():
                self.set_mode(True)
            return
        mode = 'readonly' if self.permission_mode.get() == 'readonly' else 'standard'
        allow_write = self.allow_write.get() and mode != 'readonly'
        self.save_settings(enabled=True, workspace=folder, permission_mode=mode, allow_write=allow_write)
        self.workspace.set(folder)
        self.apply_permission(mode, allow_write)
        self.enabled.set(True)
        self.app.tool_mode.set(False)
        self.refresh()
        self.app.select_page(0)
        self.app.input.focus_set()

    def open_project_in_editor(self):
        return self.app.open_project_in_editor(self.workspace.get())

    def open_project_terminal(self):
        return self.app.open_project_terminal(self.workspace.get())

    def load(self, identity):
        for card in self.cards:
            card.destroy()
        self.cards, self.active = [], None
        settings = self.app.sessions.project_settings(identity) if identity else {}
        self.enabled.set(settings.get('enabled', False))
        self.workspace.set(settings.get('workspace', ''))
        allow_write = settings.get('allow_write', True)
        mode = settings.get('permission_mode')
        if not isinstance(mode, str) or mode not in PROJECT_PERMISSION_MODES:
            mode = 'standard' if allow_write else 'readonly'
        self.apply_permission(mode, allow_write)
        self.allow_web.set(settings.get('allow_web') is True)
        self.menu_allow_web.set(self.allow_web.get())
        self.assistant_enabled.set(settings.get('assistant_enabled', True) is not False)
        self.menu_assistant_enabled.set(self.assistant_enabled.get())
        self.use_memory.set(settings.get('use_memory', False))
        self.refresh()
        rows = self.app.sessions.project_runs(identity) if identity else []
        for row in rows:
            result = row['result']
            assistant = result.get('capability_mode') == 'assistant'
            if result.get('acceptance'):
                result['acceptance'] = {**result['acceptance'], 'stale': True}
            if result.get('fixed_check'):
                result['fixed_check'] = {**result['fixed_check'], 'stale': True}
            if not assistant and result.get('snapshot_dir') and not result.get('snapshot'):
                try:
                    from project_snapshot import describe_snapshot
                    result['snapshot'] = describe_snapshot(ARTIFACTS / 'project_snapshots', result['snapshot_dir'], row['workspace'])
                except (OSError, ValueError, RuntimeError):
                    result['snapshot'] = {'status': 'unavailable', 'files': [], 'count': 0,
                                          'notice': '无法读取已保存的项目快照，请检查记录文件夹。'}
            if not assistant and result.get('engine') != 'harness' and result.get('run_dir') and (result.get('status') in ACTIVE_STATES | {'interrupted'}
                                          or (result.get('task_state') or {}).get('parent_run')):
                result = {**result, 'changes': recorded_run_changes(result['run_dir'], row['workspace'])}
                row['result'] = result
            if result.get('status') not in ACTIVE_STATES:
                continue
            if result.get('run_dir'):
                from agent_task_state import load_task
                try:
                    if assistant and Path(row['workspace']) != assistant_workspace(identity):
                        raise ValueError('智能助手任务不属于此会话。')
                    state = load_task(ARTIFACTS / 'project_runs', result['run_dir'], row['workspace']).export()
                    result = apply_task_state(result, {**state, 'status': 'interrupted'})
                except (OSError, ValueError, RuntimeError):
                    pass
            interrupted = '上次任务中断。' if assistant else '上次项目任务中断。'
            answer = interrupted
            if result.get('continuation_available'):
                answer = '上次任务中断，可以从这张任务卡继续。' if assistant else INTERRUPTED_ANSWER
            result = {**result, 'status': 'interrupted', 'answer': answer}
            row['result'] = result
            index = row['assistant_index']
            self.app.history[index] = {'role': 'assistant', 'content': answer}
            if not any(message['role'] == 'assistant' for message in self.app.history[index + 1:]):
                self.app.last_answer = answer
            self.save_reply(identity, index, result, row['workspace'], row['activity'])
        return {row['assistant_index']: row for row in rows}

    def restore(self, record):
        card = ActivityCard(self, record['workspace'], record)
        self.cards.append(card)
        self.refresh_summary()
        return card

    def sync_busy(self):
        state = 'disabled' if self.app.busy else 'normal'
        project_state = state if self.enabled.get() else 'disabled'
        for button in (self.chat_button, self.project_button, self.folder_button, self.permissions_button):
            button.configure(state=state)
        for index in self.project_menu_options:
            self.permissions_menu.entryconfigure(index, state=project_state)
        self.permissions_menu.entryconfigure(self.write_permission_option,
            state='disabled' if self.permission_mode.get() == 'readonly' else project_state)
        self.permissions_menu.entryconfigure(self.web_permission_option, state=state)
        self.permissions_menu.entryconfigure(self.assistant_permission_option,
            state='disabled' if self.enabled.get() else state)
        self.permissions_menu.entryconfigure(self.memory_permission_option, state=project_state)
        for card in self.cards:
            card.sync_resume()
        if self.enabled.get():
            self.app.skill_button.configure(state='disabled')

    def save_reply(self, identity, index, result, workspace, activity):
        try:
            self.app.sessions.update_project_reply(identity, index, result, workspace, activity)
            return True
        except Exception:
            logging.exception('Project card update failed')
            return False

    def save_card(self, card):
        if card.assistant_index is None:
            return False
        return self.save_reply(card.identity, card.assistant_index, card.result, card.workspace, card.activity)

    def update_card_answer(self, card, content):
        """Keep the visible conversation and subsequent model history in step with checks."""
        previous = card.result.get('answer', '')
        card.result = {**card.result, 'answer': content}
        app = self.app
        index = getattr(card, 'history_index', card.assistant_index)
        if index is not None and 0 <= index < len(app.history):
            app.history[index] = {'role': 'assistant', 'content': content}
            if index == len(app.history) - 1:
                app.last_answer = content
        transcript = app.transcript
        for _, name, position in transcript.dump('1.0', 'end', window=True):
            if name == str(card.frame):
                start = transcript.index(position + ' + 2 chars')
                length = transcript.tk.call('string', 'length', previous)
                end = transcript.index(f'{start} + {length} chars')
                if transcript.get(start, end) == previous:
                    transcript.configure(state='normal')
                    try:
                        transcript.delete(start, end)
                        transcript.insert(start, content)
                    finally:
                        transcript.configure(state='disabled')
                    app.format_transcript()
                break

    def invalidate_checks(self, workspace):
        for card in self.cards:
            if (os.path.normcase(card.workspace) == os.path.normcase(workspace)
                    and card.result and (card.result.get('acceptance') or card.result.get('fixed_check'))):
                for key in ('acceptance', 'fixed_check'):
                    if card.result.get(key):
                        card.result[key] = {**card.result[key], 'stale': True}
                card.finish(card.result)
                self.save_card(card)

    def verify(self, card):
        app = self.app
        if app.busy or card.assistant or card not in self.cards or not card.result or not card.result.get('run_dir'):
            return
        if self.permission_mode.get() == 'readonly':
            app.status.set('只看项目模式不能运行检查，请先选择命令权限。')
            return
        try:
            if Path(self.workspace.get()).resolve(strict=True) != Path(card.workspace).resolve(strict=True):
                raise ValueError('请先选择这张任务卡原来的项目文件夹。')
            from project_verification import parse_command, run_verification, format_verification_context
            command = card.check_command.get().strip()
            parse_command(command)
        except (OSError, ValueError, RuntimeError) as error:
            app.status.set(str(error))
            return
        permission = self.permission_mode.get()
        self.invalidate_checks(card.workspace)
        card.status.set('正在运行你指定的检查')

        def work():
            return run_verification(card.workspace, command, cancel_event=app.generation_cancel,
                                    permission_mode=permission)

        def done(outcome):
            card.result = {**card.result, 'acceptance': outcome}
            old_fixed = card.result.get('fixed_check') or {}
            if old_fixed.get('argv') and outcome.get('argv') == old_fixed['argv']:
                card.result['fixed_check'] = {**outcome, 'source': 'fixed',
                                              'repair_attempt': old_fixed.get('repair_attempt', 0)}
            if outcome.get('status') != 'passed':
                resumable = card.result.get('continuation_available', False)
                if card.result.get('engine') != 'harness':
                    try:
                        from agent_task_state import load_task
                        load_task(ARTIFACTS / 'project_runs', card.result['run_dir'], card.workspace)
                        resumable = True
                    except (OSError, ValueError, RuntimeError):
                        pass
                card.result.update(status='unfinished', continuation_available=resumable)
            details = format_verification_context(outcome)
            card.append('手动检查\n' + details)
            previous = card.result.get('answer', '').split('\n\n[手动项目检查]\n', 1)[0]
            self.update_card_answer(card, previous + '\n\n[手动项目检查]\n' + details)
            card.finish(card.result)
            summary = acceptance_summary(card.result)
            if (outcome.get('status') == 'passed' and card.result.get('fixed_check')
                    and card.result['fixed_check']['status'] != 'passed'):
                summary += ' · 项目固定检查尚未通过'
            card.status.set(summary)
            if not self.save_card(card):
                app.status.set('检查已结束，但记录未能保存，请复制详情中的结果。')
            else:
                app.status.set(summary)
            self.sync_busy()
        app.submit('正在检查项目', work, done, cancellable=True)
        self.sync_busy()

    def resume(self, card, *, recovery_confirmed=False):
        if self.app.busy or card not in self.cards or not card.can_resume():
            return
        if card.assistant:
            self.send(resume_from=card.result['run_dir'], recovery_confirmed=recovery_confirmed, assistant=True)
            return
        try:
            folder = self.workspace.get().strip()
            same_workspace = bool(folder) and Path(folder).resolve(strict=True) == Path(card.workspace).resolve(strict=True)
        except (OSError, RuntimeError):
            same_workspace = False
        if not same_workspace:
            self.app.status.set('请选择这条任务原来的项目文件夹，再继续任务。')
            return
        if not self.enabled.get():
            self.set_mode(True)
        self.send(resume_from=card.result['run_dir'], recovery_confirmed=recovery_confirmed)

    def send(self, *, resume_from=None, recovery_confirmed=False, repair_attempt=0, assistant=False):
        app = self.app
        if app.busy:
            return
        source = None
        resume_notice = ''
        if resume_from:
            source = next((card for card in self.cards if card.result and card.result.get('run_dir') == resume_from), None)
            if source is None:
                app.status.set('无法找到原任务，草稿已保留。')
                return
            assistant = source.assistant
        draft = '' if repair_attempt else app.input.get('1.0', 'end').strip()
        continue_task = ASSISTANT_CONTINUE_TASK if assistant else CONTINUE_TASK
        task = (f'自动续修 {repair_attempt}/{MAX_REPAIR_ATTEMPTS}：固定检查未通过。请根据实际失败输出修复原任务，程序会重新检查。'
                if repair_attempt else draft or (continue_task if resume_from else ''))
        folder = self.workspace.get().strip()
        if not task:
            return
        if assistant:
            if app.image_path:
                app.status.set('此任务使用文字和 CSV。请先移除图片，草稿会保留。')
                return
            csv_path = app.skill_file
            if source and source.result.get('csv_attachment') and not csv_path:
                app.status.set('继续这条任务前，请从 + 菜单重新选择要分析的 CSV 文件。')
                return
            if csv_path and not Path(csv_path).is_file():
                app.status.set('找不到已选择的 CSV 文件。请重新选择，当前草稿仍保留。')
                return
            engine = 'builtin'
        else:
            if not folder or not Path(folder).is_dir():
                app.status.set('先选择这段对话的项目文件夹，输入内容会保留。')
                return
            try:
                if is_assistant_workspace(folder):
                    app.status.set('智能助手的记录目录不能作为项目。')
                    return
                if source and Path(folder).resolve(strict=True) != Path(source.workspace).resolve(strict=True):
                    app.status.set('请选择这条任务原来的项目文件夹，再继续任务。')
                    return
            except (OSError, ValueError, RuntimeError) as error:
                app.status.set(str(error))
                return
            if not repair_attempt and (app.image_path or app.skill_file):
                app.status.set('做项目暂时只处理文字文件。请先在 + 菜单移除图片或 CSV 附件，草稿会保留。')
                return
            from harness_settings import load_preferences as load_execution_preferences
            execution = load_execution_preferences(app.sessions)
            engine = source.result.get('engine', 'builtin') if source else execution['engine']
        if engine == 'harness':
            try:
                from harness_agent import validate_selection, resolve_resume, validate_task
                execution['executable'] = validate_selection(execution['executable'], self.permission_mode.get(), self.allow_write.get())
                validate_task(task)
                if resume_from:
                    latest = str(resolve_resume(ARTIFACTS / 'project_runs', resume_from, folder))
                    if os.path.normcase(latest) != os.path.normcase(str(resume_from)):
                        if repair_attempt:
                            raise ValueError('组件会话已有更新，自动续修已停止。请从最新任务卡继续。')
                        if recovery_confirmed:
                            raise ValueError('这张卡之后已有新进度，请核实最新任务卡后再继续。')
                        resume_notice = '已接续本会话最新进度。'
                        resume_from = latest
                        source = next((card for card in self.cards if card.result and card.result.get('run_dir') == latest), None)
            except (ValueError, OSError, RuntimeError) as error:
                app.status.set(str(error))
                return
        local = app.backend == 'local'
        if local:
            from model_catalog import is_chat_model
            if not is_chat_model(app.manager.catalog, app.selected_model) or not app.manager.ready(app.selected_model):
                if assistant:
                    app.show_capabilities('models')
                app.status.set('请先选择已下载的文字模型，或接通 API；草稿会保留。')
                return
            from local_project_model import LocalProjectModel
            model = LocalProjectModel(model_id=app.selected_model)
        else:
            try:
                model = app.make_api_client()
            except Exception:
                if assistant:
                    app.show_api_settings()
                app.status.set('API 配置不可用，请在设置里检查；草稿会保留。')
                return
        identity = app.session_id
        if assistant:
            try:
                if identity is None:
                    identity = app.sessions.create()
                internal = assistant_workspace(identity, create=source is None)
                if source and (source.identity != identity or Path(source.workspace) != internal):
                    raise ValueError('智能助手任务不属于此会话。')
                folder = internal
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                app.status.set(f'无法准备智能助手的记录目录，草稿会保留。{error}')
                return
        history = list(app.history)
        workspace = str(Path(folder).absolute())
        model_task = task
        if not assistant:
            previous_workspace = None
            for previous in self.cards:
                index = getattr(previous, 'history_index', previous.assistant_index)
                if previous.assistant or index is None or not 0 <= index < len(history) or history[index]['role'] != 'assistant':
                    continue
                reply = history[index]
                history[index] = {**reply, 'content': '该项目回复对应的工作区：' + previous.workspace + '\n\n' + reply['content']}
                previous_workspace = previous.workspace
            if previous_workspace and os.path.normcase(previous_workspace) != os.path.normcase(workspace):
                model_task += '\n\n本轮已切换项目目录。请先重新检查当前工作区的文件。历史项目操作的结果仅适用于其标注的工作区。'
        if not assistant and source and source.result.get('acceptance'):
            from project_verification import format_verification_context
            model_task += '\n\n用户在本机运行的上轮检查结果（历史证据，需要重跑）：\n' + format_verification_context(source.result['acceptance'])
        if not assistant and source and source.result.get('fixed_check'):
            from project_verification import format_verification_context
            model_task += '\n\n' + format_verification_context(source.result['fixed_check'])
        fixed_command = ''
        if not assistant:
            try:
                fixed_command = load_project_check(app.sessions, workspace)
            except (ValueError, OSError, RuntimeError) as error:
                app.status.set(f'无法读取固定项目检查，请在项目菜单重新配置。{error}')
                return
        if engine == 'harness':
            try:
                validate_task(model_task)
            except ValueError as error:
                app.status.set(str(error))
                return
        context = max(16384, CONTEXTS[app.context.get()])
        permission_mode = 'readonly' if assistant else self.permission_mode.get()
        allow_write, use_memory = not assistant and self.allow_write.get(), not assistant and self.use_memory.get()
        allow_web = self.allow_web.get() and engine == 'builtin'
        personal_options = app.memories.snapshot(app.backend)
        try:
            if identity is None:
                identity = app.sessions.create()
            self.save_settings(session_id=identity)
            if not repair_attempt and hasattr(app, 'draft_session_id'):
                app.sessions.append(identity, 'user', task, draft_from=app.draft_session_id)
            else:
                app.sessions.append(identity, 'user', task)
        except (OSError, sqlite3.Error, ValueError):
            logging.exception('Task submission could not be saved')
            app.status.set('未能保存会话，草稿会保留。')
            return
        app.session_id = identity
        if not assistant:
            self.invalidate_checks(workspace)
        app.history.append({'role': 'user', 'content': task})
        app.append_heading('自动续修' if repair_attempt else '你', 'user')
        app.write(app.transcript, task + '\n\n', True)
        app.append_heading('Free AI', 'assistant')
        app.pending_chat, app.streamed = None, ''
        app.last_episode, app.last_tool_result = None, None
        if not repair_attempt:
            if hasattr(app, 'clear_submitted_draft'):
                app.clear_submitted_draft()
            else:
                app.input.delete('1.0', 'end')
        app.update_placeholder()
        app.layout_chat()
        app.root.update_idletasks()
        card = ActivityCard(self, workspace, assistant=assistant)
        initial_result = {'status': 'running', 'answer': '任务执行中。' if assistant else '项目任务执行中。',
                          'run_dir': '', 'changes': [], 'continuation_available': False, 'engine': engine}
        if assistant:
            initial_result['capability_mode'] = 'assistant'
            if csv_path:
                initial_result['csv_attachment'] = True
        else:
            initial_result['permission_mode'] = permission_mode
        if engine == 'builtin':
            initial_result['allow_web'] = allow_web
        if not assistant and source and source.result.get('acceptance'):
            initial_result['acceptance'] = {**source.result['acceptance'], 'stale': True}
        if not assistant and source and source.result.get('fixed_check'):
            initial_result['fixed_check'] = {**source.result['fixed_check'], 'stale': True}
        card.result = initial_result
        if resume_notice:
            card.append(resume_notice)
        card.history_index = len(app.history)
        app.history.append({'role': 'assistant', 'content': initial_result['answer']})
        try:
            card.assistant_index = app.sessions.append_project_reply(identity, initial_result, workspace, '')
        except Exception:
            logging.exception('Project card creation failed')
        self.cards.append(card)
        self.active = card
        self.refresh_summary()
        app.refresh_recent()
        assistant_index = card.assistant_index
        snapshot_state = {}
        denied = threading.Event()

        def approve(details):
            if permission_mode != 'standard':
                return False
            request = {'details': details, 'ready': threading.Event(), 'allowed': False}
            app.events.put(('update', card.show_approval, request))
            while not request['ready'].wait(.1):
                if app.generation_cancel.is_set():
                    return False
            if not request['allowed']:
                denied.set()
            return request['allowed'] and not app.generation_cancel.is_set()

        def on_event(event):
            if event.get('type') == 'start' and snapshot_state.get('directory'):
                event = {**event, 'snapshot_dir': snapshot_state['directory']}
            if assistant_index is not None and event.get('type') == 'start' and event.get('run_dir'):
                started = {**initial_result, 'run_dir': event['run_dir'], 'continuation_available': True}
                if event.get('snapshot_dir'):
                    started['snapshot_dir'] = event['snapshot_dir']
                started = apply_task_state(started, event.get('task_state'))
                self.save_reply(identity, assistant_index,
                                started, workspace, '')
            app.events.put(('update', card.event, event))

        def work():
            runner = None
            try:
                from project_agent import ProjectAgent
                if not assistant and permission_mode != 'readonly':
                    from project_snapshot import start_snapshot
                    snapshot_state['directory'] = start_snapshot(ARTIFACTS / 'project_snapshots', workspace,
                                                                 cancel_event=app.generation_cancel)
                if app.generation_cancel.is_set():
                    raise RuntimeError('任务已停止。')
                if engine == 'harness':
                    from harness_agent import HarnessAgent
                    runner = HarnessAgent(workspace, model, executable=execution['executable'],
                        runs_root=ARTIFACTS / 'project_runs', approve_command=approve, on_event=on_event,
                        cancel_event=app.generation_cancel, allow_write=allow_write,
                        context=context, permission_mode=permission_mode,
                        personal_memory=app.personal_memory, read_memory=personal_options['read'])
                else:
                    capabilities = ({'capability_mode': 'assistant', 'csv_path': csv_path} if assistant
                                    else {'integrated_tools': True})
                    runner = ProjectAgent(workspace, model, runs_root=ARTIFACTS / 'project_runs',
                        approve_command=approve, on_event=on_event,
                        cancel_event=app.generation_cancel, allow_write=allow_write, max_steps=16,
                        context=context, knowledge=app.knowledge, use_memory=use_memory,
                        personal_memory=app.personal_memory, read_memory=personal_options['read'],
                        write_memory=personal_options['write'], permission_mode=permission_mode, allow_web=allow_web,
                        **capabilities)
                if resume_from:
                    if recovery_confirmed:
                        result = runner.run(model_task, history=history, resume_from=resume_from,
                                          recovery_confirmed=True)
                    else:
                        result = runner.run(model_task, history=history, resume_from=resume_from)
                else:
                    result = runner.run(model_task, history=history)
                if (fixed_command and permission_mode != 'readonly'
                        and result.get('status') in ('completed', 'review', 'unfinished', 'limit')):
                    from project_verification import run_verification, format_verification_context
                    on_event({'type': 'instructions', 'message': '正在运行已保存的项目固定检查。'})
                    check = run_verification(workspace, fixed_command, cancel_event=app.generation_cancel,
                                             permission_mode=permission_mode, approve_command=approve)
                    result['fixed_check'] = {**check, 'source': 'fixed', 'repair_attempt': repair_attempt}
                    result['answer'] += '\n\n[项目固定检查]\n' + format_verification_context(result['fixed_check'])
                    if check['status'] != 'passed':
                        result.update(status='stopped' if check['status'] == 'stopped' else 'unfinished',
                                      continuation_available=bool(result.get('run_dir')))
                        result['answer'] = '项目固定检查尚未通过。\n\n模型本轮回复：\n' + result['answer']
                        if check['status'] == 'failed' and repair_attempt == MAX_REPAIR_ATTEMPTS:
                            result['answer'] += '\n已达到自动续修次数上限。'
            except Exception:
                logging.exception('Conversation task failed')
                directory = getattr(runner, 'run_dir', None)
                stopped = app.generation_cancel.is_set()
                if assistant:
                    answer = ('任务已停止。' if stopped else '智能助手未能完成任务，请查看运行记录后重试。')
                else:
                    answer = ('项目任务已停止。' if stopped else
                              '项目准备或执行失败，请检查目录、运行记录和磁盘空间。已有改动会保留。')
                result = {'status': 'stopped' if app.generation_cancel.is_set() else 'failed',
                        'answer': answer,
                        'run_dir': str(directory) if directory else '', 'changes': getattr(runner, 'changes', []),
                        'episode_id': None, 'continuation_available': directory is not None, 'engine': engine}
            if snapshot_state.get('directory'):
                result['snapshot_dir'] = snapshot_state['directory']
                try:
                    from project_snapshot import finish_snapshot
                    result['snapshot'] = finish_snapshot(ARTIFACTS / 'project_snapshots', result['snapshot_dir'], workspace)
                except (OSError, ValueError, RuntimeError):
                    result['snapshot'] = {'status': 'unavailable', 'count': 0, 'files': [],
                                          'notice': '未能保存执行后的快照，不能自动撤回这轮项目。'}
                    result['answer'] += '\n\n' + result['snapshot']['notice']
            return result

        def done(result):
            self.active = None
            result = {**card.result, **result}
            if result.get('acceptance', {}).get('stale') and result.get('status') == 'completed':
                result.update(status='review', continuation_available=bool(result.get('run_dir')))
                result['answer'] += '\n\n上轮指定的手动检查需要重新运行，请在任务卡中检查项目结果。'
            if (fixed_command and result.get('fixed_check', {}).get('stale')
                    and result.get('status') in ('review', 'completed')):
                result.update(status='unfinished', continuation_available=bool(result.get('run_dir')))
                result['answer'] += '\n\n本轮尚未执行已保存的项目固定检查。'
            memory_notice = (app.memories.update_notice(result.get('memory_updates', []))
                             + app.memories.read_notice(result.get('memory_ids', [])))
            if memory_notice:
                result = {**result, 'answer': result['answer'] + memory_notice}
            card.finish(result)
            app.last_answer = result['answer']
            app.last_episode = result.get('episode_id')
            app.history[card.history_index] = {'role': 'assistant', 'content': app.last_answer}
            app.pending_chat = None
            app.write(app.transcript, app.last_answer + '\n\n', True)
            app.format_transcript()
            notice = card.status_text(result.get('status')) + '，可以在这里继续提出下一步。'
            saved = self.save_card(card)
            if card.assistant_index is None:
                try:
                    card.assistant_index = app.sessions.append_project_reply(identity, result, workspace, card.activity)
                    saved = True
                except Exception:
                    logging.exception('Project reply creation failed')
            if not saved:
                notice = '未能保存本轮对话和任务卡片，请保留运行记录文件夹。'
                card.append(notice)
            app.status.set(notice)
            app.refresh_model_state()
            self.sync_busy()
            self.refresh_summary()
            if (not assistant and saved and repair_attempt < MAX_REPAIR_ATTEMPTS and not denied.is_set()
                    and not app.generation_cancel.is_set() and card.can_resume()
                    and app.session_id == identity and card in self.cards
                    and os.path.normcase(self.workspace.get()) == os.path.normcase(workspace)
                    and not (result.get('task_state') or {}).get('uncertain')
                    and not result.get('fixed_check', {}).get('stale')
                    and result.get('fixed_check', {}).get('status') == 'failed'):
                self.send(resume_from=result['run_dir'], repair_attempt=repair_attempt + 1)
        app.submit('智能助手正在处理' if assistant else '项目助手正在处理', work, done, cancellable=True)
        self.sync_busy()

    def undo(self, card):
        if card.assistant:
            return
        snapshot = (card.result or {}).get('snapshot') or {}
        use_snapshot = bool((card.result or {}).get('snapshot_dir') and snapshot.get('status') in ('ready', 'partial'))
        if (self.app.busy or not card.result or (card.result.get('engine') == 'harness' and not use_snapshot) or not card.result.get('run_dir')
                or not (snapshot.get('count') if use_snapshot else card.result.get('changes'))
                or '[已撤回文件修改]' in card.activity):
            return
        from project_agent import rollback_run
        try:
            if use_snapshot:
                from project_snapshot import restore_snapshot
                selected = None
                if card.undo_files.winfo_manager():
                    selected = [card.undo_files.get(index) for index in card.undo_files.curselection()]
                    if not selected:
                        card.status.set('请先在列表中选择要撤回的文件。')
                        return
                outcome = restore_snapshot(ARTIFACTS / 'project_snapshots', card.result['snapshot_dir'], card.workspace, paths=selected)
            else:
                directory = Path(card.result['run_dir']).resolve()
                directory.relative_to((ARTIFACTS / 'project_runs').resolve())
                outcome = rollback_run(directory, card.workspace)
        except (OSError, ValueError, RuntimeError):
            card.status.set('无法撤回，请检查文件和任务记录。')
            return
        card.cancel_undo()
        card.append('[已撤回文件修改]\n' + json.dumps(outcome, ensure_ascii=False))
        card.undo_button.configure(state='disabled')
        card.sync_resume()
        card.status.set('已恢复 ' + str(len(outcome['restored'])) + ' 个文件，跳过 ' + str(len(outcome['conflicts'])) + ' 个后来有改动的文件')
        restored, conflicts = outcome['restored'], outcome['conflicts']
        note = '用户已执行文件撤回。已恢复文件：' + ('、'.join(restored) or '无') + '。'
        if conflicts:
            note += '跳过文件：' + '、'.join(conflicts) + '。'
        note += '此次撤回覆盖快照中的项目文件。' if use_snapshot else '此次撤回覆盖文件工具的修改。'
        self.invalidate_checks(card.workspace)
        app = self.app
        index = getattr(card, 'history_index', card.assistant_index)
        previous = app.history[index]['content']
        content = previous + '\n\n' + note
        card.result = {**card.result, 'answer': content, 'continuation_available': False}
        state = card.result.get('task_state') or {}
        verification = state.get('verification', {})
        if restored and verification.get('checks'):
            verification = {'status': 'stale', 'checks': [{**check, 'current': False}
                                                          for check in verification['checks']]}
            card.result['task_state'] = {**state, 'verification': verification}
        if not self.save_card(card):
            notice = '文件撤回已执行，但撤回说明未能保存到会话。'
            app.status.set(notice)
            card.append(notice)
        app.history[index] = {'role': 'assistant', 'content': content}
        card.show_progress()
        if index == len(app.history) - 1:
            app.last_answer = content
        transcript = app.transcript
        for _, name, position in transcript.dump('1.0', 'end', window=True):
            if name == str(card.frame):
                start = transcript.index(position + ' + 2 chars')
                length = transcript.tk.call('string', 'length', previous)
                end = transcript.index(f'{start} + {length} chars')
                if transcript.get(start, end) == previous:
                    transcript.configure(state='normal')
                    try:
                        transcript.insert(end, '\n\n' + note)
                    finally:
                        transcript.configure(state='disabled')
                break
        card.resize()
        self.refresh_summary()
