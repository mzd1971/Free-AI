"""Project work embedded in the existing conversation and composer."""
import json
import logging
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from app_state import CONTEXTS, PROJECT_PERMISSION_MODES
from paths import ARTIFACTS
from ui_kit import COLORS as C, FONT, FlatButton, label, ui_font

TITLES = {'remember_memory': '保存长期记忆', 'list_files': '查看项目', 'search_text': '搜索项目',
          'read_file': '读取文件', 'write_file': '写入文件', 'replace_text': '修改文件',
          'run_command': '运行命令', 'update_plan': '整理计划'}
STATES = {'running': '正在执行项目任务', 'executing': '正在执行项目任务', 'checking': '正在验证任务结果',
          'completed': '本轮已结束', 'stopped': '已停止', 'limit': '达到本轮上限', 'failed': '未完成',
          'unfinished': '仍需处理', 'needs_work': '仍需处理', 'interrupted': '上次任务中断'}
ACTIVE_STATES = {'running', 'executing', 'checking'}
CONTINUABLE_STATES = {'stopped', 'limit', 'failed', 'unfinished', 'needs_work', 'interrupted'}
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


def apply_task_state(result, state):
    if not isinstance(state, dict):
        return result
    return {**result, 'task_state': state}


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
    def __init__(self, controller, workspace, record=None):
        self.controller, self.app = controller, controller.app
        self.workspace, self.result, self.assistant_index = workspace, None, None
        self.identity = self.app.session_id
        self.activity, self.expanded, self.request, self.steps = '', False, None, 0
        self.frame = tk.Frame(self.app.transcript, bg=C['bg'], padx=9, pady=4,
                              highlightthickness=1, highlightbackground=C['line'])
        self.inner = tk.Frame(self.frame, bg=C['bg'])
        self.inner.pack(fill='both', expand=True)
        self.header = tk.Frame(self.inner, bg=C['bg'])
        self.header.pack(fill='x')
        self.status = tk.StringVar(value='正在准备项目任务')
        self.status_label = label(self.header, textvariable=self.status, size=11, anchor='w', justify='left')
        self.status_label.pack(side='left', fill='x', expand=True)
        self.expand_button = FlatButton(self.header, '详情 ▾', self.toggle, variant='ghost', height=26, width=72, font_size=10)
        self.expand_button.pack(side='right')
        self.progress = label(self.inner, '', size=10, anchor='w', justify='left')
        self.resume_button = FlatButton(self.inner, '继续任务', lambda: self.controller.resume(self),
                                        variant='primary', height=30, font_size=10)
        self.recovery_button = FlatButton(self.inner, '核实中断后继续', self.confirm_recovery,
                                          variant='ghost', height=30, font_size=10)
        self.detail = tk.Text(self.inner, bg=C['surface'], fg=C['text'], height=8, width=1,
                              wrap='word', font=ui_font(self.app.root, 10), bd=0, padx=9, pady=8, state='disabled')
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
        FlatButton(actions, '执行这条命令', lambda: self.decide(True), variant='primary', height=32, font_size=10).pack(side='left')
        FlatButton(actions, '跳过', lambda: self.decide(False), variant='ghost', height=32, font_size=10).pack(side='left', padx=10)
        self.tools = tk.Frame(self.inner, bg=C['bg'])
        self.review_button = FlatButton(self.tools, '查看改动', self.toggle_review, variant='ghost',
                                        height=30, font_size=10, state='disabled')
        self.review_button.pack(side='left')
        self.undo_button = FlatButton(self.tools, '撤回文件修改', self.confirm_undo, variant='ghost', height=30, font_size=10)
        self.undo_button.pack(side='left', padx=6)
        FlatButton(self.tools, '打开记录文件夹', self.open_records, variant='ghost', height=30, font_size=10).pack(side='left', padx=6)
        self.review_box = tk.Frame(self.inner, bg=C['bg'])
        label(self.review_box, '文件工具修改', size=10, color=C['muted'], anchor='w').pack(fill='x', pady=(3, 4))
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
            width = available if self.expanded or self.request or self.undo_box.winfo_manager() else min(available, 420)
            self.status_label.configure(wraplength=width - 96)
            self.progress.configure(wraplength=width - 44)
            self.command_label.configure(wraplength=width - 44)
            self.undo_label.configure(wraplength=width - 44)
            self.frame.pack_propagate(False)
            self.frame.configure(width=width)
            self.inner.update_idletasks()
            self.frame.configure(height=self.inner.winfo_reqheight() + 10)
        finally:
            self._resizing = False

    def toggle(self):
        self.expanded = not self.expanded
        if self.expanded:
            self.refresh_detail()
            anchor = self.progress if self.progress.winfo_manager() else self.header
            self.detail.pack(fill='x', pady=(8, 0), after=anchor)
            if self.result and self.result.get('run_dir'):
                self.tools.pack(fill='x', pady=(4, 0))
        else:
            self.detail.pack_forget()
            self.tools.pack_forget()
            self.close_review()
            self.cancel_undo()
        self.expand_button.configure(text='收起 ▴' if self.expanded else '详情 ▾')
        self.resize()

    def close_review(self):
        self.review_box.pack_forget()
        self.review_button.configure(text='查看改动')

    def toggle_review(self):
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
        state = (self.result or {}).get('task_state') or {}
        sections = []
        if state.get('plan'):
            sections.append('执行计划\n' + '\n'.join(
                f'{PLAN_STATES.get(step.get("status"), "待处理")} · {step["text"]}'
                for step in state['plan']))
        if state.get('edits'):
            sections.append('任务累计编辑的文件\n' + '\n'.join(row['path'] for row in state['edits']))
        if state.get('failed_writes'):
            sections.append('待完成的文件修改\n' + '\n'.join(state['failed_writes']))
        verification = state.get('verification', {})
        summary = self.verification_summary(state)
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
        sections.append(self.activity[-50000:] or '暂无执行记录。')
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', '\n\n'.join(sections))
        self.detail.configure(state='disabled')

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
            if event.get('run_dir'):
                self.result = {**self.result, 'run_dir': event['run_dir'], 'continuation_available': True}
            if mode in PROJECT_PERMISSION_MODES:
                self.result = {**self.result, 'permission_mode': mode}
                self.append('权限 · ' + PERMISSION_LABELS[mode])
            self.result = apply_task_state(self.result, event.get('task_state'))
            self.status.set(STATES['running'])
            self.show_progress()
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
            self.append(title)
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
        elif kind in ('instructions', 'context', 'resume'):
            self.status.set(title)
            files = event.get('files', [])
            self.append(title + ('\n' + '\n'.join(files) if files else ''))
        elif kind != 'finish':
            self.status.set(title + '…')
        if kind in ('start', 'progress', 'tool_result', 'plan', 'instructions', 'context', 'resume'):
            self.controller.save_card(self)
        self.resize()
        self.controller.refresh_summary()

    def show_approval(self, request):
        if self.app.generation_cancel.is_set() or self.controller.active is not self:
            request['ready'].set()
            return
        self.request = request
        details = request['details']
        self.command_label.configure(text='需要你确认这条命令\n程序：' + str(details['executable']) +
            '\n目录：' + str(details['cwd']) +
            '\n最长 ' + str(details['timeout']) + ' 秒。命令以本机账户运行，可能读写项目外的文件。')
        self.arguments.configure(state='normal')
        self.arguments.delete('1.0', 'end')
        self.arguments.insert('1.0', '完整参数（可滚动和复制）：\n' + json.dumps(details['argv'], ensure_ascii=False))
        self.arguments.configure(state='disabled')
        self.approval_box.pack(fill='x', pady=(8, 0))
        self.status.set('等待确认验证命令' if details.get('purpose') == 'check' else '等待确认命令')
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
            elif allowed and request['details'].get('purpose') == 'check':
                self.status.set(STATES['checking'])
            else:
                self.status.set('命令已批准，等待执行' if allowed else '已跳过命令')
        self.resize()

    def finish(self, result):
        self.decide(False)
        self.result = result
        count = len({row['path'] for row in result.get('changes', [])})
        self.status.set(STATES.get(result.get('status'), '任务结束') + (f' · 修改 {count} 个文件' if count else ''))
        self.show_progress()
        if '[已撤回文件修改]' in self.activity:
            self.status.set('已执行文件撤回 · 展开过程查看结果')
        if self.expanded and result.get('run_dir'):
            self.tools.pack(fill='x', pady=(4, 0))
        self.review_button.configure(state='normal' if result.get('run_dir') else 'disabled')
        self.undo_button.configure(state='normal' if count and '[已撤回文件修改]' not in self.activity else 'disabled')
        self.sync_resume()
        self.resize()

    def show_progress(self):
        result = self.result or {}
        state = result.get('task_state') or {}
        plan = state.get('plan', [])
        lines = []
        if plan:
            complete = sum(step.get('status') == 'completed' for step in plan)
            current = next((step['text'] for step in plan if step.get('status') == 'in_progress'), '')
            lines.append(f'计划进度 {complete}/{len(plan)}' + (' · ' + current if current else ''))
        check_summary = self.verification_summary(state)
        if check_summary:
            lines.append(check_summary)
        if state.get('uncertain') or (result.get('status') == 'interrupted' and state.get('phase') == 'executing'):
            lines.append('上次操作结果仍需核实')
        mode = result.get('permission_mode')
        if mode in ('readonly', 'automatic'):
            lines.append('权限 · ' + PERMISSION_LABELS[mode])
        self.progress.configure(text='\n'.join(lines))
        if lines:
            self.progress.pack(fill='x', pady=(5, 0), after=self.header)
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

    def confirm_recovery(self):
        state = (self.result or {}).get('task_state') or {}
        if self.app.busy or not self.can_resume() or not state.get('uncertain'):
            return
        operations = [TITLES.get(row['tool'], row['tool']) + ' · ' + row['subject']
                      for row in state.get('unresolved', [])]
        details = '\n'.join(operations) or '上次记录未提供具体操作，请查看运行记录。'
        confirmed = messagebox.askyesno('核实中断后继续',
            '待核实的操作\n' + details + '\n\n请先检查相关文件、命令是否已执行及其副作用。'
            '你已核实这些操作的实际结果，确认继续任务吗？未完成的检查仍需执行。',
            parent=self.app.root, default=messagebox.NO)
        if confirmed:
            self.controller.resume(self, recovery_confirmed=True)

    def confirm_undo(self):
        if not self.app.busy:
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
        for button in (self.expand_button, self.resume_button, self.recovery_button, self.undo_button, self.review_button):
            button.command = None
        self.frame.destroy()


class ProjectConversation:
    def __init__(self, app):
        self.app, self.cards, self.active = app, [], None
        self.enabled = tk.BooleanVar(value=False)
        self.workspace = tk.StringVar(value='')
        self.permission_mode = tk.StringVar(value='standard')
        self.menu_permission_mode = tk.StringVar(value='standard')
        self.allow_write = tk.BooleanVar(value=True)
        self.use_memory = tk.BooleanVar(value=False)
        parent = app.composer.body
        self.bar = tk.Frame(parent, bg=C['input'])
        self.bar.pack(fill='x', before=app.input, pady=(0, 5))
        self.chat_button = FlatButton(self.bar, '聊天', lambda: self.set_mode(False), variant='selected', height=30, width=55, font_size=11)
        self.chat_button.pack(side='left')
        self.project_button = FlatButton(self.bar, '做项目', lambda: self.set_mode(True), variant='ghost', height=30, width=69, font_size=11)
        self.project_button.pack(side='left', padx=(3, 0))
        self.folder_button = FlatButton(self.bar, '选择项目文件夹 ▾', self.choose_workspace,
                                        variant='ghost', height=30, width=144, max_width=200, font_size=11)
        self.permissions_button = FlatButton(self.bar, '逐条确认命令 ▾', self.show_permissions,
                                             variant='ghost', height=30, width=124, font_size=11)
        self.permissions_menu = tk.Menu(app.root, tearoff=0, bg=C['surface'], fg=C['text'],
                                        activebackground=C['tint'], activeforeground=C['text'])
        for mode, title in PERMISSION_LABELS.items():
            self.permissions_menu.add_radiobutton(label=title, value=mode, variable=self.menu_permission_mode,
                                                  command=lambda mode=mode: self.set_permission_mode(mode))
        self.permissions_menu.add_separator()
        self.permissions_menu.add_checkbutton(label='允许文件工具修改', variable=self.allow_write,
                                             command=self.save_file_permission)
        self.write_permission_option = self.permissions_menu.index('end')
        self.permissions_menu.add_checkbutton(label='参考经验', variable=self.use_memory, command=self.save_settings)
        self.permissions_menu.add_separator()
        self.permissions_menu.add_command(label='在编辑器中打开项目', command=self.open_project_in_editor)
        self.permissions_menu.add_command(label='在终端中打开项目', command=self.open_project_terminal)
        self.location = label(self.bar, '', size=9, color=C['muted'])
        app.transcript.bind('<Configure>', lambda event: self.resize_cards(), add='+')
        self.bar.bind('<Configure>', lambda event: self.layout_bar())

    def layout_bar(self):
        buttons = (self.chat_button, self.project_button, self.folder_button, self.permissions_button)
        compact = self.bar.winfo_width() < sum(button.winfo_reqwidth() for button in buttons) + 20
        layout = (bool(self.enabled.get()), compact, tuple(button.winfo_reqwidth() for button in buttons))
        if getattr(self, '_bar_layout', None) == layout:
            return
        self._bar_layout = layout
        for button in buttons:
            button.pack_forget()
            button.grid_forget()
        for index in range(4):
            self.bar.columnconfigure(index, weight=0)
        self.chat_button.grid(row=0, column=0, sticky='w')
        self.project_button.grid(row=0, column=1, sticky='w', padx=(3, 0))
        if self.enabled.get():
            if compact:
                self.bar.columnconfigure(1, weight=1)
                self.folder_button.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))
                self.permissions_button.grid(row=1, column=2, sticky='e', pady=(4, 0))
            else:
                self.bar.columnconfigure(2, weight=1)
                self.folder_button.grid(row=0, column=2, sticky='w', padx=(4, 0))
                self.permissions_button.grid(row=0, column=3, sticky='e')

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
        if self.app.busy or not self.enabled.get():
            return
        button = self.permissions_button
        try:
            self.permissions_menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            self.permissions_menu.grab_release()

    def refresh(self):
        enabled = self.enabled.get()
        self.app.input.configure(height=3)
        self.chat_button.configure(variant='ghost' if enabled else 'selected')
        self.project_button.configure(variant='selected' if enabled else 'ghost')
        if enabled:
            mode = self.permission_mode.get()
            name = Path(self.workspace.get()).name or self.workspace.get()
            name = name[:11] + '…' if len(name) > 12 else name
            self.folder_button.configure(text=(name or '选择项目文件夹') + ' ▾')
            self.permissions_button.configure(text=PERMISSION_LABELS[mode] + ' ▾')
            self.location.configure(text='项目：' + (self.workspace.get() or '尚未选择'))
            hints = ['本地处理' if self.app.backend == 'local' else '任务和读取的文件将发往已选 API',
                     PERMISSION_HINTS[mode]]
            if mode == 'automatic':
                hints.append('文件工具限于所选项目')
            if mode != 'readonly':
                hints.append('文件工具修改可撤回' if self.allow_write.get() else '文件工具修改关闭')
            if CONTEXTS[self.app.context.get()] < 16384:
                hints.append('项目使用 16K 记忆容量')
            self.app.chat_hint.set(' · '.join(hints))
            self.app.skill_button.configure(state='disabled')
        else:
            self.app.skill_button.configure(state='normal')
            self.app.chat_hint.set('Enter 发送 · Shift + Enter 换行')
        self.layout_bar()
        self.sync_busy()
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

    def save_settings(self, **overrides):
        if self.app.session_id:
            self.app.sessions.set_project_settings(self.app.session_id, {
                'enabled': self.enabled.get(), 'workspace': self.workspace.get(),
                'allow_write': self.allow_write.get(), 'use_memory': self.use_memory.get(),
                'permission_mode': self.permission_mode.get(), **overrides})

    def choose_workspace(self):
        if self.app.busy:
            return
        folder = filedialog.askdirectory(parent=self.app.root, title='选择这段对话要处理的项目')
        if folder:
            folder = str(Path(folder).absolute())
            if Path(folder) == Path(self.workspace.get()):
                return
            self.save_settings(workspace=folder, permission_mode='standard', allow_write=True)
            self.workspace.set(folder)
            self.apply_permission('standard', True)
            self.refresh()

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
        self.use_memory.set(settings.get('use_memory', False))
        self.refresh()
        rows = self.app.sessions.project_runs(identity) if identity else []
        for row in rows:
            result = row['result']
            if result.get('run_dir') and (result.get('status') in ACTIVE_STATES | {'interrupted'}
                                          or (result.get('task_state') or {}).get('parent_run')):
                result = {**result, 'changes': recorded_run_changes(result['run_dir'], row['workspace'])}
                row['result'] = result
            if result.get('status') not in ACTIVE_STATES:
                continue
            if result.get('run_dir'):
                from agent_task_state import load_task
                try:
                    state = load_task(ARTIFACTS / 'project_runs', result['run_dir'], row['workspace']).export()
                    result = apply_task_state(result, {**state, 'status': 'interrupted'})
                except (OSError, ValueError, RuntimeError):
                    pass
            answer = INTERRUPTED_ANSWER if result.get('continuation_available') else '上次项目任务中断。'
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
        for button in (self.chat_button, self.project_button, self.folder_button, self.permissions_button):
            button.configure(state=state)
        for index in range(len(PERMISSION_LABELS)):
            self.permissions_menu.entryconfigure(index, state=state)
        self.permissions_menu.entryconfigure(self.write_permission_option,
            state='disabled' if self.permission_mode.get() == 'readonly' else state)
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

    def resume(self, card, *, recovery_confirmed=False):
        if self.app.busy or card not in self.cards or not card.can_resume():
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

    def send(self, *, resume_from=None, recovery_confirmed=False):
        app = self.app
        if app.busy:
            return
        draft = app.input.get('1.0', 'end').strip()
        task = draft or (CONTINUE_TASK if resume_from else '')
        folder = self.workspace.get().strip()
        if not task:
            return
        if not folder or not Path(folder).is_dir():
            app.status.set('先选择这段对话的项目文件夹，输入内容会保留。')
            return
        if app.image_path or app.skill_file:
            app.status.set('做项目暂时只处理文字文件。请先在 + 菜单移除图片或 CSV 附件，草稿会保留。')
            return
        local = app.backend == 'local'
        if local:
            from model_catalog import is_chat_model
            if not is_chat_model(app.manager.catalog, app.selected_model) or not app.manager.ready(app.selected_model):
                app.status.set('请先选择已下载的文字模型，或接通 API；草稿会保留。')
                return
            from local_project_model import LocalProjectModel
            model = LocalProjectModel(model_id=app.selected_model)
        else:
            try:
                model = app.make_api_client()
            except Exception:
                app.status.set('API 配置不可用，请在设置里检查；草稿会保留。')
                return
        if app.session_id is None:
            app.session_id = app.sessions.create()
        self.save_settings()
        identity, history = app.session_id, list(app.history)
        workspace = str(Path(folder).absolute())
        previous_workspace = None
        for previous in self.cards:
            index = getattr(previous, 'history_index', previous.assistant_index)
            if index is None or not 0 <= index < len(history) or history[index]['role'] != 'assistant':
                continue
            reply = history[index]
            history[index] = {**reply, 'content': '该项目回复对应的工作区：' + previous.workspace + '\n\n' + reply['content']}
            previous_workspace = previous.workspace
        model_task = task
        if previous_workspace and os.path.normcase(previous_workspace) != os.path.normcase(workspace):
            model_task += '\n\n本轮已切换项目目录。请先重新检查当前工作区的文件。历史项目操作的结果仅适用于其标注的工作区。'
        context = max(16384, CONTEXTS[app.context.get()])
        permission_mode = self.permission_mode.get()
        allow_write, use_memory = self.allow_write.get(), self.use_memory.get()
        personal_options = app.memories.snapshot(app.backend)
        app.sessions.append(identity, 'user', task)
        app.history.append({'role': 'user', 'content': task})
        app.append_heading('你', 'user')
        app.write(app.transcript, task + '\n\n', True)
        app.append_heading('Free AI', 'assistant')
        app.pending_chat, app.streamed = None, ''
        app.last_episode, app.last_tool_result = None, None
        app.input.delete('1.0', 'end')
        app.update_placeholder()
        app.layout_chat()
        app.root.update_idletasks()
        card = ActivityCard(self, workspace)
        initial_result = {'status': 'running', 'answer': '项目任务执行中。', 'run_dir': '',
                          'changes': [], 'continuation_available': False, 'permission_mode': permission_mode}
        card.result = initial_result
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

        def approve(details):
            if permission_mode != 'standard':
                return False
            request = {'details': details, 'ready': threading.Event(), 'allowed': False}
            app.events.put(('update', card.show_approval, request))
            while not request['ready'].wait(.1):
                if app.generation_cancel.is_set():
                    return False
            return request['allowed'] and not app.generation_cancel.is_set()

        def on_event(event):
            if assistant_index is not None and event.get('type') == 'start' and event.get('run_dir'):
                started = {**initial_result, 'run_dir': event['run_dir'], 'continuation_available': True}
                started = apply_task_state(started, event.get('task_state'))
                self.save_reply(identity, assistant_index,
                                started, workspace, '')
            app.events.put(('update', card.event, event))

        def work():
            runner = None
            try:
                from project_agent import ProjectAgent
                runner = ProjectAgent(workspace, model, runs_root=ARTIFACTS / 'project_runs',
                    approve_command=approve, on_event=on_event,
                    cancel_event=app.generation_cancel, allow_write=allow_write, max_steps=16,
                    context=context, knowledge=app.knowledge, use_memory=use_memory,
                    personal_memory=app.personal_memory, read_memory=personal_options['read'],
                    write_memory=personal_options['write'], permission_mode=permission_mode)
                if resume_from:
                    if recovery_confirmed:
                        return runner.run(model_task, history=history, resume_from=resume_from,
                                          recovery_confirmed=True)
                    return runner.run(model_task, history=history, resume_from=resume_from)
                return runner.run(model_task, history=history)
            except Exception:
                logging.exception('Project task failed')
                directory = getattr(runner, 'run_dir', None)
                return {'status': 'failed', 'answer': '项目任务发生错误，请检查目录、运行记录和磁盘空间。已有改动会保留。',
                        'run_dir': str(directory) if directory else '', 'changes': getattr(runner, 'changes', []),
                        'episode_id': None, 'continuation_available': directory is not None}

        def done(result):
            self.active = None
            result = {**card.result, **result}
            memory_notice = app.memories.update_notice(result.get('memory_updates', []))
            if memory_notice:
                result = {**result, 'answer': result['answer'] + memory_notice}
            card.finish(result)
            app.last_answer = result['answer']
            app.last_episode = result.get('episode_id')
            app.history[card.history_index] = {'role': 'assistant', 'content': app.last_answer}
            app.pending_chat = None
            app.write(app.transcript, app.last_answer + '\n\n', True)
            app.format_transcript()
            notice = STATES.get(result.get('status'), '任务结束') + '，可以在这里继续提出下一步。'
            saved = self.save_card(card)
            if card.assistant_index is None:
                try:
                    card.assistant_index = app.sessions.append_project_reply(identity, result, workspace, card.activity)
                    saved = True
                except Exception:
                    logging.exception('Project reply creation failed')
            if not saved:
                notice = '未能保存本轮对话和项目卡片，请保留运行记录文件夹。'
                card.append(notice)
            app.status.set(notice)
            app.refresh_model_state()
            self.sync_busy()
            self.refresh_summary()
        app.submit('项目助手正在处理', work, done, cancellable=True)
        self.sync_busy()

    def undo(self, card):
        if (self.app.busy or not card.result or not card.result.get('run_dir') or not card.result.get('changes')
                or '[已撤回文件修改]' in card.activity):
            return
        from project_agent import rollback_run
        try:
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
        note += '此次撤回覆盖文件工具的修改。'
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
