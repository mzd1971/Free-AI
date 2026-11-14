"""Small, dependency-free desktop components shared by the two Free AI apps."""
import tkinter as tk
from tkinter import ttk

COLORS = {'bg': '#faf9f6', 'side': '#f0efeb', 'surface': '#ffffff',
          'text': '#262724', 'muted': '#85877e', 'line': '#e5e5df',
          'accent': '#7462c8', 'tint': '#efecf9', 'success': '#568064'}
FONT = 'Microsoft YaHei UI'


def background(parent):
    try:
        return parent.cget('bg')
    except tk.TclError:
        return COLORS['bg']


def setup_theme(root, title, size=(1320, 880)):
    root.title(title)
    root.tk.call('tk', 'scaling', 1.4)
    width = min(size[0], root.winfo_screenwidth() - 70)
    height = min(size[1], root.winfo_screenheight() - 95)
    root.geometry(f'{width}x{height}+32+28')
    root.minsize(min(940, width), min(640, height))
    root.configure(bg=COLORS['bg'])
    root.option_add('*Font', (FONT, 12))
    root.option_add('*Menu.background', COLORS['surface'])
    root.option_add('*Menu.foreground', COLORS['text'])
    root.option_add('*Menu.activeBackground', COLORS['tint'])
    root.option_add('*Menu.activeForeground', COLORS['text'])
    root.option_add('*Menu.relief', 'flat')
    root.option_add('*Menu.borderWidth', 0)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=(FONT, 12), background=COLORS['bg'], foreground=COLORS['text'])
    style.configure('TFrame', background=COLORS['bg'])
    style.configure('TLabel', background=COLORS['bg'], foreground=COLORS['text'])
    style.configure('TButton', background=COLORS['side'], borderwidth=0, padding=(12, 8))
    style.map('TButton', background=[('active', COLORS['line'])])
    style.configure('Primary.TButton', foreground='white', background=COLORS['text'])
    style.map('Primary.TButton', background=[('active', '#44463f'), ('disabled', '#bbbdb4')])
    style.configure('TNotebook', borderwidth=0, background=COLORS['bg'])
    style.configure('TNotebook.Tab', background=COLORS['side'], padding=(14, 8), borderwidth=0)
    style.map('TNotebook.Tab', background=[('selected', COLORS['surface'])])
    style.configure('Treeview', background=COLORS['surface'], fieldbackground=COLORS['surface'],
                    borderwidth=0, rowheight=38, font=(FONT, 11))
    style.configure('Treeview.Heading', background=COLORS['side'], borderwidth=0, font=(FONT, 11))
    style.map('Treeview', background=[('selected', COLORS['tint'])], foreground=[('selected', COLORS['text'])])
    style.configure('TEntry', fieldbackground=COLORS['surface'], borderwidth=0, padding=8)
    style.configure('TCombobox', fieldbackground=COLORS['surface'], borderwidth=0, padding=6)
    style.configure('Horizontal.TProgressbar', background=COLORS['accent'], troughcolor=COLORS['side'],
                    borderwidth=0, lightcolor=COLORS['accent'], darkcolor=COLORS['accent'])
    try:
        from paths import ROOT
        root.iconbitmap(str(ROOT / 'assets/free-ai.ico'))
    except (OSError, tk.TclError):
        pass


def rounded(canvas, x1, y1, x2, y2, radius, **kwargs):
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [x1+radius,y1,x2-radius,y1,x2,y1,x2,y1+radius,
              x2,y2-radius,x2,y2,x2-radius,y2,x1+radius,y2,
              x1,y2,x1,y2-radius,x1,y1+radius,x1,y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class RoundedFrame(tk.Frame):
    def __init__(self, parent, bg=None, radius=18, padding=18, border=None, **kwargs):
        super().__init__(parent, bg=background(parent), bd=0, highlightthickness=0, **kwargs)
        self.color = bg or COLORS['surface']
        self.radius, self.border = radius, border
        self.canvas = tk.Canvas(self, bg=background(parent), highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=self.color, bd=0)
        pad = padding if isinstance(padding, tuple) else (padding, padding)
        self.body.pack(fill='both', expand=True, padx=pad[0], pady=pad[1])
        self.canvas.bind('<Configure>', self.draw)

    def draw(self, event=None):
        self.canvas.delete('all')
        rounded(self.canvas, 1, 1, max(2, self.winfo_width()-1), max(2, self.winfo_height()-1), self.radius,
                fill=self.color, outline=self.border or self.color, width=1)


class FlatButton(tk.Canvas):
    def __init__(self, parent, text='', command=None, variant='secondary', width=None, height=40,
                 radius=12, font_size=12, anchor='center', state='normal', **kwargs):
        self.value, self.command, self.variant = text, command, variant
        self.button_state, self.radius, self.font_size, self.align = state, radius, font_size, anchor
        self.hovered = False
        if width is None:
            width = max(66, int(sum(1.8 if ord(c) > 255 else 1 for c in text) * font_size * .7 + 32))
        super().__init__(parent, width=width, height=height, bg=background(parent), bd=0,
                         highlightthickness=0, takefocus=True, cursor='hand2', **kwargs)
        self.bind('<Configure>', self.draw)
        self.bind('<Enter>', lambda e: self._hover(True))
        self.bind('<Leave>', lambda e: self._hover(False))
        self.bind('<ButtonRelease-1>', lambda e: self.invoke())
        self.bind('<Return>', lambda e: self.invoke())
        self.bind('<space>', lambda e: self.invoke())
        self.bind('<FocusIn>', self.draw)
        self.bind('<FocusOut>', self.draw)

    def _hover(self, value):
        self.hovered = value
        self.draw()

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        for source, target in [('text','value'), ('command','command'), ('variant','variant'), ('state','button_state')]:
            if source in kwargs:
                setattr(self, target, kwargs.pop(source))
        result = super().configure(**kwargs) if kwargs else None
        self.draw()
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
        fill = {'primary': COLORS['text'], 'secondary': COLORS['side'], 'ghost': background(self.master),
                'selected': COLORS['line'], 'accent': COLORS['tint']}.get(self.variant, COLORS['side'])
        if self.hovered and not disabled:
            fill = '#42443e' if self.variant == 'primary' else '#e7e6df'
        foreground = 'white' if self.variant == 'primary' else COLORS['text']
        if disabled:
            foreground = '#a2a49a'
            if self.variant == 'primary':
                fill = '#e3e4de'
        w, h = max(self.winfo_width(), int(float(super().cget('width')))), max(self.winfo_height(), 2)
        rounded(self, 1, 1, w-1, h-1, self.radius, fill=fill, outline=fill)
        self.create_text(16 if self.align == 'w' else w/2, h/2, text=self.value,
                         anchor=self.align, fill=foreground, font=(FONT, self.font_size))
        if self.focus_get() == self and not disabled:
            rounded(self, 2, 2, w-2, h-2, self.radius, fill='', outline=COLORS['accent'], width=1)


def label(parent, text='', size=12, bold=False, color=None, **kwargs):
    return tk.Label(parent, text=text, bg=background(parent), fg=color or COLORS['text'],
                    font=(FONT, size, 'bold' if bold else 'normal'), bd=0, **kwargs)


class ScrollArea(tk.Frame):
    def __init__(self, parent, bg=None, **kwargs):
        color = bg or background(parent)
        super().__init__(parent, bg=color, **kwargs)
        self.canvas = tk.Canvas(self, bg=color, bd=0, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.body = tk.Frame(self.canvas, bg=color)
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor='nw')
        self.body.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.body.bind('<MouseWheel>', self.wheel)

    def wheel(self, event):
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta/120), 'units')
        return 'break'

    def bind_wheel(self, widget):
        widget.bind('<MouseWheel>', self.wheel, add='+')
        for child in widget.winfo_children():
            self.bind_wheel(child)
