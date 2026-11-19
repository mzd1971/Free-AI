"""Project execution settings in the native coding-tools page."""
import sqlite3
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser

import harness_settings
from ui_kit import COLORS as C, FlatButton, label, ui_font


ENGINES = {'Free AI 内置（默认）': 'builtin', 'DeepSeek Harness（可选）': 'harness'}
OFFICIAL_GUIDE = 'https://deepseek-harness.github.io/deepseek-harness/en/guide/python-sdk'


class HarnessPanel(tk.Frame):
    def __init__(self, app, parent, description):
        super().__init__(parent, bg=C['surface'])
        self.app = app
        prefs = harness_settings.load_preferences(app.sessions)
        self.engine = tk.StringVar(self, next(title for title, value in ENGINES.items() if value == prefs['engine']))
        self.executable = tk.StringVar(self, prefs['executable'] or str(harness_settings.default_executable()))
        self.notice = tk.StringVar(self)

        self.selector = ttk.Combobox(self, textvariable=self.engine, values=list(ENGINES), state='readonly',
                                     font=ui_font(app.root, 11))
        self.selector.pack(fill='x', pady=(8, 2))
        description(self, '新任务使用保存的执行方式，续跑沿用原任务的执行方式。').pack(fill='x')
        self.options = tk.Frame(self, bg=C['surface'])
        description(self.options, 'DeepSeek Harness 是实验选项，沿用聊天中选择的模型或 API。组件约下载 87 MB、占用 153 MB，'
                    '只在点击安装后下载，数据留在 Free AI。标准模式每次工具操作都需确认，自动模式直接执行，均使用当前 Windows 账户权限。'
                    '执行结果需验收；聊天任务卡支持项目文本快照与检查，暂未接通自动读写长期记忆。').pack(fill='x', pady=(6, 5))
        label(self.options, '执行组件路径（dsh.exe 或 SDK 运行程序）', size=10).pack(anchor='w')
        row = tk.Frame(self.options, bg=C['surface'])
        row.pack(fill='x')
        self.entry = ttk.Entry(row, textvariable=self.executable, width=16)
        self.entry.pack(side='left', fill='x', expand=True)
        self.browse = FlatButton(row, '浏览组件', self.choose_executable, width=88, height=30, font_size=10)
        self.browse.pack(side='right', padx=(6, 0))

        actions = tk.Frame(self.options, bg=C['surface'])
        actions.pack(fill='x', pady=(8, 0))
        self.check = FlatButton(actions, '检查连接', self.check_connection, height=32, font_size=10)
        self.install = FlatButton(actions, '安装执行组件', self.install_component, height=32, font_size=10)
        self.docs = FlatButton(actions, '官方说明', self.open_guide, variant='ghost', height=32, font_size=10)
        for i, button in enumerate((self.check, self.install, self.docs)):
            actions.columnconfigure(i, weight=1, uniform='harness')
            button.grid(row=0, column=i, sticky='ew', padx=(0, 4))

        self.save = FlatButton(self, '保存执行方式', self.save_preferences, variant='primary', width=126, height=34)
        self.save.pack(anchor='w', pady=(8, 2))
        description(self, textvariable=self.notice).pack(fill='x')
        self.selector.bind('<<ComboboxSelected>>', self.changed)
        self.entry.bind('<KeyRelease>', self.changed)
        self.sync_busy()

    def sync_busy(self):
        state = 'disabled' if self.app.busy else 'normal'
        self.selector.configure(state='disabled' if self.app.busy else 'readonly')
        for widget in (self.entry, self.browse, self.check, self.install, self.save):
            widget.configure(state=state)
        if ENGINES.get(self.engine.get()) == 'harness':
            self.options.pack(fill='x', before=self.save)
        else:
            self.options.pack_forget()

    def changed(self, event=None):
        self.notice.set('修改尚未保存。')
        self.sync_busy()

    def choose_executable(self):
        if self.app.busy:
            return
        path = filedialog.askopenfilename(parent=self, title='选择 Harness 执行组件', filetypes=[('执行组件', '*.exe')])
        if path:
            self.executable.set(path)
            self.changed()

    def save_preferences(self):
        if self.app.busy:
            return
        try:
            harness_settings.save_preferences(self.app.sessions, {
                'engine': ENGINES[self.engine.get()], 'executable': self.executable.get().strip()})
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.notice.set(f'保存失败：{exc}')
        else:
            self.notice.set('执行方式已保存。')

    def open_guide(self):
        try:
            opened = webbrowser.open(OFFICIAL_GUIDE)
        except OSError:
            opened = False
        if not opened:
            self.notice.set('无法打开官方说明，请检查默认浏览器。')

    def check_connection(self):
        path = self.executable.get().strip()
        self.run_component_action(False, path)

    def install_component(self):
        self.run_component_action(True)

    def run_component_action(self, install, path=''):
        if self.app.busy:
            return
        title = '安装执行组件' if install else '检查组件连接'
        cancel = self.app.generation_cancel
        self.notice.set(title + '…')

        def work():
            try:
                if install:
                    return harness_settings.install_component(cancel_event=cancel), ''
                return harness_settings.probe(path, cancel_event=cancel), ''
            except Exception as exc:
                return None, str(exc)

        def done(result):
            value, error = result
            if cancel.is_set():
                self.notice.set(title + '已停止。')
            elif value is None:
                self.notice.set(title + '失败。' + error)
            elif install:
                self.executable.set(str(value))
                self.notice.set('执行组件已安装。请保存执行方式以使用此路径。')
            else:
                resume = '支持任务续跑' if value['resume'] else '此版本无法续跑'
                self.notice.set(f'连接已通过 · 服务报告版本 {value["version"]} · {resume}。' + value.get('message', ''))

        self.app.submit(title, work, done, cancellable=True)
