"""Project work embedded in the existing conversation and composer."""
import json
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from app_state import CONTEXTS
from paths import ARTIFACTS
from ui_kit import COLORS as C, FONT, FlatButton, label

TITLES = {'remember_memory': '保存长期记忆', 'list_files': '查看项目', 'read_file': '读取文件', 'write_file': '写入文件',
          'replace_text': '修改文件', 'run_command': '运行命令', 'update_plan': '整理计划'}
STATES = {'completed': '本轮已结束', 'stopped': '已停止', 'limit': '达到本轮上限', 'failed': '未完成'}
COMMAND_STATES = {'denied': '已跳过', 'cancelled': '已停止', 'timeout': '已超时'}


class ActivityCard:
    def __init__(self, controller, workspace, record=None):
        self.controller, self.app = controller, controller.app
        self.workspace, self.result, self.assistant_index = workspace, None, None
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
        self.detail = tk.Text(self.inner, bg=C['surface'], fg=C['text'], height=8, width=1,
                              wrap='word', font=(FONT, 10), bd=0, padx=9, pady=8, state='disabled')
        self.approval_box = tk.Frame(self.inner, bg=C['tint'], padx=10, pady=9)
        self.command_label = label(self.approval_box, '', size=10, anchor='w', justify='left')
        self.command_label.pack(fill='x')
        arguments_box = tk.Frame(self.approval_box, bg=C['surface'])
        arguments_box.pack(fill='x', pady=(6, 0))
        self.arguments = tk.Text(arguments_box, height=4, width=1, wrap='word',
            bg=C['surface'], fg=C['text'], bd=0, padx=7, pady=6, font=(FONT, 10), state='disabled')
        scroll = tk.Scrollbar(arguments_box, command=self.arguments.yview)
        scroll.pack(side='right', fill='y')
        self.arguments.configure(yscrollcommand=scroll.set)
        self.arguments.pack(side='left', fill='x', expand=True)
        actions = tk.Frame(self.approval_box, bg=C['tint'])
        actions.pack(fill='x', pady=(7, 0))
        FlatButton(actions, '执行这条命令', lambda: self.decide(True), variant='primary', height=32, font_size=10).pack(side='left')
        FlatButton(actions, '跳过', lambda: self.decide(False), variant='ghost', height=32, font_size=10).pack(side='left', padx=10)
        self.tools = tk.Frame(self.inner, bg=C['bg'])
        self.undo_button = FlatButton(self.tools, '撤回文件修改', self.confirm_undo, variant='ghost', height=30, font_size=10)
        self.undo_button.pack(side='left')
        FlatButton(self.tools, '打开记录文件夹', self.open_records, variant='ghost', height=30, font_size=10).pack(side='left', padx=6)
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
            self.detail.pack(fill='x', pady=(8, 0), after=self.header)
            if self.result and self.result.get('run_dir'):
                self.tools.pack(fill='x', pady=(4, 0))
        else:
            self.detail.pack_forget()
            self.tools.pack_forget()
            self.cancel_undo()
        self.expand_button.configure(text='收起 ▴' if self.expanded else '详情 ▾')
        self.resize()

    def refresh_detail(self):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', self.activity[-50000:] or '暂无执行记录。')
        self.detail.see('end')
        self.detail.configure(state='disabled')

    def append(self, text):
        self.activity = (self.activity + text + '\n').encode('utf-8')[-280000:].decode('utf-8', errors='ignore')
        if self.expanded:
            self.refresh_detail()

    def event(self, event):
        kind, tool = event.get('type'), event.get('tool', '')
        title = TITLES.get(tool, event.get('message', '正在处理'))
        if kind == 'tool_result':
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
        elif kind == 'plan':
            self.append('计划：\n' + '\n'.join(event.get('steps', [])))
            self.status.set('已整理执行计划')
        elif kind != 'finish':
            self.status.set(title + '…')
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
        self.status.set('等待确认命令')
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
            else:
                self.status.set('命令已批准，等待执行' if allowed else '已跳过命令')
        self.resize()

    def finish(self, result):
        self.decide(False)
        self.result = result
        count = len({row['path'] for row in result.get('changes', [])})
        self.status.set(STATES.get(result.get('status'), '任务结束') + (f' · 修改 {count} 个文件' if count else ''))
        if '[已撤回文件修改]' in self.activity:
            self.status.set('已执行文件撤回 · 展开过程查看结果')
        if self.expanded and result.get('run_dir'):
            self.tools.pack(fill='x', pady=(4, 0))
        self.undo_button.configure(state='normal' if count and '[已撤回文件修改]' not in self.activity else 'disabled')
        self.resize()

    def confirm_undo(self):
        if not self.app.busy:
            if not self.expanded:
                self.toggle()
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


class ProjectConversation:
    def __init__(self, app):
        self.app, self.cards, self.active = app, [], None
        self.enabled = tk.BooleanVar(value=False)
        self.workspace = tk.StringVar(value='')
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
                                        variant='ghost', height=30, width=162, font_size=11)
        self.permissions_button = FlatButton(self.bar, '权限 ▾', self.show_permissions,
                                             variant='ghost', height=30, width=74, font_size=11)
        self.permissions_menu = tk.Menu(app.root, tearoff=0, bg=C['surface'], fg=C['text'],
                                        activebackground=C['tint'], activeforeground=C['text'])
        self.permissions_menu.add_checkbutton(label='允许改文件', variable=self.allow_write, command=self.save_settings)
        self.permissions_menu.add_checkbutton(label='参考经验', variable=self.use_memory, command=self.save_settings)
        self.location = label(self.bar, '', size=9, color=C['muted'])
        app.transcript.bind('<Configure>', lambda event: self.resize_cards(), add='+')

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
        self.app.input.configure(height=2)
        self.chat_button.configure(variant='ghost' if enabled else 'selected')
        self.project_button.configure(variant='selected' if enabled else 'ghost')
        if enabled:
            name = Path(self.workspace.get()).name or self.workspace.get()
            name = name[:11] + '…' if len(name) > 12 else name
            self.folder_button.configure(text=(name or '选择项目文件夹') + ' ▾')
            self.folder_button.pack(side='left', padx=(4, 0))
            self.permissions_button.pack(side='right')
            self.location.configure(text='项目：' + (self.workspace.get() or '尚未选择'))
            self.app.chat_hint.set(('本地处理' if self.app.backend == 'local' else '任务和读取的文件将发往已选 API') +
                ' · 命令逐条确认 · 文件工具修改可撤回' +
                (' · 项目使用 16K 记忆容量' if CONTEXTS[self.app.context.get()] < 16384 else ''))
            self.app.skill_button.configure(state='disabled')
        else:
            self.folder_button.pack_forget()
            self.permissions_button.pack_forget()
            self.app.skill_button.configure(state='normal')
            self.app.chat_hint.set('Enter 发送 · Shift + Enter 换行')
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

    def save_settings(self):
        if self.app.session_id:
            self.app.sessions.set_project_settings(self.app.session_id, {
                'enabled': self.enabled.get(), 'workspace': self.workspace.get(),
                'allow_write': self.allow_write.get(), 'use_memory': self.use_memory.get()})

    def choose_workspace(self):
        if self.app.busy:
            return
        folder = filedialog.askdirectory(parent=self.app.root, title='选择这段对话要处理的项目')
        if folder:
            self.workspace.set(str(Path(folder).absolute()))
            self.save_settings()
            self.refresh()

    def load(self, identity):
        for card in self.cards:
            card.frame.destroy()
        self.cards, self.active = [], None
        settings = self.app.sessions.project_settings(identity) if identity else {}
        self.enabled.set(settings.get('enabled', False))
        self.workspace.set(settings.get('workspace', ''))
        self.allow_write.set(settings.get('allow_write', True))
        self.use_memory.set(settings.get('use_memory', False))
        self.refresh()
        return {row['assistant_index']: row for row in self.app.sessions.project_runs(identity)} if identity else {}

    def restore(self, record):
        card = ActivityCard(self, record['workspace'], record)
        self.cards.append(card)
        self.refresh_summary()
        return card

    def sync_busy(self):
        state = 'disabled' if self.app.busy else 'normal'
        for button in (self.chat_button, self.project_button, self.folder_button, self.permissions_button):
            button.configure(state=state)
        if self.enabled.get():
            self.app.skill_button.configure(state='disabled')

    def send(self):
        app = self.app
        if app.busy:
            return
        task = app.input.get('1.0', 'end').strip()
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
        allow_write, use_memory = self.allow_write.get(), self.use_memory.get()
        personal_options = app.memories.snapshot(app.backend)
        app.sessions.append(identity, 'user', task)
        app.history.append({'role': 'user', 'content': task})
        app.append_heading('你', 'user')
        app.write(app.transcript, task + '\n\n', True)
        app.append_heading('Free AI', 'assistant')
        app.pending_chat, app.streamed = identity, ''
        app.last_episode, app.last_tool_result = None, None
        app.input.delete('1.0', 'end')
        app.update_placeholder()
        app.layout_chat()
        app.root.update_idletasks()
        card = ActivityCard(self, workspace)
        self.cards.append(card)
        self.active = card
        self.refresh_summary()
        app.refresh_recent()

        def approve(details):
            request = {'details': details, 'ready': threading.Event(), 'allowed': False}
            app.events.put(('update', card.show_approval, request))
            while not request['ready'].wait(.1):
                if app.generation_cancel.is_set():
                    return False
            return request['allowed'] and not app.generation_cancel.is_set()

        def work():
            from project_agent import ProjectAgent
            runner = None
            try:
                runner = ProjectAgent(workspace, model, runs_root=ARTIFACTS / 'project_runs',
                    approve_command=approve, on_event=lambda event: app.events.put(('update', card.event, event)),
                    cancel_event=app.generation_cancel, allow_write=allow_write, max_steps=16,
                    context=context, knowledge=app.knowledge, use_memory=use_memory,
                    personal_memory=app.personal_memory, read_memory=personal_options['read'],
                    write_memory=personal_options['write'])
                return runner.run(model_task, history=history)
            except Exception:
                directory = getattr(runner, 'run_dir', None)
                return {'status': 'failed', 'answer': '项目任务发生错误，请检查目录、运行记录和磁盘空间。已有改动会保留。',
                        'run_dir': str(directory) if directory else '', 'changes': getattr(runner, 'changes', []), 'episode_id': None}

        def done(result):
            self.active = None
            memory_notice = app.memories.update_notice(result.get('memory_updates', []))
            if memory_notice:
                result = {**result, 'answer': result['answer'] + memory_notice}
            card.finish(result)
            app.last_answer = result['answer']
            app.last_episode = result.get('episode_id')
            card.history_index = len(app.history)
            app.history.append({'role': 'assistant', 'content': app.last_answer})
            app.pending_chat = None
            app.write(app.transcript, app.last_answer + '\n\n', True)
            app.format_transcript()
            notice = STATES.get(result.get('status'), '任务结束') + '，可以在这里继续提出下一步。'
            try:
                card.assistant_index = app.sessions.append_project_reply(identity, result, workspace, card.activity)
            except Exception:
                notice = '未能保存本轮对话和项目卡片，请保留运行记录文件夹。'
                card.append(notice)
            app.status.set(notice)
            app.refresh_model_state()
            self.sync_busy()
        app.submit('项目助手正在处理', work, done, cancellable=True)
        self.sync_busy()

    def undo(self, card):
        if (self.app.busy or not card.result or not card.result.get('run_dir')
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
        card.status.set('已恢复 ' + str(len(outcome['restored'])) + ' 个文件；跳过 ' + str(len(outcome['conflicts'])) + ' 个后来有改动的文件')
        restored, conflicts = outcome['restored'], outcome['conflicts']
        note = '用户已执行文件撤回。已恢复文件：' + ('、'.join(restored) or '无') + '。'
        if conflicts:
            note += '跳过文件：' + '、'.join(conflicts) + '。'
        note += '此次撤回覆盖文件工具的修改。'
        app = self.app
        index = getattr(card, 'history_index', card.assistant_index)
        previous = app.history[index]['content']
        content = previous + '\n\n' + note
        try:
            content = app.sessions.annotate_project_reply(app.session_id, card.assistant_index, note, card.activity)
        except Exception:
            notice = '文件撤回已执行，但撤回说明未能保存到会话。'
            app.status.set(notice)
            card.append(notice)
        app.history[index] = {'role': 'assistant', 'content': content}
        card.result = {**card.result, 'answer': content}
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
