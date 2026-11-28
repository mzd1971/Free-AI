"""Native interface controls shared by the client panels."""

import tkinter as tk

from ui_kit import COLORS as C, background, display_scale, label, rounded


class SwitchRow(tk.Frame):
    """A BooleanVar-backed switch with a clickable label and one tab stop."""

    def __init__(self, parent, text, variable, command=None, description='', font_size=10, bg=None):
        color = bg or background(parent)
        super().__init__(parent, bg=color, bd=0, highlightthickness=0)
        self._state, self._command = 'normal', command
        self._variable = None
        self._trace = None
        self._wraplength = 0
        self._key = None
        self._mouse_pressed = False
        scale = display_scale(self)

        self._labels = tk.Frame(self, bg=color, bd=0)
        self._labels.grid(row=0, column=0, sticky='ew', padx=(round(8 * scale), round(10 * scale)),
                          pady=round(5 * scale))
        self._title = label(self._labels, text, size=font_size, anchor='w', justify='left')
        self._title.pack(fill='x')
        self._description = label(self._labels, description, size=max(8, font_size - 1), anchor='w', justify='left')
        if description:
            self._description.pack(fill='x', pady=(round(2 * scale), 0))
        self._track = tk.Canvas(self, width=round(50 * scale), height=round(30 * scale), bg=color,
                                bd=0, highlightthickness=0, takefocus=0)
        self._track.grid(row=0, column=1, padx=(0, round(7 * scale)))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, minsize=round(44 * scale))

        self._surfaces = (self, self._labels, self._title, self._description, self._track)
        for widget in self._surfaces:
            widget.bind('<ButtonPress-1>', self._press_mouse)
            widget.bind('<ButtonRelease-1>', self._release_mouse)
        for key in ('space', 'Return', 'KP_Enter'):
            self.bind(f'<KeyPress-{key}>', self._press_key)
            self.bind(f'<KeyRelease-{key}>', self._release_key)
        self.bind('<FocusIn>', self._draw)
        self.bind('<FocusOut>', self._lose_focus)
        self._track.bind('<Configure>', self._draw)
        self._labels.bind('<Configure>', self._wrap)
        self._set_variable(variable)
        self._set_wrap(round(240 * scale))
        self._update_style()

    def _set_variable(self, variable):
        if self._trace is not None:
            self._variable.trace_remove('write', self._trace)
        self._variable = variable
        self._trace = variable.trace_add('write', self._draw)

    def _set_wrap(self, width):
        width = min(width, self._wraplength) if self._wraplength else width
        for widget in (self._title, self._description):
            widget.configure(wraplength=max(1, width))

    def _wrap(self, event):
        self._set_wrap(event.width)

    def _press_mouse(self, event):
        if self._state == 'normal':
            self._mouse_pressed = True
            self.focus_set()
        return 'break'

    def _release_mouse(self, event):
        pressed, self._mouse_pressed = self._mouse_pressed, False
        x = event.x_root - self.winfo_rootx()
        y = event.y_root - self.winfo_rooty()
        if pressed and 0 <= x < self.winfo_width() and 0 <= y < self.winfo_height():
            self.invoke()
        return 'break'

    def _press_key(self, event):
        if self._state == 'normal' and self._key is None:
            self._key = event.keysym
        return 'break'

    def _release_key(self, event):
        if self._key == event.keysym:
            self._key = None
            self.invoke()
        return 'break'

    def _lose_focus(self, event):
        self._key = None
        self._draw()

    def _draw(self, *_):
        canvas = self._track
        canvas.delete('all')
        scale = display_scale(self)
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 34 * scale or height < 24 * scale:
            return
        active = self._variable.get()
        disabled = self._state == 'disabled'
        theme = getattr(self._root(), '_free_ai_theme', 'light')
        if disabled:
            track_fill = C['selection_bg'] if active else C['disabled_bg']
        elif theme == 'dark':
            track_fill = '#4778c6' if active else '#626b79'
        elif theme == 'cyberpunk':
            track_fill = C['accent'] if active else C['line']
        else:
            track_fill = C['primary_bg'] if active else '#b6c1d0'
        center = height / 2
        left, right = 15 * scale, width - 15 * scale
        canvas.create_line(left, center, right, center, width=22 * scale, capstyle='round',
                           fill=track_fill, tags='track')
        knob = right if active else left
        radius = 8 * scale
        canvas.create_oval(knob - radius, center - radius, knob + radius, center + radius,
                           fill='#ffffff', outline='', tags='thumb')
        if self.focus_get() is self and not disabled:
            rounded(canvas, 1, 1, width - 2, height - 2, height / 2,
                    fill='', outline=C['focus'], width=1, tags='focus')

    def _update_style(self):
        disabled = self._state == 'disabled'
        super().configure(takefocus=not disabled)
        color = self.cget('bg')
        for widget in self._surfaces:
            if widget is self:
                super().configure(cursor='arrow' if disabled else 'hand2')
            else:
                widget.configure(bg=color, cursor='arrow' if disabled else 'hand2')
        self._title.configure(fg=C['disabled_fg'] if disabled else C['text'])
        self._description.configure(fg=C['disabled_fg'] if disabled else C['muted'])
        self._draw()

    def configure(self, cnf=None, **options):
        if isinstance(cnf, str):
            return super().configure(cnf, **options)
        options = {**(cnf or {}), **options}
        if not options:
            return super().configure()
        if 'state' in options:
            state = options.pop('state')
            if state not in ('normal', 'disabled'):
                raise ValueError('SwitchRow state must be normal or disabled.')
            self._state = state
            if state == 'disabled':
                self._key, self._mouse_pressed = None, False
        if 'text' in options:
            self._title.configure(text=options.pop('text'))
        if 'description' in options:
            description = options.pop('description')
            self._description.configure(text=description)
            if description:
                self._description.pack(fill='x', pady=(round(2 * display_scale(self)), 0))
            else:
                self._description.pack_forget()
        if 'command' in options:
            self._command = options.pop('command')
        if 'variable' in options:
            self._set_variable(options.pop('variable'))
        if 'wraplength' in options:
            self._wraplength = self.winfo_pixels(options.pop('wraplength'))
            self._set_wrap(max(1, self._labels.winfo_width()))
        if options:
            super().configure(**options)
        self._update_style()

    config = configure

    def cget(self, option):
        if option == 'state':
            return self._state
        if option == 'text':
            return self._title.cget('text')
        if option == 'variable':
            return str(self._variable)
        return super().cget(option)

    def instate(self, states):
        disabled = self._state == 'disabled'
        return all(state == ('disabled' if disabled else '!disabled') for state in states)

    def invoke(self):
        if self._state == 'disabled':
            return None
        self._variable.set(not self._variable.get())
        if self._command is not None:
            return self._command()
        return None

    def destroy(self):
        if self._trace is not None:
            self._variable.trace_remove('write', self._trace)
            self._trace = None
        super().destroy()
