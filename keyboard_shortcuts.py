"""Keyboard shortcuts scoped to the main Free AI window."""
from collections.abc import Callable
from tkinter import ttk
from typing import NamedTuple


def focus_input(app):
    if app.current_page != 0:
        app.select_page(0)
    app.input.focus_set()


def stop_task(app):
    if app.current_page != 0 or not app.busy or not app.cancellable:
        return False
    app.stop_generation()


def show_shortcuts(app):
    app.select_page(2)
    app.settings_panel.select_category('shortcuts')


class Shortcut(NamedTuple):
    sequence: str
    keys: str
    description: str
    action: Callable


SHORTCUTS = (
    Shortcut('<Control-n>', 'Ctrl+N', '开始新对话', lambda app: app.new_conversation()),
    Shortcut('<Control-l>', 'Ctrl+L', '把光标移到聊天输入框', focus_input),
    Shortcut('<Control-comma>', 'Ctrl+,', '打开设置', lambda app: app.select_page(2)),
    Shortcut('<Control-b>', 'Ctrl+B', '展开或收起左侧栏', lambda app: app.toggle_sidebar()),
    Shortcut('<Control-Shift-M>', 'Ctrl+Shift+M', '打开或收起长期记忆', lambda app: app.open_memory()),
    Shortcut('<Control-Shift-O>', 'Ctrl+Shift+O', '选择这段对话要处理的项目文件夹',
             lambda app: app.project.choose_workspace()),
    Shortcut('<Control-Shift-C>', 'Ctrl+Shift+C', '复制当前对话里最后一条回答', lambda app: app.copy_answer()),
    Shortcut('<Escape>', 'Esc', '在聊天页停止正在运行且可取消的任务', stop_task),
    Shortcut('<F1>', 'F1', '查看快捷键', show_shortcuts),
)

COMPOSER_SHORTCUTS = (
    ('Ctrl+Enter', '在聊天输入框发送消息'),
    ('Enter', '在聊天输入框换行'),
)


def install_shortcuts(app):
    root = app.root

    def handle(event, shortcut):
        if event.widget.winfo_toplevel() is not root or root.grab_current() is not None:
            return None
        if shortcut.sequence == '<Escape>' and isinstance(event.widget, ttk.Combobox):
            return None
        if shortcut.action(app) is not False:
            return 'break'

    for shortcut in SHORTCUTS:
        root.bind(shortcut.sequence, lambda event, shortcut=shortcut: handle(event, shortcut), add='+')
