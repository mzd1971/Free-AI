"""Display the optional text returned by a model alongside its answer."""

import tkinter as tk
from tkinter import ttk

from app_state import REASONING_LIMIT
from ui_kit import COLORS as C, FlatButton, background, label, ui_font


class ReasoningPanel(tk.Frame):
    def __init__(self, parent, *, on_resize=None):
        color = background(parent)
        super().__init__(parent, bg=color, bd=0, highlightthickness=0)
        self._on_resize = on_resize
        self._text = ''
        self._has_content = False
        self._drawn_length = 0
        self._replace_display = False
        self._finished = False
        self._pending = None
        self.expanded = True

        self._header = FlatButton(self, '', self.toggle, variant='ghost', height=26, font_size=10,
                                  anchor='w', icon='chevron_down', icon_side='right')
        self._body = tk.Frame(self, bg=color, padx=8, pady=4)
        viewport = tk.Frame(self._body, bg=color)
        viewport.pack(fill='x')
        tk.Frame(viewport, bg=C['line'], width=2).pack(side='left', fill='y')
        self._display = tk.Text(viewport, height=3, width=1, wrap='word', state='disabled', takefocus=True,
                                bg=color, fg=C['text'], bd=0, padx=8, pady=2, spacing3=2,
                                font=ui_font(self, 10), exportselection=False,
                                selectbackground=C['selection_bg'], selectforeground=C['selection_fg'],
                                highlightthickness=1, highlightbackground=color, highlightcolor=C['focus'])
        scrollbar = ttk.Scrollbar(viewport, orient='vertical', command=self._display.yview)
        scrollbar.pack(side='right', fill='y')
        self._display.configure(yscrollcommand=scrollbar.set)
        self._display.pack(side='left', fill='both', expand=True)
        self._hint = label(self._body, '', size=9, color=C['muted'], anchor='w', justify='left', wraplength=210)
        self._hint.pack(fill='x', padx=(10, 0), pady=(2, 0))
        self._body.bind('<Configure>', self._wrap_hint, add='+')
        self.bind('<Destroy>', self._cancel_flush, add='+')

    @property
    def text(self):
        return self._text if self._has_content else ''

    def append(self, text):
        incoming = text[:REASONING_LIMIT - len(self._text)]
        if incoming:
            self._text += incoming
            self._has_content = self._has_content or bool(incoming.strip())
            self._finished = False
            if self._has_content and self._pending is None:
                self._pending = self.after_idle(self._flush)

    def set_text(self, text):
        text = text[:REASONING_LIMIT]
        if text != self._text:
            self._replace_display = self._replace_display or not text.startswith(self._text)
            self._text = text
            self._has_content = bool(text.strip())
        self._finished = False
        if self._pending is None:
            self._pending = self.after_idle(self._flush)

    def finish(self):
        self._finished = True
        self._cancel_flush()
        self._flush()

    def toggle(self):
        self.expanded = not self.expanded
        self._cancel_flush()
        self._flush()

    def _cancel_flush(self, _event=None):
        if self._pending is not None:
            self.after_cancel(self._pending)
            self._pending = None

    def _wrap_hint(self, event):
        width = max(1, event.width - 26)
        if int(self._hint.cget('wraplength')) != width:
            self._hint.configure(wraplength=width)

    def _flush(self):
        self._pending = None
        header_shown = bool(self._header.winfo_manager())
        body_shown = bool(self._body.winfo_manager())
        body_visible = self._has_content and self.expanded
        resized = header_shown != self._has_content or body_shown != body_visible

        if self._has_content:
            status = '已接收' if self._finished else '接收中'
            heading = f'模型思考 · {status}' if self.expanded else f'模型思考 · {status} · 展开'
            if self._header.value != heading:
                self._header.configure(text=heading, icon=None if self.expanded else 'chevron_down')
            if not header_shown:
                self._header.pack(anchor='w')
        elif header_shown:
            self._header.pack_forget()

        if body_visible:
            if self._replace_display or self._drawn_length < len(self._text):
                self._display.configure(state='normal')
                if self._replace_display:
                    self._display.delete('1.0', 'end')
                    self._drawn_length = 0
                    self._replace_display = False
                self._display.insert('end', self._text[self._drawn_length:])
                self._display.configure(state='disabled')
                self._drawn_length = len(self._text)
            hint = '模型返回的说明，仅供参考，以实际结果为准。'
            if len(self._text) == REASONING_LIMIT:
                hint += f'已达到 {REASONING_LIMIT:,} 字符的显示上限。'
            if self._hint.cget('text') != hint:
                self._hint.configure(text=hint)
                resized = True
            if not body_shown:
                self._body.pack(fill='x')
        elif body_shown:
            self._body.pack_forget()

        if resized and self._on_resize is not None:
            self._on_resize()
