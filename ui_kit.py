"""Small, dependency-free desktop components shared by the two Free AI apps."""
import sys
import tkinter as tk
from tkinter import font as tkfont, ttk
from weakref import WeakSet

from ui_preferences import DEFAULT_TEXT_SIZE, TEXT_SCALE

LIGHT_COLORS = {
    'bg': '#faf9f6', 'side': '#f0efeb', 'surface': '#ffffff', 'input': '#ffffff',
    'text': '#262724', 'muted': '#61645a', 'line': '#e5e5df',
    'accent': '#7462c8', 'tint': '#efecf9', 'success': '#568064',
    'hover': '#e7e6df', 'primary_bg': '#262724', 'primary_fg': '#ffffff',
    'primary_hover': '#42443e', 'disabled_bg': '#e3e4de', 'disabled_fg': '#a2a49a',
    'focus': '#7462c8', 'selection_bg': '#efecf9', 'selection_fg': '#262724',
    'card_border': '#ffffff',
}
DARK_COLORS = {
    'bg': '#161719', 'side': '#202226', 'surface': '#282b30', 'input': '#353940',
    'text': '#f5f6f8', 'muted': '#c2c8d1', 'line': '#686f7b',
    'accent': '#b5d1f8', 'tint': '#4b617e', 'success': '#a5dbb0',
    'hover': '#515967', 'primary_bg': '#f5f6f8', 'primary_fg': '#17191d',
    'primary_hover': '#cddcf0', 'disabled_bg': '#343841', 'disabled_fg': '#949ba7',
    'focus': '#a9cbff', 'selection_bg': '#506989', 'selection_fg': '#ffffff',
    'card_border': '#616873',
}
CYBER_COLORS = {
    'bg': '#050909', 'side': '#091315', 'surface': '#0b191c', 'input': '#10282d',
    'text': '#e8f6ed', 'muted': '#b4d1cf', 'line': '#40949a',
    'accent': '#00eeee', 'tint': '#14494c', 'success': '#92ff59',
    'hover': '#20565a', 'primary_bg': '#a5ff24', 'primary_fg': '#061011',
    'primary_hover': '#ceff77', 'disabled_bg': '#203536', 'disabled_fg': '#98aeac',
    'focus': '#00eeee', 'selection_bg': '#a5ff24', 'selection_fg': '#061011',
    'card_border': '#32747a', 'selected_bg': '#14494c', 'selected_fg': '#b3ff3b',
    'brand': '#a5ff24', 'secondary': '#fff12a',
}
CLIENT_SKINS = {'边缘行者 · 霓虹': 'cyberpunk', '经典深色': 'dark'}
COLORS = LIGHT_COLORS.copy()
FONT = 'Microsoft YaHei UI'


def _font_registry(root):
    if not hasattr(root, '_free_ai_fonts'):
        root._free_ai_text_size = DEFAULT_TEXT_SIZE
        root._free_ai_fonts = {}
        root._free_ai_buttons = WeakSet()
    return root._free_ai_fonts


def ui_font(widget, size=12, bold=False, family=FONT):
    """Return an app-wide named font so existing widgets and tags can resize in place."""
    root = widget._root()
    fonts = _font_registry(root)
    key = (family, size, bold)
    if key not in fonts:
        fonts[key] = tkfont.Font(root=root, family=family, weight='bold' if bold else 'normal',
                                 size=round(size * TEXT_SCALE[root._free_ai_text_size]))
    return fonts[key]


def text_scale(widget):
    return TEXT_SCALE[getattr(widget._root(), '_free_ai_text_size', DEFAULT_TEXT_SIZE)]


def set_text_size(root, choice):
    if choice not in TEXT_SCALE:
        raise ValueError('未知字号')
    root = root._root()
    fonts = _font_registry(root)
    root._free_ai_text_size = choice
    for (_, size, _), font in fonts.items():
        font.configure(size=round(size * TEXT_SCALE[choice]))
    if getattr(root, '_free_ai_theme', None):
        style = ttk.Style(root)
        style.configure('Treeview', rowheight=max(38, ui_font(root, 11).metrics('linespace') + 14))
        style.configure('Knowledge.Treeview', rowheight=max(34, ui_font(root, 12).metrics('linespace') + 12))
    for button in root._free_ai_buttons:
        button._resize_text()
    root.event_generate('<<FreeAITextSizeChanged>>')


def background(parent):
    try:
        return parent.cget('bg')
    except tk.TclError:
        return COLORS['bg']


def setup_theme(root, title, size=(1320, 880), theme='light', *,
                text_size=DEFAULT_TEXT_SIZE, min_size=(940, 640)):
    palettes = {'light': LIGHT_COLORS, 'dark': DARK_COLORS, 'cyberpunk': CYBER_COLORS}
    if theme not in palettes:
        raise ValueError('Unknown application theme')
    COLORS.clear()
    COLORS.update(palettes[theme])
    root._free_ai_theme = theme
    root.title(title)
    width = min(size[0], root.winfo_screenwidth() - 70)
    height = min(size[1], root.winfo_screenheight() - 95)
    root.geometry(f'{width}x{height}+32+28')
    root.minsize(min(min_size[0], width), min(min_size[1], height))
    root.configure(bg=COLORS['bg'])
    root.option_add('*Font', ui_font(root))
    root.option_add('*Menu.background', COLORS['surface'])
    root.option_add('*Menu.foreground', COLORS['text'])
    root.option_add('*Menu.activeBackground', COLORS['tint'])
    root.option_add('*Menu.activeForeground', COLORS['text'])
    root.option_add('*Menu.relief', 'flat')
    root.option_add('*Menu.borderWidth', 0)
    root.option_add('*Menu.disabledForeground', COLORS['disabled_fg'])
    for widget in ('Text', 'Entry', 'Listbox'):
        root.option_add(f'*{widget}.background', COLORS['input'])
        root.option_add(f'*{widget}.foreground', COLORS['text'])
        root.option_add(f'*{widget}.selectBackground', COLORS['selection_bg'])
        root.option_add(f'*{widget}.selectForeground', COLORS['selection_fg'])
        root.option_add(f'*{widget}.highlightBackground', COLORS['line'])
        root.option_add(f'*{widget}.highlightColor', COLORS['focus'])
    for widget in ('Text', 'Entry'):
        root.option_add(f'*{widget}.insertBackground', COLORS['text'])
    for widget in ('Checkbutton', 'Radiobutton'):
        root.option_add(f'*{widget}.background', COLORS['bg'])
        root.option_add(f'*{widget}.foreground', COLORS['text'])
        root.option_add(f'*{widget}.activeBackground', COLORS['hover'])
        root.option_add(f'*{widget}.activeForeground', COLORS['text'])
        root.option_add(f'*{widget}.selectColor', COLORS['input'])
        root.option_add(f'*{widget}.disabledForeground', COLORS['disabled_fg'])
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=ui_font(root), background=COLORS['bg'], foreground=COLORS['text'],
                    bordercolor=COLORS['line'], lightcolor=COLORS['line'], darkcolor=COLORS['line'],
                    focuscolor=COLORS['focus'], selectbackground=COLORS['selection_bg'],
                    selectforeground=COLORS['selection_fg'], insertcolor=COLORS['text'])
    style.map('.', foreground=[('disabled', COLORS['disabled_fg'])])
    style.configure('TFrame', background=COLORS['bg'])
    style.configure('TLabel', background=COLORS['bg'], foreground=COLORS['text'])
    style.configure('TButton', background=COLORS['side'], borderwidth=0, padding=(12, 8))
    style.map('TButton', background=[('disabled', COLORS['side']), ('active', COLORS['hover'])])
    style.configure('Primary.TButton', foreground=COLORS['primary_fg'], background=COLORS['primary_bg'])
    style.map('Primary.TButton',
              background=[('disabled', COLORS['disabled_bg']), ('active', COLORS['primary_hover'])],
              foreground=[('disabled', COLORS['disabled_fg'])])
    style.configure('TNotebook', borderwidth=0, background=COLORS['bg'])
    style.configure('TNotebook.Tab', background=COLORS['side'], padding=(14, 8), borderwidth=0)
    style.map('TNotebook.Tab', background=[('selected', COLORS['surface'])])
    style.configure('Treeview', background=COLORS['surface'], fieldbackground=COLORS['surface'],
                    borderwidth=0, font=ui_font(root, 11))
    style.configure('Treeview.Heading', background=COLORS['side'], borderwidth=0, font=ui_font(root, 11))
    style.configure('Knowledge.Treeview', font=ui_font(root))
    style.configure('Knowledge.TNotebook.Tab', font=ui_font(root))
    style.map('Treeview', background=[('selected', COLORS['tint'])], foreground=[('selected', COLORS['text'])])
    style.configure('TEntry', fieldbackground=COLORS['input'], borderwidth=0, padding=8)
    style.configure('TCombobox', fieldbackground=COLORS['input'], background=COLORS['side'],
                    foreground=COLORS['text'], arrowcolor=COLORS['text'], borderwidth=0, padding=6,
                    selectforeground=COLORS['selection_fg'], selectbackground=COLORS['selection_bg'])
    style.map('TCombobox',
              fieldbackground=[('disabled', COLORS['disabled_bg']), ('readonly', COLORS['input'])],
              foreground=[('disabled', COLORS['disabled_fg']), ('readonly', COLORS['text'])],
              selectforeground=[('readonly', COLORS['selection_fg'])],
              selectbackground=[('readonly', COLORS['selection_bg'])],
              background=[('active', COLORS['hover']), ('readonly', COLORS['side'])])
    style.configure('TCheckbutton', indicatorbackground=COLORS['input'],
                    indicatorforeground=COLORS['text'])
    style.map('TCheckbutton', background=[('active', COLORS['side'])],
              indicatorbackground=[('selected', COLORS['tint'])])
    style.configure('Vertical.TScrollbar', background=COLORS['side'],
                    troughcolor=COLORS['bg'], arrowcolor=COLORS['muted'])
    style.map('Vertical.TScrollbar', background=[('active', COLORS['hover'])])
    style.configure('Horizontal.TProgressbar', background=COLORS['accent'], troughcolor=COLORS['side'],
                    borderwidth=0, lightcolor=COLORS['accent'], darkcolor=COLORS['accent'])
    set_text_size(root, text_size)
    try:
        from paths import ROOT
        root.iconbitmap(str(ROOT / 'assets/free-ai.ico'))
    except (OSError, tk.TclError):
        pass
    if sys.platform == 'win32':
        root.after_idle(lambda: _set_windows_titlebar_theme(root))


def _set_windows_titlebar_theme(root):
    import ctypes
    from ctypes import wintypes

    try:
        get_parent = ctypes.windll.user32.GetParent
        get_parent.argtypes = (wintypes.HWND,)
        get_parent.restype = wintypes.HWND
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        set_attribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
        set_attribute.restype = ctypes.c_long
        window = root.winfo_id()
        frame = get_parent(window) or window
        if frame:
            dark = wintypes.BOOL(root._free_ai_theme != 'light')
            set_attribute(frame, 20, ctypes.byref(dark), ctypes.sizeof(dark))
    except (AttributeError, OSError, tk.TclError):
        pass


def rounded(canvas, x1, y1, x2, y2, radius, **kwargs):
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [x1+radius,y1,x2-radius,y1,x2,y1,x2,y1+radius,
              x2,y2-radius,x2,y2,x2-radius,y2,x1+radius,y2,
              x1,y2,x1,y2-radius,x1,y1+radius,x1,y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


def _is_cyberpunk(widget):
    return getattr(widget.winfo_toplevel(), '_free_ai_theme', 'light') == 'cyberpunk'


def _chamfered(canvas, x1, y1, x2, y2, size, **kwargs):
    cut = max(0, min(size, (x2 - x1) / 2, (y2 - y1) / 2))
    return canvas.create_polygon(
        x1 + cut, y1, x2, y1, x2, y2 - cut, x2 - cut, y2, x1, y2, x1, y1 + cut,
        **kwargs,
    )


def paint_circuit(canvas):
    """Draw a static circuit trace along the bottom of the cyberpunk header."""
    tag = 'header-circuit'
    canvas.delete(tag)
    width, height = canvas.winfo_width(), canvas.winfo_height()
    if not _is_cyberpunk(canvas) or width < 56 or height < 10:
        return
    start, end = 8, width - 8
    left, right = start + (end - start) * .22, start + (end - start) * .81
    top, bottom = 3, height - 4
    step = min(9, (right - left) / 4)
    canvas.create_line(
        start, bottom, left, bottom, left + step, top, right - step, top,
        right, bottom, end, bottom, fill=COLORS['line'], tags=tag,
    )
    canvas.create_line(left + step, top, left + 2 * step, top,
                       fill=COLORS['accent'], width=2, tags=tag)
    for x, y in ((start, bottom), ((left + right) / 2, top), (end, bottom)):
        canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=COLORS['bg'],
                           outline=COLORS['accent'], tags=tag)


class RoundedFrame(tk.Frame):
    def __init__(self, parent, bg=None, radius=18, padding=18, border=None, **kwargs):
        super().__init__(parent, bg=background(parent), bd=0, highlightthickness=0, **kwargs)
        self.color = bg or COLORS['surface']
        themed_border = (COLORS['card_border']
                         if getattr(self.winfo_toplevel(), '_free_ai_theme', 'light') != 'light' else self.color)
        self.radius = radius
        self.border = border if border is not None else themed_border
        self.canvas = tk.Canvas(self, bg=background(parent), highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=self.color, bd=0)
        pad = padding if isinstance(padding, tuple) else (padding, padding)
        self.body.pack(fill='both', expand=True, padx=pad[0], pady=pad[1])
        self.canvas.bind('<Configure>', self.draw)

    def draw(self, event=None):
        self.canvas.delete('all')
        right, bottom = max(2, self.winfo_width() - 1), max(2, self.winfo_height() - 1)
        if _is_cyberpunk(self):
            _chamfered(self.canvas, 1, 1, right, bottom, 12,
                        fill=self.color, outline=self.border or COLORS['card_border'], width=1)
            if right >= 48 and bottom >= 34:
                self.canvas.create_line(1, 19, 1, 13, 13, 1, 30, 1, fill=COLORS['accent'], width=2)
                self.canvas.create_line(right, bottom - 19, right, bottom - 12,
                                        right - 12, bottom, right - 29, bottom,
                                        fill=COLORS['accent'], width=2)
        else:
            rounded(self.canvas, 1, 1, right, bottom, self.radius,
                    fill=self.color, outline=self.border or self.color, width=1)


class FlatButton(tk.Canvas):
    """Canvas button with a minimum width and an optional cap for variable captions."""

    def __init__(self, parent, text='', command=None, variant='secondary', width=None, height=40,
                 radius=12, font_size=12, anchor='center', state='normal', *, max_width=None, **kwargs):
        self.value, self.command, self.variant = text, command, variant
        self.button_state, self.radius, self.font_size, self.align = state, radius, font_size, anchor
        self.hovered = False
        self._base_width, self._base_height = width, height
        self._max_width = max_width
        self._font = ui_font(parent, font_size)
        width, height = self._requested_size()
        super().__init__(parent, width=width, height=height, bg=background(parent), bd=0,
                         highlightthickness=0, takefocus=state != 'disabled',
                         cursor='arrow' if state == 'disabled' else 'hand2', **kwargs)
        self._root()._free_ai_buttons.add(self)
        self.bind('<Configure>', self.draw)
        self.bind('<Enter>', lambda e: self._hover(True))
        self.bind('<Leave>', lambda e: self._hover(False))
        self.bind('<Button-1>', self._focus_on_click)
        self.bind('<ButtonRelease-1>', self._activate)
        self.bind('<Return>', self._activate)
        self.bind('<space>', self._activate)
        self.bind('<FocusIn>', self.draw)
        self.bind('<FocusOut>', self.draw)
        self.bind('<Destroy>', lambda e: self._root()._free_ai_buttons.discard(self))

    def _requested_size(self):
        text_width = self._font.measure(self.value)
        padding = 28 if self.align == 'w' or self._base_width is None else 20
        if self._base_width is not None and self.align == 'center' and len(self.value) == 1:
            padding = 8
        width = max(self._base_width if self._base_width is not None else 66, text_width + padding)
        if self._max_width is not None:
            width = min(width, self._max_width)
        height = max(self._base_height, self._font.metrics('linespace') + 8)
        return width, height

    def _resize_text(self):
        width, height = self._requested_size()
        super().configure(width=width, height=height)
        self.draw()

    def _focus_on_click(self, event):
        if self.button_state != 'disabled':
            self.focus_set()

    def _activate(self, event):
        self.invoke()
        return 'break'

    def _hover(self, value):
        self.hovered = value
        self.draw()

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        for source, target in [('text','value'), ('command','command'), ('variant','variant'), ('state','button_state')]:
            if source in kwargs:
                setattr(self, target, kwargs.pop(source))
        if 'width' in kwargs:
            self._base_width = kwargs.pop('width')
        if 'height' in kwargs:
            self._base_height = kwargs.pop('height')
        if self.button_state == 'disabled':
            kwargs.update(takefocus=False, cursor='arrow')
        else:
            kwargs.update(takefocus=True, cursor='hand2')
        result = super().configure(**kwargs)
        self._resize_text()
        return result
    config = configure

    def cget(self, name):
        if name == 'state':
            return self.button_state
        if name == 'text':
            return self.value
        return super().cget(name)

    def invoke(self):
        if self.button_state != 'disabled' and self.command:
            return self.command()

    def draw(self, event=None):
        self.delete('all')
        disabled = self.button_state == 'disabled'
        fill = {'primary': COLORS['primary_bg'], 'secondary': COLORS['side'], 'ghost': background(self.master),
                'selected': COLORS.get('selected_bg', COLORS['line']), 'accent': COLORS['tint']}.get(self.variant, COLORS['side'])
        if self.hovered and not disabled:
            fill = COLORS['primary_hover'] if self.variant == 'primary' else COLORS['hover']
        foreground = COLORS['primary_fg'] if self.variant == 'primary' else COLORS['text']
        if self.variant == 'selected':
            foreground = COLORS.get('selected_fg', COLORS['text'])
        if disabled:
            foreground = COLORS['disabled_fg']
            if self.variant == 'primary':
                fill = COLORS['disabled_bg']
        w, h = self.winfo_width(), self.winfo_height()
        if w < 3 or h < 3:
            return
        cyberpunk = _is_cyberpunk(self)
        if cyberpunk:
            outline = fill
            if not disabled and (self.variant in ('secondary', 'selected', 'accent') or self.hovered):
                outline = COLORS['card_border'] if self.variant == 'secondary' else COLORS['line']
                if self.variant == 'primary':
                    outline = fill
            _chamfered(self, 1, 1, w - 1, h - 1, 7, fill=fill, outline=outline)
        else:
            rounded(self, 1, 1, w-1, h-1, self.radius, fill=fill, outline=fill)
        text = self.value
        available = w - (24 if self.align == 'w' else 8)
        text_width = self._font.measure(text)
        if text_width > available and len(text) > 1:
            text = text[:max(0, int(len(text) * available / text_width))]
            while text and self._font.measure(text + '…') > available:
                text = text[:-1]
            text += '…'
        self.create_text(16 if self.align == 'w' else w/2, h/2, text=text,
                         anchor=self.align, fill=foreground, font=self._font)
        if self.focus_get() == self and not disabled:
            if cyberpunk:
                focus = COLORS['primary_fg'] if self.variant == 'primary' else COLORS['focus']
                _chamfered(self, 3, 3, w - 3, h - 3, 6, fill='', outline=focus, width=1)
            else:
                rounded(self, 2, 2, w-2, h-2, self.radius, fill='', outline=COLORS['focus'], width=1)


def label(parent, text='', size=12, bold=False, color=None, **kwargs):
    return tk.Label(parent, text=text, bg=background(parent), fg=color or COLORS['text'],
                    font=ui_font(parent, size, bold), bd=0, **kwargs)


class ScrollArea(tk.Frame):
    def __init__(self, parent, bg=None, *, max_width=None, scrollbar=False, **kwargs):
        color = bg or background(parent)
        super().__init__(parent, bg=color, **kwargs)
        self.max_width = max_width
        self.canvas = tk.Canvas(self, bg=color, bd=0, highlightthickness=0)
        if scrollbar:
            bar = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
            bar.pack(side='right', fill='y')
            self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.body = tk.Frame(self.canvas, bg=color)
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor='nw', width=1)
        self.body.bind('<Configure>', self._update_scroll_region)
        self.canvas.bind('<Configure>', self._resize_body)
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.body.bind('<MouseWheel>', self.wheel)

    def wheel(self, event):
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta/120), 'units')
        return 'break'

    def _resize_body(self, event):
        width = min(event.width, self.max_width) if self.max_width else event.width
        self.canvas.itemconfigure(self.window, width=width)
        self.canvas.coords(self.window, (event.width - width) // 2, 0)
        self._update_scroll_region()

    def _update_scroll_region(self, event=None):
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), self.body.winfo_reqheight()))

    def bind_wheel(self, widget):
        if isinstance(widget, (tk.Text, ttk.Treeview)):
            return
        if widget not in (self.body, self.canvas):
            widget.bind('<MouseWheel>', self.wheel, add='+')
        for child in widget.winfo_children():
            self.bind_wheel(child)
