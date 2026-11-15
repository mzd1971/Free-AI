"""Long-term memories managed beside the current conversation."""
import re
import tkinter as tk
from tkinter import ttk

from ui_kit import COLORS as C, FONT, FlatButton, RoundedFrame, ScrollArea, label, ui_font

KINDS = {'偏好': 'preference', '事实': 'fact', '做法': 'procedure', '备忘': 'note'}
SCOPES = {'这个助手': 'agent', '所有助手': 'global', '当前项目': 'project'}


class MemoryPanel:
    def __init__(self, app):
        self.app, self.opened, self.identity, self.delete_pending = app, False, None, None
        self.rows = []
        self.read_enabled = tk.BooleanVar(value=app.sessions.get('personal_memory_read', True))
        self.auto_enabled = tk.BooleanVar(value=app.sessions.get('personal_memory_auto', True))
        self.remote_enabled = tk.BooleanVar(value=app.sessions.get('personal_memory_remote', False))
        self.frame = RoundedFrame(app.pages[0], bg=C['surface'], padding=12, radius=14)
        top = tk.Frame(self.frame.body, bg=C['surface'])
        top.pack(fill='x')
        label(top, '长期记忆', size=14, bold=True, color=C.get('brand', C['text'])).pack(side='left')
        FlatButton(top, '×', self.toggle, width=28, height=28, variant='ghost').pack(side='right')
        footer = tk.Frame(self.frame.body, bg=C['surface'])
        footer.pack(side='bottom', fill='x', pady=(8, 0))
        self.area = ScrollArea(self.frame.body, bg=C['surface'])
        self.area.pack(fill='both', expand=True)
        scroll = ttk.Scrollbar(self.area, orient='vertical', command=self.area.canvas.yview)
        scroll.pack(side='right', fill='y', before=self.area.canvas)
        self.area.canvas.configure(yscrollcommand=scroll.set)
        host = self.area.body
        label(host, '在聊天和做项目时使用，无需训练。', size=10, color=C['muted'], wraplength=270).pack(anchor='w', pady=(9, 7))
        self.options = []
        for text, variable in [('使用长期记忆', self.read_enabled), ('允许 AI 自动记忆', self.auto_enabled),
                               ('联网模型也可读写记忆', self.remote_enabled)]:
            check = tk.Checkbutton(host, text=text, variable=variable, command=self.save_options,
                bg=C['surface'], fg=C['text'], activebackground=C['surface'], activeforeground=C['text'],
                selectcolor=C['input'], font=ui_font(self.app.root, 10), anchor='w', bd=0)
            check.pack(fill='x')
            self.options.append(check)
        label(host, '自动记忆会在回答后再整理一次；联网整理可能产生额外调用费用。',
              size=9, color=C['muted'], wraplength=270, justify='left').pack(anchor='w', pady=(5, 9))
        label(host, '当前助手 / 当前项目的记忆', size=10, color=C['muted']).pack(anchor='w', pady=(2, 6))
        self.listbox = tk.Listbox(host, height=5, font=ui_font(self.app.root, 10), bg=C['input'], fg=C['text'],
            selectbackground=C['tint'], selectforeground=C['text'], bd=0, highlightthickness=0, exportselection=False)
        self.listbox.pack(fill='x')
        self.listbox.bind('<<ListboxSelect>>', self.select)
        tools = tk.Frame(host, bg=C['surface'])
        tools.pack(fill='x', pady=(5, 6))
        FlatButton(tools, '+ 新记忆', self.new, variant='ghost', height=30, font_size=10).pack(side='left')
        self.status_button = FlatButton(tools, '停用', self.change_status, variant='ghost', height=30, width=66, font_size=10)
        self.status_button.pack(side='right')
        self.provenance = label(host, '手工填写会作为你定义的记忆。', size=9, color=C['muted'], wraplength=270, justify='left')
        self.provenance.pack(anchor='w', pady=(2, 7))
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
        actions = tk.Frame(footer, bg=C['surface'])
        actions.pack(fill='x')
        self.save_button = FlatButton(actions, '保存记忆', self.save, variant='primary', height=32, font_size=10)
        self.save_button.pack(side='left')
        self.delete_button = FlatButton(actions, '删除', self.delete, variant='ghost', height=32, width=100, font_size=10)
        self.delete_button.pack(side='right')
        self.notice = tk.StringVar(value='AI 记忆会标明来源，你可以编辑、停用或删除。')
        label(footer, textvariable=self.notice, size=10, color=C['muted'], wraplength=270,
              justify='left').pack(anchor='w', pady=(9, 12))
        self.area.bind_wheel(host)

    def workspace(self):
        return self.app.project.workspace.get() if self.app.project.enabled.get() else ''

    def snapshot(self, backend):
        allowed = backend != 'api' or self.remote_enabled.get()
        return {'read': bool(allowed and self.read_enabled.get()), 'write': bool(allowed and self.auto_enabled.get())}

    def save_options(self):
        for name, variable in [('read', self.read_enabled), ('auto', self.auto_enabled), ('remote', self.remote_enabled)]:
            self.app.sessions.set('personal_memory_' + name, variable.get())
        self.notice.set('已保存。联网开关只控制长期记忆，聊天本身仍由当前模型设置决定。')

    def toggle(self):
        self.opened = not self.opened
        if self.opened:
            self.app.select_page(0)
            if self.app.pages[0].winfo_width() < 900 and self.app.sidebar.winfo_manager():
                self.app.sidebar.pack_forget()
            self.refresh()
        self.app.layout_chat()

    def refresh(self):
        if not self.opened:
            return
        try:
            self.rows = self.app.personal_memory.list(workspace=self.workspace())
        except Exception:
            self.notice.set('暂时无法读取记忆，请检查记录文件夹。')
            return
        self.listbox.delete(0, 'end')
        for row in self.rows:
            source = '你' if row['source'] == 'user' else 'AI'
            state = ' · 停用' if row['status'] == 'disabled' else ' · 待确认' if row['status'] == 'pending' else ''
            self.listbox.insert('end', f'{source} · {row["title"]}{state}')
        selected = next((index for index, row in enumerate(self.rows) if row['id'] == self.identity), None)
        if selected is not None:
            self.listbox.selection_set(selected)
        elif self.identity:
            self.new()
        self.sync_busy()

    def sync_busy(self):
        state = 'disabled' if self.app.busy else 'normal'
        for widget in (*self.options, self.save_button, self.delete_button, self.status_button):
            widget.configure(state=state)

    def new(self):
        self.identity, self.delete_pending = None, None
        self.title.set('')
        self.content.delete('1.0', 'end')
        self.kind.set('偏好')
        self.scope.set('当前项目' if self.workspace() else '这个助手')
        self.listbox.selection_clear(0, 'end')
        self.provenance.configure(text='手工填写会作为你定义的记忆。')
        self.delete_button.configure(text='删除')
        self.notice.set('例如：回答用中文，先给结论；或填写这个项目的长期约定。')

    def select(self, event=None):
        selection = self.listbox.curselection()
        if not selection:
            return
        row = self.rows[selection[0]]
        self.identity, self.delete_pending = row['id'], None
        self.title.set(row['title'])
        self.content.delete('1.0', 'end')
        self.content.insert('1.0', row['content'])
        self.kind.set(next(name for name, value in KINDS.items() if value == row['kind']))
        self.scope.set(next(name for name, value in SCOPES.items() if value == row['scope']))
        source = '你定义的记忆' if row['source'] == 'user' else 'AI 记下 · 尚未核实'
        evidence = row['evidence']
        origin = ('聊天对话' if evidence.startswith(('对话', '用户原话')) else
                  '项目任务' if evidence.startswith('项目运行') else evidence[:60])
        self.provenance.configure(text=source + ('\n来源：' + origin if origin else ''))
        self.status_button.configure(text='启用' if row['status'] != 'active' else '停用')
        self.delete_button.configure(text='删除')
        self.notice.set('修改后保存，会成为你定义的记忆；AI 不能覆盖它。')

    def save(self):
        if self.app.busy:
            return
        try:
            row = self.app.personal_memory.save_user(self.title.get(), self.content.get('1.0', 'end-1c'),
                kind=KINDS[self.kind.get()], scope=SCOPES[self.scope.get()], workspace=self.workspace(), memory_id=self.identity)
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
            self.app.personal_memory.set_status(self.identity, status)
            self.refresh()
            self.select()
            self.notice.set('已停用，后续不会再检索这条记忆。' if status == 'disabled' else '已启用。')
        except Exception:
            self.notice.set('未能更新记忆状态。')

    def delete(self):
        if self.app.busy or not self.identity:
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

    def update_notice(self, updates):
        if not updates:
            return ''
        self.refresh()
        titles = '、'.join(row['title'] for row in updates)
        return '\n\n[长期记忆] AI 已记下：' + titles + '。这些记录尚未核实，可点“记忆”修改或删除。'

    def chat_notice(self, result, message):
        notice = self.update_notice(result.get('memory_updates', []))
        if notice:
            return notice
        if result.get('memory_warning'):
            return '\n\n[长期记忆] ' + result['memory_warning'] + ' 回答已保留，可点“记忆”查看已有记录或手工保存。'
        # A model saying "remembered" is not evidence of a database write.
        requested = re.search(r'记住|记下|记牢|(?:保存|添加|写入|加入).{0,10}记忆', message)
        if not requested:
            return ''
        if 'memory_status' in result or result.get('memory_attempted'):
            return '\n\n[长期记忆] 本轮没有新增记忆。可点“记忆”查看已有记录或手工保存；以记忆列表为准。'
        return '\n\n[长期记忆] 本轮未启用自动记忆。可点“记忆”开启或手工保存；仅在回答中承诺记住不会写入长期记忆。'
