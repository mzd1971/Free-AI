"""Long-term memories managed beside the current conversation."""
import re
import tkinter as tk
from tkinter import ttk

from memory_retrieval_panel import MemoryRetrievalPanel
from ui_controls import SwitchRow
from ui_kit import COLORS as C, FlatButton, RoundedFrame, ScrollArea, label, ui_font

KINDS = {'偏好': 'preference', '事实': 'fact', '做法': 'procedure', '备忘': 'note'}
SCOPES = {'这个助手': 'agent', '所有助手': 'global', '当前项目': 'project'}
IMPORTANCE = {'普通': 0.5, '重要': 0.7}


class MemoryPanel:
    def __init__(self, app):
        self.app, self.opened, self.identity, self.delete_pending = app, False, None, None
        self._view = 'manage'
        self.rows = []
        self._source_evidence = ''
        self.read_enabled = tk.BooleanVar(value=app.sessions.get('personal_memory_read', True))
        self.auto_enabled = tk.BooleanVar(value=app.sessions.get('personal_memory_auto', True))
        self.remote_enabled = tk.BooleanVar(value=app.sessions.get('personal_memory_remote', False))
        self.mode_state = tk.StringVar()
        self._option_widgets = []
        self.frame = RoundedFrame(app.pages[0], bg=C['surface'], padding=12, radius=14)
        top = tk.Frame(self.frame.body, bg=C['surface'])
        top.pack(fill='x')
        label(top, '长期记忆', size=14, bold=True, color=C.get('brand', C['text'])).pack(side='left')
        FlatButton(top, '×', self.toggle, width=28, height=28, variant='ghost').pack(side='right')
        tabs = tk.Frame(self.frame.body, bg=C['surface'])
        tabs.pack(fill='x', pady=(10, 8))
        self.manage_tab = FlatButton(tabs, '管理', lambda: self._switch_view('manage'), variant='selected',
                                     height=28, font_size=10)
        self.manage_tab.pack(side='left')
        self.retrieval_tab = FlatButton(tabs, '检索测试', lambda: self._switch_view('retrieval'), variant='ghost',
                                        height=28, font_size=10)
        self.retrieval_tab.pack(side='left', padx=(4, 0))
        self.management = tk.Frame(self.frame.body, bg=C['surface'])
        self.management.pack(fill='both', expand=True)
        self.retrieval = MemoryRetrievalPanel(
            self.frame.body, preview=lambda *args, **kwargs: app.personal_memory.preview_call(*args, **kwargs),
            workspace=self.workspace, route=self._retrieval_route,
        )
        footer = tk.Frame(self.management, bg=C['surface'])
        footer.pack(side='bottom', fill='x', pady=(8, 0))
        self.area = ScrollArea(self.management, bg=C['surface'])
        self.area.pack(fill='both', expand=True)
        scroll = ttk.Scrollbar(self.area, orient='vertical', command=self.area.canvas.yview)
        scroll.pack(side='right', fill='y', before=self.area.canvas)
        self.area.canvas.configure(yscrollcommand=scroll.set)
        host = self.area.body
        self._wrapped(host, '长期记忆是单独保存的上下文。AI 可以直接保存，稍后由你核实，无需重新训练。').pack(
            fill='x', pady=(9, 7))
        self._wrapped(host, textvariable=self.mode_state).pack(fill='x', pady=(0, 5))
        self.options = self.build_options(host)
        self._wrapped(host, '开启联网授权后，相关记忆可能随请求发给服务商，自动整理可能产生额外调用费用。').pack(
            fill='x', pady=(5, 9))
        label(host, '当前助手 / 当前项目的记忆', size=10, color=C['muted']).pack(anchor='w', pady=(2, 6))
        self.retention_note = self._wrapped(host, '未核实的 AI 记忆随时间变旧，可能自动归档，暂停检索。原文会保留，可以恢复。你保存、已核实或置顶的记忆不会自动归档。')
        self.retention_note.pack(fill='x', pady=(0, 6))
        self.list_frame = tk.Frame(host, bg=C['surface'])
        self.list_frame.pack(fill='x')
        self.listbox = tk.Listbox(self.list_frame, height=5, width=1, font=ui_font(self.app.root, 10), bg=C['input'], fg=C['text'],
            selectbackground=C['tint'], selectforeground=C['text'], bd=0, highlightthickness=0, exportselection=False)
        list_scroll = ttk.Scrollbar(self.list_frame, orient='vertical', command=self.listbox.yview)
        list_scroll.pack(side='right', fill='y')
        self.listbox.pack(side='left', fill='x', expand=True)
        self.listbox.configure(yscrollcommand=list_scroll.set)
        self.empty_label = self._wrapped(host, '这里会显示你和 AI 保存的记忆。')
        self.empty_label.pack(fill='x', pady=(3, 0))
        self.listbox.bind('<<ListboxSelect>>', self.select)
        tools = tk.Frame(host, bg=C['surface'])
        tools.pack(fill='x', pady=(5, 6))
        FlatButton(tools, '+ 新记忆', self.new, variant='ghost', height=30, font_size=10).pack(side='left')
        self.status_button = FlatButton(tools, '停用', self.change_status, variant='ghost', height=30, width=66, font_size=10)
        self.status_button.pack(side='right')
        self.restore_button = FlatButton(host, '恢复记忆', self.restore, variant='secondary', height=28,
                                         font_size=10, state='disabled')
        self.provenance = self._wrapped(host, '手工填写会作为你定义的记忆。')
        self.provenance.pack(fill='x', pady=(2, 7))
        self.source_actions = tk.Frame(host, bg=C['surface'])
        self.source_actions.pack(fill='x', pady=(0, 5))
        self.verify_button = FlatButton(self.source_actions, '核实这条', self.verify, variant='ghost', height=28,
                                        font_size=10, state='disabled')
        self.verify_button.pack(side='left')
        self.source_button = FlatButton(self.source_actions, '查看来源', self.toggle_source, variant='ghost',
                                        height=28, font_size=10, state='disabled')
        self.source_button.pack(side='left', padx=(5, 0))
        self.source_frame = tk.Frame(host, bg=C['surface'])
        source_view = tk.Frame(self.source_frame, bg=C['input'])
        source_view.pack(fill='x')
        self.source_text = tk.Text(source_view, height=4, width=1, wrap='word', state='disabled',
                                   font=ui_font(self.app.root, 10), bg=C['input'], fg=C['text'], padx=6, pady=4, bd=0)
        source_scroll = ttk.Scrollbar(source_view, orient='vertical', command=self.source_text.yview)
        source_scroll.pack(side='right', fill='y')
        self.source_text.pack(side='left', fill='x', expand=True)
        self.source_text.configure(yscrollcommand=source_scroll.set)
        self.source_copy_button = FlatButton(self.source_frame, '复制来源', self.copy_source, variant='ghost',
                                             height=26, font_size=10)
        self.source_copy_button.pack(anchor='w', pady=(4, 0))
        self.title = tk.StringVar()
        label(host, '记忆名称', size=10).pack(anchor='w')
        self.title_input = ttk.Entry(host, textvariable=self.title)
        self.title_input.pack(fill='x', pady=(4, 8))
        self.content = tk.Text(host, height=5, width=1, wrap='word', font=ui_font(self.app.root, 11), bg=C['input'],
                               fg=C['text'], insertbackground=C['text'], bd=0, padx=7, pady=6)
        self.content.pack(fill='x')
        selectors = tk.Frame(host, bg=C['surface'])
        selectors.pack(fill='x', pady=8)
        self.kind, self.scope = tk.StringVar(value='偏好'), tk.StringVar(value='这个助手')
        ttk.Combobox(selectors, values=list(KINDS), textvariable=self.kind, state='readonly', width=6).pack(side='left')
        ttk.Combobox(selectors, values=list(SCOPES), textvariable=self.scope, state='readonly', width=10).pack(side='right')
        importance_row = tk.Frame(host, bg=C['surface'])
        importance_row.pack(fill='x', pady=(0, 3))
        label(importance_row, '重要性', size=10).pack(side='left', padx=(0, 8))
        self.importance = tk.StringVar(value='普通')
        self.importance_input = ttk.Combobox(importance_row, values=list(IMPORTANCE), textvariable=self.importance,
                                            state='readonly', width=6, font=ui_font(self.app.root, 10))
        self.importance_input.pack(side='left')
        self.importance_input.bind('<<ComboboxSelected>>', self.change_importance)
        self._wrapped(host, '设为重要可提高检索优先级，并延后自动归档。').pack(fill='x', pady=(0, 5))
        self.pin_enabled = tk.BooleanVar()
        self.pin_option = self._switch(host, '每次都参考', self.pin_enabled, self.change_pin)
        self.pin_option.pack(fill='x')
        self.pin_scope_note = self._wrapped(host, '启用且在适用范围内时优先提供，上下文空间不足时可能省略。')
        self.pin_scope_note.pack(fill='x', pady=(0, 5))
        actions = tk.Frame(footer, bg=C['surface'])
        actions.pack(fill='x')
        self.save_button = FlatButton(actions, '保存记忆', self.save, variant='primary', height=32, font_size=10)
        self.save_button.pack(side='left')
        self.delete_button = FlatButton(actions, '删除', self.delete, variant='ghost', height=32, width=100, font_size=10)
        self.delete_button.pack(side='right')
        self.notice = tk.StringVar(value='AI 记忆会标明来源，你可以编辑、停用或删除。')
        self._wrapped(footer, textvariable=self.notice).pack(fill='x', pady=(9, 12))
        self.area.bind_wheel(host)
        self.refresh_mode()

    @staticmethod
    def _wrap(widget):
        widget.bind('<Configure>', lambda event: widget.configure(wraplength=max(60, event.width - 22)))
        return widget

    def _wrapped(self, parent, text='', **options):
        return self._wrap(label(parent, text, size=10, color=C['muted'], anchor='w',
                                wraplength=270, justify='left', **options))

    def _switch(self, parent, text, variable, command):
        return SwitchRow(parent, text, variable, command, font_size=10)

    def build_options(self, parent):
        options = []
        for text, variable in [('使用长期记忆', self.read_enabled), ('允许 AI 自动记忆', self.auto_enabled),
                               ('联网模型也可读写记忆', self.remote_enabled)]:
            check = self._switch(parent, text, variable, self.save_options)
            check.pack(fill='x')
            options.append(check)
        self._option_widgets.extend(options)
        return options

    def workspace(self):
        return self.app.project.workspace.get() if self.app.project.enabled.get() else ''

    def snapshot(self, backend):
        allowed = backend != 'api' or self.remote_enabled.get()
        return {'read': bool(allowed and self.read_enabled.get()), 'write': bool(allowed and self.auto_enabled.get())}

    def _retrieval_route(self, query):
        from app_skills import get_skill
        from clock_context import clock_query_kind

        app = self.app
        can_read = self.snapshot(app.backend)['read']
        skill, tool_mode = app.skill.get(), app.tool_mode.get()
        route, budget = '普通聊天', 1400
        if app.project.enabled.get():
            route, budget = '项目助手', 1800
        elif tool_mode or get_skill(skill)['kind'] == 'tool':
            can_read = False
        elif skill == 'general' and not app.image_path:
            if not app.skill_file and clock_query_kind(query):
                can_read = False
            elif app.project.assistant_enabled.get():
                route, budget = '智能助手', 1800
        message = (f'{route} · 检索预算 {budget} 字节' if can_read else '测试仅本地，当前回答不会读取记忆。')
        return {'budget': budget, 'read_enabled': can_read, 'message': message}

    def refresh_mode(self):
        app = self.app
        if self._view == 'retrieval':
            self.retrieval.sync_route()
        if app.backend == 'api' and not self.remote_enabled.get():
            self.mode_state.set('联网模式 · 未授权长期记忆，读取和自动保存均关闭。')
            return
        mode = '联网模式' if app.backend == 'api' else '本地模式'
        options = self.snapshot(app.backend)
        reading = '开启' if options['read'] else '关闭'
        if app.project.enabled.get():
            from harness_settings import load_preferences
            if load_preferences(app.sessions)['engine'] == 'harness':
                self.mode_state.set(f'Harness（{mode}）· 读取{reading} · 暂不支持自动保存')
                return
        else:
            from app_skills import get_skill
            if app.tool_mode.get() or get_skill(app.skill.get())['kind'] == 'tool':
                self.mode_state.set('当前工具任务不使用长期记忆。')
                return
        writing = '开启' if options['write'] else '关闭'
        self.mode_state.set(f'{mode} · 读取{reading} · 自动保存{writing}')

    def save_options(self):
        for name, variable in [('read', self.read_enabled), ('auto', self.auto_enabled), ('remote', self.remote_enabled)]:
            self.app.sessions.set('personal_memory_' + name, variable.get())
        self.refresh_mode()
        self.notice.set('长期记忆设置已保存。')

    def show(self):
        self.opened = True
        self.app.select_page(0)
        if self.app.pages[0].winfo_width() < 900 and self.app.sidebar.winfo_manager():
            self.app.sidebar.pack_forget()
        self.refresh()
        self.app.layout_chat()

    def toggle(self):
        if self.opened:
            self.opened = False
            self.retrieval.reset()
            self._switch_view('manage')
            self.app.layout_chat()
        else:
            self.show()

    def _switch_view(self, view):
        if self._view == view:
            return
        self._view = view
        testing = view == 'retrieval'
        if testing:
            self.retrieval.clear_results()
        if testing and self.delete_pending:
            self.delete_pending = None
            self.delete_button.configure(text='删除')
            self.notice.set('可继续编辑当前记忆。')
        hidden, shown = (self.management, self.retrieval) if testing else (self.retrieval, self.management)
        hidden.pack_forget()
        shown.pack(fill='both', expand=True)
        self.manage_tab.configure(variant='ghost' if testing else 'selected')
        self.retrieval_tab.configure(variant='selected' if testing else 'ghost')
        self.sync_busy()
        self.refresh()
        if testing:
            self.retrieval.query_input.focus_set()

    def refresh(self):
        self.refresh_mode()
        if not self.opened:
            return
        if self._view == 'retrieval':
            self.sync_busy()
            return
        try:
            workspace = self.workspace()
            if not self.app.busy:
                self.app.personal_memory.maintain(workspace=workspace)
            self.rows = self.app.personal_memory.list(workspace=workspace, include_archived=True)
        except Exception:
            self.notice.set('暂时无法读取记忆，请检查记录文件夹。')
            return
        self.listbox.delete(0, 'end')
        if self.rows:
            self.empty_label.pack_forget()
        else:
            self.empty_label.pack(fill='x', after=self.list_frame, pady=(3, 0))
        for row in self.rows:
            source = '你' if row['source'] == 'user' else 'AI 已核实' if row['verified'] else 'AI 未核实'
            state = '停用 · ' if row['status'] == 'disabled' else '待确认 · ' if row['status'] == 'pending' else ''
            pinned = '置顶 · ' if row['pinned'] else ''
            archived = '已归档 · ' if row['archived'] else ''
            self.listbox.insert('end', f'{archived}{pinned}{state}{source} · {row["title"]}')
        selected = next((index for index, row in enumerate(self.rows) if row['id'] == self.identity), None)
        if selected is not None:
            self.listbox.selection_set(selected)
            self.listbox.see(selected)
            self._show_provenance(self.rows[selected])
        elif self.identity:
            self.new()
        self.sync_busy()

    def sync_busy(self):
        state = 'disabled' if self.app.busy else 'normal'
        for widget in (*self._option_widgets, self.pin_option, self.status_button):
            widget.configure(state=state)
        editor_state = 'disabled' if self._view == 'retrieval' else state
        self.save_button.configure(state=editor_state)
        self.delete_button.configure(state=editor_state)
        self.retrieval.sync_busy(self.app.busy)
        self.importance_input.configure(state='disabled' if self.app.busy else 'readonly')
        selected = next((row for row in self.rows if row['id'] == self.identity), None)
        verify = 'normal' if not self.app.busy and selected and not selected['verified'] else 'disabled'
        self.verify_button.configure(state=verify)
        restore = 'normal' if not self.app.busy and selected and selected['archived'] else 'disabled'
        self.restore_button.configure(state=restore)

    def new(self):
        self.identity, self.delete_pending = None, None
        self.title.set('')
        self.content.delete('1.0', 'end')
        self.kind.set('偏好')
        self.scope.set('当前项目' if self.workspace() else '这个助手')
        self.importance.set('普通')
        self.restore_button.pack_forget()
        self.restore_button.configure(state='disabled')
        self.listbox.selection_clear(0, 'end')
        self.provenance.configure(text='手工填写会作为你定义的记忆。')
        self.pin_enabled.set(False)
        self.verify_button.configure(text='核实这条', state='disabled')
        self._source_evidence = ''
        self.close_source()
        self.source_button.configure(state='disabled')
        self.delete_button.configure(text='删除')
        self.notice.set('例如：回答用中文，先给结论；或填写这个项目的长期约定。')

    def select(self, event=None):
        selection = self.listbox.curselection()
        if not selection:
            return
        row = self.rows[selection[0]]
        if row['id'] != self.identity:
            self.close_source()
        self.identity, self.delete_pending = row['id'], None
        self.title.set(row['title'])
        self.content.delete('1.0', 'end')
        self.content.insert('1.0', row['content'])
        self.kind.set(next(name for name, value in KINDS.items() if value == row['kind']))
        self.scope.set(next(name for name, value in SCOPES.items() if value == row['scope']))
        self._show_provenance(row)
        self.delete_button.configure(text='删除')
        self.notice.set('修改后保存，会成为你定义的记忆；AI 不能覆盖它。')
        self.sync_busy()

    def _show_provenance(self, row):
        source = ('你定义的记忆' if row['source'] == 'user'
                  else 'AI 记下 · 已核实' if row['verified'] else 'AI 记下 · 尚未核实')
        evidence = row['evidence']
        self._source_evidence = evidence
        self.source_button.configure(state='normal' if evidence else 'disabled')
        if not evidence:
            self.close_source()
        elif self.source_frame.winfo_manager():
            self._write_source()
        origin = ('聊天对话' if evidence.startswith(('对话', '用户原话')) else
                  '项目任务' if evidence.startswith('项目运行') else evidence[:60])
        state = '\n已归档，暂不参与自动检索。' if row['archived'] else ''
        self.provenance.configure(text=source + ('\n来源：' + origin if origin else '') + state)
        if row['archived']:
            self.restore_button.pack(anchor='w', after=self.provenance, pady=(0, 5))
        else:
            self.restore_button.pack_forget()
        self.status_button.configure(text='启用' if row['status'] != 'active' else '停用')
        self.pin_enabled.set(row['pinned'])
        self.importance.set('重要' if row['importance'] >= IMPORTANCE['重要'] else '普通')
        self.verify_button.configure(text='已核实' if row['verified'] else '核实这条')

    def _write_source(self):
        self.source_text.configure(state='normal')
        self.source_text.delete('1.0', 'end')
        self.source_text.insert('1.0', self._source_evidence)
        self.source_text.configure(state='disabled')

    def close_source(self):
        self.source_frame.pack_forget()
        self.source_button.configure(text='查看来源')

    def toggle_source(self):
        if self.source_frame.winfo_manager():
            self.close_source()
        elif self._source_evidence:
            self._write_source()
            self.source_frame.pack(fill='x', after=self.source_actions, pady=(0, 6))
            self.source_button.configure(text='收起来源')

    def copy_source(self):
        if not self._source_evidence:
            return
        try:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(self._source_evidence)
        except tk.TclError:
            self.notice.set('无法复制来源，请选中文本后重试。')
        else:
            self.notice.set('来源已复制。')

    def change_pin(self):
        if self.app.busy or not self.identity:
            return
        try:
            row = self.app.personal_memory.set_pinned(self.identity, self.pin_enabled.get())
        except Exception:
            self.pin_enabled.set(not self.pin_enabled.get())
            self.notice.set('无法更新置顶设置。')
            return
        self.refresh()
        self.notice.set('已置顶。' if row['pinned'] else '已取消置顶。')

    def change_importance(self, event=None):
        if self.app.busy or not self.identity:
            return
        importance = self.importance.get()
        try:
            self.app.personal_memory.set_importance(self.identity, IMPORTANCE[importance])
        except Exception:
            self.refresh()
            self.notice.set('无法更新记忆重要性。')
            return
        self.refresh()
        self.notice.set(f'记忆已设为{importance}。')

    def restore(self):
        if self.app.busy or not self.identity:
            return
        try:
            row = self.app.personal_memory.restore(self.identity)
        except Exception:
            self.notice.set('无法恢复这条记忆。')
            return
        self.refresh()
        if row['status'] == 'active':
            self.notice.set('记忆已恢复，可在适用范围内参与自动检索。')
        else:
            self.notice.set('已解除归档。启用这条记忆后，它才会参与自动检索。')

    def verify(self):
        if self.app.busy or not self.identity:
            return
        try:
            row = self.app.personal_memory.get(self.identity)
            if (self.title.get() != row['title'] or self.content.get('1.0', 'end-1c') != row['content']
                    or KINDS[self.kind.get()] != row['kind'] or SCOPES[self.scope.get()] != row['scope']):
                self.notice.set('请先保存修改，再核实这条记忆。')
                return
            self.app.personal_memory.verify(self.identity)
        except Exception:
            self.notice.set('无法保存核实状态。')
            return
        self.refresh()
        self.notice.set('已核实并保留 AI 来源，不再被自动覆盖。')

    def save(self):
        if self.app.busy or self._view == 'retrieval':
            return
        try:
            row = self.app.personal_memory.save_user(self.title.get(), self.content.get('1.0', 'end-1c'),
                kind=KINDS[self.kind.get()], scope=SCOPES[self.scope.get()], workspace=self.workspace(),
                memory_id=self.identity, pinned=self.pin_enabled.get(),
                importance=None if self.identity else IMPORTANCE[self.importance.get()])
            self.identity = row['id']
            self.refresh()
            self.select()
            self.notice.set('已保存，从下一次回答起可使用，不需要重新训练。')
        except ValueError as error:
            self.notice.set(str(error))
        except Exception:
            self.notice.set('未能保存，请检查记录文件夹和磁盘空间。')

    def change_status(self):
        if self.app.busy or not self.identity:
            return
        try:
            row = self.app.personal_memory.get(self.identity)
            status = 'disabled' if row['status'] == 'active' else 'active'
            row = self.app.personal_memory.set_status(self.identity, status)
            self.refresh()
            self.select()
            if status == 'disabled':
                self.notice.set('已停用，后续不会再检索这条记忆。')
            elif row['archived']:
                self.notice.set('已启用。恢复这条记忆后，它才会参与自动检索。')
            else:
                self.notice.set('已启用。')
        except Exception:
            self.notice.set('未能更新记忆状态。')

    def delete(self):
        if self.app.busy or self._view == 'retrieval' or not self.identity:
            return
        if self.delete_pending != self.identity:
            self.delete_pending = self.identity
            self.delete_button.configure(text='确认删除')
            self.notice.set('再点一次确认删除。记忆原文会移除，已有聊天内容不会自动擦除。')
            return
        try:
            self.app.personal_memory.delete(self.identity)
            self.new()
            self.refresh()
            self.notice.set('记忆已删除，后续不会再检索。')
        except Exception:
            self.notice.set('未能删除这条记忆。')

    @staticmethod
    def read_notice(memory_ids):
        count = len({identity for identity in memory_ids if isinstance(identity, str) and identity})
        return f'\n\n[长期记忆] 本轮向模型提供了 {count} 条记忆作为参考。' if count else ''

    def update_notice(self, updates):
        if not updates:
            return ''
        self.refresh()
        titles = '、'.join(row['title'] for row in updates)
        return '\n\n[长期记忆] AI 已记下：' + titles + '。可在“长期记忆”查看并核实。'

    def chat_notice(self, result, message, *, backend=None):
        saved = self.update_notice(result.get('memory_updates', []))
        parts = [self.read_notice(result.get('memory_ids', [])), saved]
        status = result.get('memory_status')
        warning = result.get('memory_warning')
        if status == 'cancelled':
            parts.append('\n\n[长期记忆] 已停止自动整理。回答和已保存的记录仍保留。')
        if warning:
            parts.append('\n\n[长期记忆] ' + warning + ' 回答已保留，可查看或手工保存记忆。')
        elif status == 'failed':
            parts.append('\n\n[长期记忆] 自动整理失败。回答已保留，可以手工保存。')
        requested = re.search(r'记住|记下|记牢|(?:保存|添加|写入|加入).{0,10}记忆', message)
        if requested and not saved and not warning and status not in ('cancelled', 'failed'):
            if status or result.get('memory_attempted'):
                detail = '本轮没有新增记忆。可查看已有记录，或手工保存。'
            elif backend == 'api' and not self.remote_enabled.get():
                detail = '联网模式尚未获准读写长期记忆。可在“长期记忆”中授权或手工保存。'
            else:
                detail = '本轮未启用自动记忆。可在“长期记忆”中开启或手工保存。'
            parts.append('\n\n[长期记忆] ' + detail)
        return ''.join(parts)
