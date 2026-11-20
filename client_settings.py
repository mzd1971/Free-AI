"""Native, searchable settings for the Free AI client."""
import os
import sqlite3
import tkinter as tk
from tkinter import filedialog, ttk

from api_panel import APISettingsPanel
from coding_tools import load_preferences, save_preferences
from paths import ARTIFACTS, ROOT
from project_chat import PERMISSION_LABELS
from ui_kit import CLIENT_SKINS, COLORS as C, FONT, FlatButton, RoundedFrame, ScrollArea, label, ui_font
from ui_preferences import TEXT_SIZES


CATEGORIES = {
    'general': '常规与权限',
    'coding': '编程工具',
    'models': '模型与 API',
    'appearance': '外观',
    'storage': '文件位置',
    'about': '关于',
}

PERMISSION_DESCRIPTIONS = {
    'standard': '助手可以查看项目，按文件工具设置修改文件；运行每条命令前会征求你的确认。',
    'readonly': '助手可以查看项目文件，修改文件和运行命令均不可用。',
    'automatic': '助手可直接运行命令。命令以当前 Windows 用户身份运行，可能访问项目外的文件和网络。',
}

EDITORS = {'系统默认（打开文件夹）': 'default', 'Visual Studio Code': 'vscode', '自定义程序': 'custom'}
SHELLS = {'PowerShell': 'powershell', '命令提示符（cmd）': 'cmd'}


class SettingsPanel(tk.Frame):
    def __init__(self, app, page):
        super().__init__(page, bg=C['bg'])
        self.app = app
        self.category = 'general'
        self.sections = {category: [] for category in CATEGORIES}
        self.pack(fill='both', expand=True, padx=24, pady=(12, 4))

        navigation = self.navigation = tk.Frame(self, bg=C['side'], width=156)
        navigation.pack(side='left', fill='y', padx=(0, 18))
        navigation.pack_propagate(False)
        label(navigation, '搜索设置', size=10, color=C['muted']).pack(anchor='w', padx=12, pady=(12, 4))
        self.query = tk.StringVar(self)
        search = ttk.Entry(navigation, textvariable=self.query, width=13, font=ui_font(self.app.root, 10))
        search.pack(fill='x', padx=10, pady=(0, 12))
        search.bind('<Escape>', lambda event: self.query.set(''))
        self.category_buttons = {}
        for key, title in CATEGORIES.items():
            button = FlatButton(navigation, title, lambda key=key: self.select_category(key),
                                variant='ghost', anchor='w', height=34, font_size=11)
            button.pack(fill='x', padx=8, pady=2)
            self.category_buttons[key] = button

        area = app.settings_area = ScrollArea(self, bg=C['bg'], max_width=1100)
        area.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(area, command=area.canvas.yview, orient='vertical')
        area.canvas.pack_forget()
        scrollbar.pack(side='right', fill='y')
        area.canvas.pack(side='left', fill='both', expand=True)
        area.canvas.configure(yscrollcommand=scrollbar.set)
        self.body = area.body
        self.heading = label(self.body, size=21, bold=True)
        self.heading.pack(anchor='w', pady=(0, 14))
        self.empty_message = label(self.body, '没有匹配的设置。', size=11, color=C['muted'])

        self.build_permissions()
        self.build_memory()
        self.build_coding_tools()
        self.build_models()
        self.build_appearance()
        self.build_storage()
        self.build_about()
        area.bind_wheel(self.body)

        self._query_trace = self.query.trace_add('write', self.render)
        self._permission_trace = app.project.permission_mode.trace_add('write', self.sync_busy)
        self._file_permission_trace = app.project.allow_write.trace_add('write', self.sync_busy)
        self.bind('<Destroy>', self._remove_traces)
        self.sync_busy()
        self.render()

    def _remove_traces(self, event):
        if event.widget is self:
            self.query.trace_remove('write', self._query_trace)
            self.app.project.permission_mode.trace_remove('write', self._permission_trace)
            self.app.project.allow_write.trace_remove('write', self._file_permission_trace)

    def select_category(self, category):
        self.category = category
        if self.query.get():
            self.query.set('')
        else:
            self.render()

    def toggle_navigation(self):
        if self.navigation.winfo_manager():
            self.navigation.pack_forget()
        else:
            self.navigation.pack(side='left', fill='y', padx=(0, 18), before=self.app.settings_area)

    def render(self, *_):
        terms = self.query.get().casefold().strip().split()
        self.heading.configure(text='搜索结果' if terms else CATEGORIES[self.category])
        for key, button in self.category_buttons.items():
            button.configure(variant='selected' if key == self.category and not terms else 'ghost')

        self.empty_message.pack_forget()
        for sections in self.sections.values():
            for widget, _ in sections:
                widget.pack_forget()

        visible = []
        if terms:
            visible = [widget for sections in self.sections.values() for widget, keywords in sections
                       if all(term in keywords for term in terms)]
        else:
            visible = [widget for widget, _ in self.sections[self.category]]
        for widget in visible:
            widget.pack(fill='x', padx=(0, 10), pady=(0, 12))
        if not visible:
            self.empty_message.pack(anchor='w', pady=10)
        self.app.settings_area.canvas.yview_moveto(0)

    def register_section(self, category, widget, keywords):
        self.sections[category].append((widget, f'{CATEGORIES[category]} {keywords}'.casefold()))

    def card(self, category, title, description='', keywords=''):
        card = RoundedFrame(self.body, padding=(18, 15))
        self.register_section(category, card, f'{title} {description} {keywords}')
        label(card.body, title, size=13, bold=True).pack(anchor='w')
        if description:
            self.description(card.body, description).pack(fill='x', pady=(6, 0))
        return card.body

    @staticmethod
    def description(parent, text='', **kwargs):
        widget = label(parent, text, size=10, color=C['muted'], anchor='w', justify='left', **kwargs)
        widget.bind('<Configure>', lambda event: widget.configure(wraplength=event.width))
        return widget

    def action(self, title, callback):
        def run():
            try:
                callback()
            except OSError as exc:
                self.app.status.set(f'“{title}”失败：{exc}')
        return run

    def actions(self, parent, entries):
        row = tk.Frame(parent, bg=C['surface'])
        row.pack(fill='x', pady=(12, 0))
        row.columnconfigure(0, weight=1, uniform='action')
        row.columnconfigure(1, weight=1, uniform='action')
        for index, (title, callback) in enumerate(entries):
            FlatButton(row, title, self.action(title, callback), width=154, height=34, font_size=10).grid(
                row=index // 2, column=index % 2, sticky='ew', padx=(0, 8), pady=(0, 5))

    def build_permissions(self):
        body = self.card('general', '项目助手权限', '设置应用于当前会话。新会话默认逐条确认命令。',
                         ' '.join((*PERMISSION_LABELS.values(), *PERMISSION_DESCRIPTIONS.values())))
        self.permission_buttons = {}
        for mode, title in PERMISSION_LABELS.items():
            button = FlatButton(body, title, lambda mode=mode: self.change_permission(mode),
                                variant='secondary', anchor='w', height=36, font_size=11)
            button.pack(fill='x', pady=(10, 2))
            self.permission_buttons[mode] = button
            self.description(body, PERMISSION_DESCRIPTIONS[mode]).pack(fill='x', padx=16)
        self.permission_notice = tk.StringVar(self)
        self.description(body, textvariable=self.permission_notice).pack(fill='x', pady=(8, 0))

    def change_permission(self, mode):
        if not self.app.busy:
            self.app.project.set_permission_mode(mode)
        self.sync_busy()

    def sync_busy(self, *_):
        mode = self.app.project.permission_mode.get()
        busy = self.app.busy
        self.harness.sync_busy()
        for key, button in self.permission_buttons.items():
            button.configure(variant='selected' if key == mode else 'secondary',
                             text=('✓  ' if key == mode else '') + PERMISSION_LABELS[key],
                             state='disabled' if busy else 'normal')
        access = ('当前可以修改项目文件。' if mode != 'readonly' and self.app.project.allow_write.get()
                  else '当前文件工具不能修改项目。')
        self.permission_notice.set(access + ('任务运行期间无法切换权限。' if busy else
            ' 文件工具开关可在聊天的权限菜单调整。' if mode != 'readonly' else ''))

    def build_memory(self):
        memories = self.app.memories
        memory = self.card('general', '长期记忆',
                           '把长期偏好、事实和做法保存为外置上下文。AI 可以直接保存，稍后由你核实。',
                           '记忆 自动记忆 外置上下文 联网授权 置顶 每次都参考')
        self.description(memory, textvariable=memories.mode_state).pack(fill='x', pady=(8, 2))
        self.memory_options = memories.build_options(memory)
        self.description(memory, '授权联网读写后，相关记忆可能会发给所选服务商，自动整理可能增加费用。').pack(
            fill='x', pady=(4, 0))
        FlatButton(memory, '管理长期记忆', memories.show, variant='ghost', height=32, font_size=11).pack(
            anchor='w', pady=(6, 0))

    def build_coding_tools(self):
        prefs = load_preferences(self.app.sessions)
        self.editor = tk.StringVar(self, next(title for title, value in EDITORS.items() if value == prefs['editor']))
        self.shell = tk.StringVar(self, next(title for title, value in SHELLS.items() if value == prefs['shell']))
        self.editor_path = tk.StringVar(self, prefs['editor_path'])
        self.coding_notice = tk.StringVar(self, '项目打开操作使用已保存的设置。')

        tools = self.card('coding', '编辑器与终端', keywords='系统默认 Visual Studio Code VS Code 自定义程序 可执行文件 PowerShell 命令提示符 cmd')
        fields = tk.Frame(tools, bg=C['surface'])
        fields.pack(fill='x', pady=(8, 0))
        fields.columnconfigure(1, weight=1)
        for row, name, variable, values in [(0, '编辑器', self.editor, EDITORS), (1, '终端', self.shell, SHELLS)]:
            label(fields, name, size=11).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=4)
            selection = ttk.Combobox(fields, textvariable=variable, values=list(values), state='readonly', font=ui_font(self.app.root, 11))
            selection.grid(row=row, column=1, sticky='ew', pady=4)
            selection.bind('<<ComboboxSelected>>', self.coding_changed)

        self.editor_path_row = tk.Frame(tools, bg=C['surface'])
        label(self.editor_path_row, '程序路径', size=11).pack(side='left', padx=(0, 12))
        FlatButton(self.editor_path_row, '浏览…', self.choose_editor_path, width=66, height=30, font_size=10).pack(
            side='right', padx=(6, 0))
        ttk.Entry(self.editor_path_row, textvariable=self.editor_path, state='readonly', width=12, font=ui_font(self.app.root, 10)).pack(
            side='left', fill='x', expand=True)
        self.path_anchor = tk.Frame(tools, bg=C['surface'], height=1)
        self.path_anchor.pack(fill='x')
        self.update_editor_path()

        FlatButton(tools, '保存偏好', self.save_coding_tools, variant='primary', width=102, height=34, font_size=11).pack(
            anchor='w', pady=(10, 5))
        self.description(tools, textvariable=self.coding_notice).pack(fill='x')

        from harness_panel import HarnessPanel
        project = self.card('coding', '项目执行方式', keywords='Free AI 内置 DeepSeek Harness dsh.exe '
                            '安装执行组件 检查连接 官方说明 打开当前项目 用编辑器打开项目 在项目中打开终端 工作区')
        self.harness = HarnessPanel(self.app, project, self.description)
        self.harness.pack(fill='x')
        ttk.Separator(project).pack(fill='x', pady=12)
        label(project, '打开当前项目', size=13, bold=True).pack(anchor='w')
        self.description(project, '请先在项目对话中选择文件夹。').pack(fill='x')
        self.description(project, textvariable=self.app.project.workspace).pack(fill='x')
        self.actions(project, [('用编辑器打开项目', self.app.open_project_in_editor),
                               ('在项目中打开终端', self.app.open_project_terminal)])

    def update_editor_path(self):
        if EDITORS[self.editor.get()] == 'custom':
            self.editor_path_row.pack(fill='x', pady=(4, 0), before=self.path_anchor)
        else:
            self.editor_path_row.pack_forget()

    def coding_changed(self, event=None):
        self.update_editor_path()
        self.coding_notice.set('修改尚未保存。')

    def choose_editor_path(self):
        chosen = filedialog.askopenfilename(parent=self, title='选择编辑器程序', filetypes=[('可执行文件', '*.exe')])
        if chosen:
            self.editor_path.set(chosen)
            self.coding_changed()

    def save_coding_tools(self):
        try:
            save_preferences(self.app.sessions, EDITORS[self.editor.get()], SHELLS[self.shell.get()], self.editor_path.get())
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.coding_notice.set(f'无法保存编程工具设置：{exc}')
        else:
            self.coding_notice.set('编程工具设置已保存。')

    def build_models(self):
        app = self.app
        api_host = tk.Frame(self.body, bg=C['bg'])
        self.register_section('models', api_host,
                              '联网 API 连接 DeepSeek 自定义兼容接口 服务商 服务地址 模型名 API 密钥 '
                              '记住密钥 回答上限 思考 本地经验 保存设置 测试连接 使用 API 切回本机 清除已存密钥')
        app.api_panel = APISettingsPanel(app, api_host)
        self.description(api_host, '这里的“本地经验”是任务经验库。联网读写长期记忆的授权在「常规与权限 → 长期记忆」。').pack(
            fill='x', pady=(0, 8))
        models = self.card('models', '模型占用', '需要推理时才加载模型。释放内存会保留模型选择，停用模型会清除选择。',
                           '本地模型 释放模型 停用模型 下载 能力中心')
        self.actions(models, [('释放模型', app.release_model), ('停用模型', app.disable_model),
                              ('查看可下载模型', lambda: app.show_capabilities('models'))])

    def build_appearance(self):
        app = self.app
        text_body = self.card('appearance', '文字大小', '立即调整文字和控件大小，不会清空草稿。客户端与训练端共用此偏好。',
                              '字体 缩放 标准 大 特大 全屏 阅读')
        current_size = getattr(app.root, '_free_ai_text_size', 'normal')
        app.text_size_choice = tk.StringVar(self, next((name for name, value in TEXT_SIZES.items() if value == current_size), '标准'))
        app.text_size_picker = ttk.Combobox(text_body, values=list(TEXT_SIZES), textvariable=app.text_size_choice,
                                           state='readonly', width=16)
        app.text_size_picker.pack(anchor='w', pady=(10, 6))
        app.text_size_picker.bind('<<ComboboxSelected>>', app.change_text_size)
        label(text_body, '阅读预览：把想法变成可以完成的任务。', size=14, anchor='w', justify='left').pack(fill='x', pady=(4, 4))
        body = self.card('appearance', '界面皮肤', keywords='主题 白蓝 蓝色 明亮 边缘行者 霓虹 经典深色')
        app.skin_choice = tk.StringVar(self, next(name for name, value in CLIENT_SKINS.items() if value == app.theme))
        app.skin_picker = ttk.Combobox(body, values=list(CLIENT_SKINS), textvariable=app.skin_choice,
                                      state='readonly', width=24)
        app.skin_picker.pack(anchor='w', pady=(9, 6))
        app.skin_notice = tk.StringVar(self, '选择皮肤后，重新打开 Free AI 生效。')
        self.description(body, textvariable=app.skin_notice).pack(fill='x')
        app.skin_picker.bind('<<ComboboxSelected>>', app.save_skin)

    def build_storage(self):
        app = self.app
        records = self.card('storage', '聊天和训练记录', str(ARTIFACTS), '数据 打开记录文件夹 更改记录位置')
        self.actions(records, [('打开记录文件夹', lambda: os.startfile(ARTIFACTS)), ('更改记录位置', app.choose_storage)])
        models = self.card('storage', '模型和扩展', str(app.manager.root), '模型文件夹 模型位置 安装')
        self.description(models, '模型位置可在安装时选择，默认为 FreeAI 文件夹内的 models 目录。').pack(
            fill='x', pady=(6, 0))
        self.actions(models, [('打开模型文件夹', app.open_model_folder)])

    def build_about(self):
        about = self.card('about', 'Free AI', '版本 2.8.0', '使用说明 帮助')
        self.actions(about, [('使用说明', lambda: os.startfile(ROOT / '使用说明.md'))])
        trainer = self.card('about', '独立训练端', '在 Free AI Trainer 中准备资料、训练、评估与改进经验。', '打开训练端')
        self.actions(trainer, [('打开训练端', self.app.open_training)])
