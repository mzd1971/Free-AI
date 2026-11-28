"""Keyboard shortcuts scoped to the main Free AI window."""
from collections.abc import Callable
from functools import lru_cache
import sys
from tkinter import ttk
from typing import NamedTuple


SETTINGS_KEY = 'client_shortcuts'
DEFAULT_SEND_KEY = 'Enter'
SEND_KEYS = (DEFAULT_SEND_KEY, 'Ctrl+Enter')
SHIFT, CONTROL = 0x1, 0x4
# Leave Caps Lock and Num Lock out of the modifier check.
KEY_MODIFIER_MASK = 0x200ED


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
    name: str
    keys: str
    description: str
    action: Callable


SHORTCUTS = (
    Shortcut('new_chat', 'Ctrl+N', '开始新对话', lambda app: app.new_conversation()),
    Shortcut('focus_chat', 'Ctrl+L', '把光标移到聊天输入框', focus_input),
    Shortcut('settings', 'Ctrl+,', '打开设置', lambda app: app.select_page(2)),
    Shortcut('sidebar', 'Ctrl+B', '展开或收起侧栏', lambda app: app.toggle_sidebar()),
    Shortcut('memory', 'Ctrl+Shift+M', '打开或收起长期记忆', lambda app: app.open_memory()),
    Shortcut('workspace', 'Ctrl+Shift+O', '选择这段对话要处理的项目文件夹',
             lambda app: app.project.choose_workspace()),
    Shortcut('copy_answer', 'Ctrl+Shift+C', '复制当前对话里最后一条回答', lambda app: app.copy_answer()),
    Shortcut('stop', 'Esc', '在聊天页停止正在运行且可取消的任务', stop_task),
    Shortcut('shortcuts', 'F1', '查看快捷键', show_shortcuts),
)

DEFAULT_SHORTCUTS = {shortcut.name: shortcut.keys for shortcut in SHORTCUTS}
SHORTCUT_FORMAT = '可用 Esc、F1 至 F9、Ctrl+B/L/N/逗号/数字、Ctrl+Shift+字母，例如 Ctrl+Shift+J。'
RESERVED_KEYS = {
    'Ctrl+A', 'Ctrl+C', 'Ctrl+D', 'Ctrl+E', 'Ctrl+F', 'Ctrl+H', 'Ctrl+I', 'Ctrl+K',
    'Ctrl+O', 'Ctrl+P', 'Ctrl+T', 'Ctrl+V', 'Ctrl+W', 'Ctrl+X', 'Ctrl+Y', 'Ctrl+Z',
    'Ctrl+Shift+A', 'Ctrl+Shift+V', 'Ctrl+Shift+X', 'Ctrl+Shift+Y', 'Ctrl+Shift+Z',
}
SUPPORTED_KEYS = {
    'Esc', 'Ctrl+B', 'Ctrl+L', 'Ctrl+N', 'Ctrl+,',
    *(f'F{number}' for number in range(1, 10)),
    *(f'Ctrl+{number}' for number in range(10)),
    *(f'Ctrl+Shift+{key}' for key in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),
} - RESERVED_KEYS
CANONICAL_KEYS = {key.casefold(): key for key in SUPPORTED_KEYS | RESERVED_KEYS}


def validate_shortcut_keys(keys):
    normalized = {}
    used = {}
    for shortcut in SHORTCUTS:
        value = keys.get(shortcut.name, shortcut.keys)
        key = CANONICAL_KEYS.get(''.join(value.split()).casefold()) if isinstance(value, str) else None
        if key in RESERVED_KEYS:
            raise ValueError(f'{key} 已保留给文本编辑。')
        if key is None:
            raise ValueError(SHORTCUT_FORMAT)
        if key in used:
            raise ValueError(f'{key} 已用于“{used[key]}”。')
        normalized[shortcut.name] = key
        used[key] = shortcut.description
    return normalized


def load_shortcut_preferences(sessions):
    saved = sessions.get(SETTINGS_KEY)
    if not isinstance(saved, dict):
        return DEFAULT_SEND_KEY, DEFAULT_SHORTCUTS.copy()
    send_key = saved.get('send_key')
    if send_key not in SEND_KEYS:
        send_key = DEFAULT_SEND_KEY
    keys = saved.get('keys')
    try:
        return send_key, validate_shortcut_keys(keys if isinstance(keys, dict) else {})
    except ValueError:
        return send_key, DEFAULT_SHORTCUTS.copy()


def save_shortcut_preferences(sessions, send_key, keys):
    if send_key not in SEND_KEYS:
        raise ValueError('请选择 Enter 或 Ctrl+Enter 发送。')
    normalized = validate_shortcut_keys(keys)
    sessions.set(SETTINGS_KEY, {'send_key': send_key, 'keys': normalized})
    return normalized


def shortcut_hint(send_key):
    if send_key == 'Ctrl+Enter':
        return 'Ctrl + Enter 发送 · Enter 或 Shift + Enter 换行'
    return 'Enter 发送 · Shift + Enter 换行'


def key_modifiers(event):
    return event.state & KEY_MODIFIER_MASK


@lru_cache(maxsize=1)
def _ime_api():
    from ctypes import WinDLL, c_void_p
    from ctypes.wintypes import BOOL, DWORD, HWND, LONG

    imm = WinDLL('imm32')
    imm.ImmGetContext.argtypes = (HWND,)
    imm.ImmGetContext.restype = c_void_p
    imm.ImmGetCompositionStringW.argtypes = (c_void_p, DWORD, c_void_p, DWORD)
    imm.ImmGetCompositionStringW.restype = LONG
    imm.ImmReleaseContext.argtypes = (HWND, c_void_p)
    imm.ImmReleaseContext.restype = BOOL
    return imm


def is_ime_event(event):
    # VK_PROCESSKEY marks keyboard input handled by the Windows input method.
    if getattr(event, 'keycode', None) == 229:
        return True
    if sys.platform != 'win32':
        return False
    imm = _ime_api()
    window = event.widget.winfo_id()
    context = imm.ImmGetContext(window)
    if context:
        try:
            return imm.ImmGetCompositionStringW(context, 0x0008, None, 0) > 0  # GCS_COMPSTR
        finally:
            imm.ImmReleaseContext(window, context)
    return False


def shortcut_available(event, root):
    # Tk's combobox popup can grab input using a window with no Python widget.
    return (event.widget.winfo_toplevel() is root and not root.tk.call('grab', 'current', root._w)
            and not is_ime_event(event))


def _event_keys(event):
    modifiers = key_modifiers(event)
    key = event.keysym
    if modifiers == 0:
        return 'Esc' if key == 'Escape' else key
    if modifiers == CONTROL:
        return 'Ctrl+' + (',' if key == 'comma' else key.upper())
    if modifiers == CONTROL | SHIFT:
        return 'Ctrl+Shift+' + key.upper()
    return None


def install_shortcuts(app):
    if getattr(app, '_shortcut_binding', None) is not None:
        return
    root = app.root

    def handle(event):
        key = _event_keys(event)
        for shortcut in SHORTCUTS:
            if key != app.shortcut_keys[shortcut.name]:
                continue
            if key == 'Esc' and isinstance(event.widget, ttk.Combobox):
                return None
            if shortcut_available(event, root) and shortcut.action(app) is not False:
                return 'break'
            return None

    app._shortcut_binding = root.bind('<KeyPress>', handle, add='+')
