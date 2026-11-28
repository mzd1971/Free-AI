"""Read-only inspection of the chat memory context before model use."""

import tkinter as tk
from tkinter import ttk

from ui_kit import COLORS as C, FlatButton, ScrollArea, label, ui_font


LIMITS = {'按内容容量': None, '3 条': 3, '5 条': 5, '10 条': 10}
MATCH_LEVELS = {'宽松': 1, '更相关': 3, '严格': 6}
SCOPES = {'agent': '这个助手', 'project': '当前项目', 'global': '所有助手'}


class MemoryRetrievalPanel(tk.Frame):
    def __init__(self, parent, *, preview, workspace, route):
        super().__init__(parent, bg=C['surface'])
        self.preview, self.workspace, self.route = preview, workspace, route
        self._busy, self._context, self._last_scope = False, '', None
        self.query = tk.StringVar(self)
        self.limit = tk.StringVar(self, value=next(iter(LIMITS)))
        self.match = tk.StringVar(self, value=next(iter(MATCH_LEVELS)))
        self.availability = tk.StringVar(self)
        self.summary = tk.StringVar(self)

        self.area = ScrollArea(self, bg=C['surface'], scrollbar=True)
        self.area.pack(fill='both', expand=True)
        host = self.area.body
        self._wrapped(host, '仅在本地预览，无需调用模型。问题和选项只用于测试，正式设置保持原样。').pack(
            fill='x', pady=(3, 6))
        self._wrapped(host, textvariable=self.availability, color=C['accent']).pack(fill='x', pady=(0, 8))
        label(host, '输入一个问题', size=10, bold=True).pack(anchor='w')
        self.query_input = ttk.Entry(host, textvariable=self.query, width=1, font=ui_font(host, 10))
        self.query_input.pack(fill='x', pady=(4, 6))
        self.query_input.bind('<Return>', self.run)
        self.limit_input = self._choice(host, '最多采用', self.limit, LIMITS)
        self.match_input = self._choice(host, '匹配要求', self.match, MATCH_LEVELS)
        self._wrapped(host, '匹配分按问题与记忆中的关键词计算，不是正确率。').pack(fill='x')
        self.test_button = FlatButton(host, '测试记忆', self.run, variant='primary', height=31, font_size=10)
        self.test_button.pack(anchor='w', pady=(5, 3))
        self._wrapped(host, textvariable=self.summary).pack(fill='x', pady=(3, 4))

        self.results = tk.Frame(host, bg=C['surface'])
        label(self.results, '准备给 AI 看的内容', size=10, bold=True).pack(anchor='w', pady=(4, 4))
        self.context_text = self._text(self.results, height=6)
        self.copy_button = FlatButton(self.results, '复制这段内容', self.copy_context, variant='ghost',
                                      height=28, font_size=10, state='disabled')
        self.copy_button.pack(anchor='w', pady=(3, 5))
        label(self.results, '检索结果', size=10, bold=True).pack(anchor='w', pady=(3, 4))
        self.items_text = self._text(self.results, height=10)
        self.items_text.tag_configure('heading', font=ui_font(self, 10, bold=True), foreground=C['accent'])
        self.items_text.tag_configure('title', font=ui_font(self, 10, bold=True))

        for option in (self.query, self.limit, self.match):
            option.trace_add('write', self.clear_results)
        self.area.bind_wheel(host)

    @staticmethod
    def _wrapped(parent, text='', *, color=None, **options):
        widget = label(parent, text, anchor='w', justify='left', size=10,
                       color=color or C['muted'], wraplength=270, **options)
        widget.bind('<Configure>', lambda event: widget.configure(wraplength=max(60, event.width - 12)))
        return widget

    @staticmethod
    def _choice(parent, title, variable, values):
        frame = tk.Frame(parent, bg=C['surface'])
        frame.pack(fill='x', pady=(0, 4))
        label(frame, title, size=10).pack(side='left', padx=(0, 8))
        combo = ttk.Combobox(frame, textvariable=variable, values=list(values), state='readonly',
                             width=9, font=ui_font(parent, 10))
        combo.pack(side='left', fill='x', expand=True)
        return combo

    @staticmethod
    def _text(parent, *, height):
        frame = tk.Frame(parent, bg=C['input'])
        frame.pack(fill='x')
        text = tk.Text(frame, height=height, width=1, state='disabled', wrap='word', undo=False,
                       font=ui_font(parent, 10), bg=C['input'], fg=C['text'], padx=6, pady=5, bd=0)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=text.yview)
        scroll.pack(side='right', fill='y')
        text.pack(side='left', fill='x', expand=True)
        text.configure(yscrollcommand=scroll.set)
        return text

    @staticmethod
    def _replace(text, value):
        text.configure(state='normal')
        text.delete('1.0', 'end')
        text.insert('1.0', value)
        text.configure(state='disabled')

    def sync_busy(self, busy):
        self._busy = busy
        self.test_button.configure(state='disabled' if busy else 'normal')

    def sync_route(self):
        route = self.route(self.query.get().strip())
        scope = (self.workspace(), route['budget'])
        if scope != self._last_scope:
            self.clear_results()
            self._last_scope = scope
        self.availability.set(route['message'])
        return scope

    def clear_results(self, *_):
        self.summary.set('')
        if self.results.winfo_manager():
            self.results.pack_forget()
            self._replace(self.context_text, '')
            self._replace(self.items_text, '')
        if self._context:
            self._context = ''
            self.copy_button.configure(state='disabled')

    def reset(self):
        self.query.set('')
        self.limit.set(next(iter(LIMITS)))
        self.match.set(next(iter(MATCH_LEVELS)))
        self.area.canvas.yview_moveto(0)

    def run(self, event=None):
        if self._busy:
            return 'break'
        self.clear_results()
        query = self.query.get().strip()
        workspace, budget = self.sync_route()
        if not query:
            self.summary.set('请输入要测试的问题。')
            self.query_input.focus_set()
            return 'break'
        try:
            result = self.preview(query, workspace=workspace, budget=budget,
                                  max_items=LIMITS[self.limit.get()], minimum_match=MATCH_LEVELS[self.match.get()])
        except ValueError as error:
            self.summary.set(str(error))
            return 'break'
        except Exception:
            self.summary.set('暂时无法测试记忆，请检查记录文件夹。')
            return 'break'
        self._show_result(result)
        return 'break'

    def _show_result(self, result):
        self._context = result['context']
        count = len(result['memory_ids'])
        self.summary.set(f'预览采用 {count} 条 · 使用 {result["used_bytes"]} / {result["budget"]} 字节')
        self._replace(self.context_text, self._context or '这次测试没有采用记忆。')
        self.copy_button.configure(state='normal' if self._context else 'disabled')
        self._show_items(result['items'])
        self.results.pack(fill='x', pady=(1, 6))

    def _show_items(self, items):
        text = self.items_text
        text.configure(state='normal')
        text.delete('1.0', 'end')
        if not items:
            text.insert('end', '当前范围内没有记忆。')
        for selected, heading in ((True, '已采用'), (False, '未采用')):
            group = [item for item in items if item['selected'] == selected]
            if not group:
                continue
            text.insert('end', heading + '\n', 'heading')
            for item in group:
                source = ('你保存' if item['source'] == 'user' else
                          'AI 保存 · 已核实' if item['verified'] else 'AI 保存 · 未核实')
                text.insert('end', item['title'] + '\n', 'title')
                text.insert('end', item['reason'] + '\n')
                text.insert('end', f'{source} · {SCOPES[item["scope"]]}\n')
                if item['matched_terms']:
                    terms = '、'.join(item['matched_terms'])
                    text.insert('end', f'匹配 {item["match_score"]} 分 · {terms}\n')
                if item['evidence']:
                    text.insert('end', item['evidence'] + '\n')
                text.insert('end', item['content'] + '\n\n')
        text.configure(state='disabled')

    def copy_context(self):
        if not self._context:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(self._context)
        except tk.TclError:
            self.summary.set('无法复制，请选中文字后重试。')
        else:
            self.summary.set('内容已复制。')
