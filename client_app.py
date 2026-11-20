"""Free AI client: conversations and optional capabilities, without a training workspace."""
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from app_state import ACTIVE_DRAFT_SESSION, Sessions, CONTEXTS
from capability_manager import PackageManager
from model_catalog import chat_models, is_chat_model, model_name
from app_skills import skill_catalog, get_skill, execute_skill
from api_settings import APISettings
from desktop_app import DesktopApp as ExistingActions, size_text
from epistemology import KnowledgeStore
from paths import ROOT, ARTIFACTS
from ui_kit import COLORS as C, CLIENT_SKINS, CLIENT_LIGHT_COLORS, FONT, FlatButton, RoundedFrame, ScrollArea, label, setup_theme, ui_font, set_text_size, text_scale
from ui_preferences import TEXT_SIZES, load_text_size, save_text_size


SHORTCUT_HINT = 'Ctrl + Enter 发送 · Enter 换行'
BUSY_DRAFT_HINT = '当前任务结束后再发送。你可以继续编辑草稿。'


class ClientApp(ExistingActions):
    """Reuse tested file/audio actions; own a deliberately small client interface."""
    def __init__(self, root):
        self.root = root
        self.sessions = Sessions(ARTIFACTS)
        self.theme = self.sessions.get('client_theme', 'light')
        if self.theme not in CLIENT_SKINS.values():
            self.theme = 'light'
        setup_theme(root, 'Free AI', (1440, 900), theme=self.theme, text_size=load_text_size(),
                    palette=CLIENT_LIGHT_COLORS if self.theme == 'light' else None)
        self.events, self.buttons = queue.Queue(), []
        self.busy, self.cancellable, self.downloading = False, False, None
        self.generation_cancel, self.download_cancel = threading.Event(), threading.Event()
        self.manager, self.knowledge = PackageManager(), KnowledgeStore()
        from agent_memory import AgentMemoryStore
        self.personal_memory = AgentMemoryStore()
        self.api_settings = APISettings(ARTIFACTS)
        self.backend = 'api' if self.sessions.get('client_backend') == 'api' else 'local'
        # Existing files are only a cache, not consent to choose or load a model.
        self.selected_model = self.sessions.get('client_model')
        if not is_chat_model(self.manager.catalog, self.selected_model):
            self.selected_model = None
        saved_skill = self.sessions.get('client_skill', 'general')
        self.skill = tk.StringVar(value=saved_skill if saved_skill in {s['id'] for s in skill_catalog()} else 'general')
        self.skill_file = None
        self.cap_section = 'models'
        self.session_id, self.pending_chat, self.last_episode = None, None, None
        self.draft_session_id = None
        self._saved_draft = self._draft_save_id = self._composer_size_id = None
        self._memory_drawer = self._closing = False
        self.history, self.last_answer, self.streamed = [], '', ''
        self.image_path, self.training_process = None, None
        self.tool_mode = tk.BooleanVar(value=False)
        self.last_tool_result = None
        self.context = tk.StringVar(value=self.sessions.get('context', next(iter(CONTEXTS))))
        if self.context.get() not in CONTEXTS:
            self.context.set(next(iter(CONTEXTS)))
        self.status = tk.StringVar(value='当前使用 API；发送时会将当前对话交给所填服务商。' if self.backend == 'api'
                                  else '先下载模型或连接 API，也可以直接使用本机小工具。' if not self.selected_model else '模型按需加载，打开应用不会启动推理。')
        self.model_state = tk.StringVar(value='未启用模型')
        self.chat_hint = tk.StringVar(value=SHORTCUT_HINT)
        self.download_status = tk.StringVar(value='只下载你需要的能力，聊天和经验会一直保留。')
        self.sidebar = tk.Frame(root, bg=C['side'], width=280)
        self.sidebar.pack(side='left', fill='y')
        self.sidebar.pack_propagate(False)
        brand = tk.Frame(self.sidebar, bg=C['side'])
        brand.pack(fill='x', padx=22, pady=(26, 26))
        try:
            self.brand_image = tk.PhotoImage(file=str(ROOT / 'assets/free-ai-ui.png')).subsample(2)
            brand_button = tk.Button(brand, image=self.brand_image, command=lambda: self.select_page(0),
                width=36, height=38, bg=C['side'], activebackground=C['hover'], bd=0, relief='flat',
                highlightthickness=0, cursor='hand2')
        except tk.TclError:
            brand_button = FlatButton(brand, 'F', lambda: self.select_page(0), variant='selected', width=36, height=38, font_size=18)
        brand_button.pack(side='left', padx=(0, 9))
        self.brand_label = label(brand, 'Free AI', size=19, bold=True, color=C.get('brand', C['text']))
        self.brand_label.pack(side='left')
        new_box = RoundedFrame(self.sidebar, bg=C['surface'], padding=1, radius=12, border=C['line'])
        new_box.pack(fill='x', padx=16, pady=(0, 18))
        FlatButton(new_box.body, '+   新对话', self.new_conversation, variant='ghost', height=44).pack(fill='x')
        FlatButton(self.sidebar, '◇   模型与扩展', lambda: self.select_page(1), variant='ghost', anchor='w', height=44).pack(fill='x', padx=16)
        FlatButton(self.sidebar, '▱   项目助手', self.open_project_assistant, variant='ghost', anchor='w', height=44).pack(fill='x', padx=16, pady=2)
        FlatButton(self.sidebar, '◎   长期记忆', self.open_memory, variant='ghost', anchor='w', height=44).pack(fill='x', padx=16, pady=2)
        bottom = tk.Frame(self.sidebar, bg=C['side'])
        bottom.pack(side='bottom', fill='x', padx=14, pady=18)
        FlatButton(bottom, '设置', lambda: self.select_page(2), variant='ghost', anchor='w', height=36).pack(fill='x')
        label(bottom, '本地工作空间  ·  2.8.0', size=9, color=C['muted']).pack(anchor='w', padx=16, pady=(8, 0))
        self.recent_area = ScrollArea(self.sidebar, bg=C['side'])
        self.recent_area.pack(fill='both', expand=True, padx=14)
        main = self.main = tk.Frame(root, bg=C['bg'])
        main.pack(side='left', fill='both', expand=True)
        header = self.header = tk.Frame(main, bg=C['bg'], height=58)
        header.pack(fill='x', padx=24, pady=(5, 0))
        self.page_title = tk.StringVar(value='新的对话')
        self.sidebar_button = FlatButton(header, '☰', self.toggle_sidebar, variant='ghost', width=34, height=34, font_size=13)
        self.sidebar_button.pack(side='left', padx=(0, 10))
        self.details_button = FlatButton(header, '◫', self.toggle_project_details, variant='ghost', width=36, height=34, font_size=16)
        self.details_button.pack(side='right', padx=(12, 0))
        self.memory_button = FlatButton(header, '长期记忆', self.open_memory, variant='ghost', height=34, font_size=11)
        self.memory_button.pack(side='right', padx=(8, 0))
        self.header_title = label(header, textvariable=self.page_title, size=11, anchor='w')
        self.header_title.pack(side='left', fill='x', expand=True, pady=12)
        if self.theme == 'cyberpunk':
            from ui_kit import paint_circuit
            circuit = tk.Canvas(main, bg=C['bg'], height=18, highlightthickness=0, bd=0)
            circuit.pack(fill='x', padx=24, pady=(0, 2))
            circuit.bind('<Configure>', lambda event: paint_circuit(event.widget))
        self.footer = tk.Frame(main, bg=C['bg'])
        self.footer.pack(side='bottom', fill='x', padx=24, pady=(2, 7))
        footer_notice = label(self.footer, textvariable=self.status, size=10, color=C['muted'], anchor='w', justify='left')
        footer_notice.pack(side='left', fill='x', expand=True)
        footer_notice.bind('<Configure>', lambda event: footer_notice.configure(wraplength=max(80, event.width)))
        self.stop_task_button = FlatButton(self.footer, '停止', self.stop_generation, variant='ghost', width=60, height=28)
        self.progress = ttk.Progressbar(main, mode='indeterminate', length=30)
        self.host = tk.Frame(main, bg=C['bg'])
        self.host.pack(fill='both', expand=True)
        self.pages = [tk.Frame(self.host, bg=C['bg']) for _ in range(3)]
        self.current_page = 0
        self.build_chat(self.pages[0])
        from project_chat import ProjectConversation
        self.project = ProjectConversation(self)
        from memory_panel import MemoryPanel
        self.memories = MemoryPanel(self)
        self.build_capabilities(self.pages[1])
        self.build_settings(self.pages[2])
        self.refresh_recent()
        active = self.sessions.get(ACTIVE_DRAFT_SESSION)
        if active == '':
            self.load_conversation(None)
        elif isinstance(active, str) and self.sessions.has_session(active):
            self.load_conversation(active)
        elif self.recent_rows:
            self.load_conversation(self.recent_rows[0][0])
        else:
            self.load_conversation(None)
        self.refresh_capabilities()
        self.select_page(0)
        self.apply_text_layout()
        root.bind('<<FreeAITextSizeChanged>>', lambda event: self.apply_text_layout(), add='+')
        root.bind('<Configure>', self._layout_sidebar, add='+')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = self.callback_error
        self.poll_id = root.after(60, self.poll)

    def select_page(self, index):
        if index == 2 and self.current_page != 2:
            self._sidebar_before_settings = bool(self.sidebar.winfo_manager())
            self.sidebar.pack_forget()
        elif index != 2 and self.current_page == 2 and getattr(self, '_sidebar_before_settings', False):
            self.sidebar.pack(side='left', fill='y', before=self.main)
        for page in self.pages:
            page.pack_forget()
        self.pages[index].pack(fill='both', expand=True)
        self.current_page = index
        self.page_title.set(dict(self.recent_rows).get(self.session_id, '新的对话') if index == 0 else ['对话', '能力中心', '设置'][index])
        if index == 0:
            self.layout_chat()
        elif index == 2 and hasattr(self, 'settings_panel'):
            self.settings_panel.sync_busy()

    def build_chat(self, page):
        self.show_project_details = True
        self._project_drawer = self._project_drawer_requested = False
        self.project_summary = RoundedFrame(page, bg=C['surface'], radius=16, padding=16, border=C['line'])
        self.summary_scroll = ScrollArea(self.project_summary.body, bg=C['surface'], scrollbar=True)
        self.summary_scroll.pack(fill='both', expand=True)
        self.summary_content = self.summary_scroll.body
        self.chat_stack = tk.Frame(page, bg=C['bg'])
        self.hero = tk.Frame(self.chat_stack, bg=C['bg'])
        if self.theme == 'cyberpunk':
            grid = tk.Canvas(self.hero, bg=C['bg'], highlightthickness=0, bd=0)
            grid.place(x=0, y=0, relwidth=1, relheight=1)
            def draw_grid(event):
                grid.delete('all')
                for x in range(22, event.width, 42):
                    for y in range(20, event.height, 42):
                        grid.create_oval(x, y, x+2, y+2, fill='#19393c', outline='')
            grid.bind('<Configure>', draw_grid)
        self.hero_content = tk.Frame(self.hero, bg=C['bg'])
        self.hero_content.pack(pady=(0, 24))
        hero_heading = tk.Frame(self.hero_content, bg=C['bg'])
        hero_heading.pack()
        if hasattr(self, 'brand_image'):
            label(hero_heading, image=self.brand_image).pack(side='left', padx=(0, 12))
        self.hero_title = label(hero_heading, '从一个想法开始', size=22, bold=True, wraplength=650, justify='center')
        self.hero_title.pack(side='left')
        self.transcript_area = tk.Frame(self.chat_stack, bg=C['bg'])
        transcript_scroll = ttk.Scrollbar(self.transcript_area, orient='vertical')
        transcript_scroll.pack(side='right', fill='y')
        self.transcript = tk.Text(self.transcript_area, bg=C['bg'], fg=C['text'], wrap='word', bd=0,
             highlightthickness=0, padx=16, pady=18, font=ui_font(self.root, 12), spacing1=2, spacing2=5, spacing3=5,
             selectbackground=C['selection_bg'], selectforeground=C['selection_fg'], state='disabled', height=8)
        self.transcript.pack(side='left', fill='both', expand=True)
        self.transcript.configure(yscrollcommand=transcript_scroll.set)
        transcript_scroll.configure(command=self.transcript.yview)
        self.transcript.tag_configure('user', font=ui_font(self.root, 11, bold=True), foreground=C['text'], spacing1=20, spacing3=8)
        self.transcript.tag_configure('assistant', foreground=C.get('secondary', C['muted']), font=ui_font(self.root, 10), spacing1=20, spacing3=8)
        self.transcript.tag_configure('md_marker', elide=True)
        self.transcript.tag_configure('cjk', wrap='char')
        self.transcript.tag_configure('md_heading', font=ui_font(self.root, 15, bold=True), spacing1=12, spacing3=6)
        self.transcript.tag_configure('md_bold', font=ui_font(self.root, 12, bold=True))
        self.transcript.tag_configure('md_code', font=ui_font(self.root, 11, family='Consolas'), background=C['side'], spacing1=2, spacing3=2)
        self.context_bar = tk.Frame(self.chat_stack, bg=C['bg'])
        self.context_bar.pack(fill='x', padx=6, pady=(0, 10))
        self.composer = RoundedFrame(self.chat_stack, bg=C['input'], radius=32, padding=(16, 13), border=C['line'])
        self.composer.pack(fill='x')
        self.composer.bind('<Configure>', self._schedule_composer_size, add='+')
        self.attachment_bar = tk.Frame(self.composer.body, bg=C['input'])
        self.attachment_label = label(self.attachment_bar, size=10, color=C['muted'], justify='left', anchor='w')
        self.attachment_label.pack(side='left', fill='x', expand=True)
        FlatButton(self.attachment_bar, '移除附件', self.clear_attachments, variant='ghost',
                   height=26, font_size=10).pack(side='right', padx=(6, 0))
        self.input_frame = tk.Frame(self.composer.body, bg=C['input'])
        self.input_frame.pack(fill='x')
        self.input = tk.Text(self.input_frame, bg=C['input'], fg=C['text'], font=ui_font(self.root, 12),
            height=2, width=1, wrap='char', bd=0, highlightthickness=0, insertbackground=C['accent'],
            padx=3, pady=7, undo=True)
        self.input.pack(side='left', fill='both', expand=True)
        self.input_scrollbar = ttk.Scrollbar(self.input_frame, command=self.input.yview, orient='vertical')
        self.input.configure(yscrollcommand=self.input_scrollbar.set)
        self.placeholder = label(self.input, '描述你想完成的事…', size=12, color=C['muted'], cursor='xterm', anchor='w')
        self.placeholder.configure(padx=0, pady=0)
        self.update_placeholder()
        self.placeholder.bind('<Button-1>', lambda e: self.input.focus_set())
        self.input.bind('<<Modified>>', self._draft_text_changed)
        self.input.bind('<Configure>', self._schedule_composer_size)
        self.input.bind('<FocusIn>', lambda e: self.update_placeholder())
        self.input.bind('<FocusOut>', lambda e: self.update_placeholder())
        self.input.bind('<Control-Return>', self.enter_send)
        self.input.bind('<Control-KP_Enter>', self.enter_send)
        tools = self.composer_tools = tk.Frame(self.composer.body, bg=C['input'])
        tools.pack(fill='x', pady=(12, 0))
        self.add_button = FlatButton(tools, '+', self.attachment_menu, variant='ghost', width=34, height=34, font_size=20)
        self.add_button.pack(side='left')
        self.buttons.append(self.add_button)
        self.model_button = FlatButton(tools, '选择模型', self.model_menu, variant='ghost', width=130, max_width=240, height=34, font_size=10)
        self.model_button.pack(side='left', padx=(4, 0))
        self.context_button = FlatButton(tools, self.context.get().split(' · ')[0] + ' ▾', self.context_menu,
                                        variant='ghost', width=100, height=34, font_size=10)
        self.context_button.pack(side='left')
        self.skill_button = FlatButton(tools, get_skill(self.skill.get())['title'] + ' ▾', self.skill_menu,
                                       variant='ghost', width=100, height=34, font_size=10)
        self.skill_button.pack(side='left')
        self.send_button = FlatButton(tools, '↑', self.send_or_stop, width=40, height=40, radius=20, font_size=20, variant='primary')
        self.send_button.pack(side='right')
        self.more_button = FlatButton(tools, '···', self.answer_menu, variant='ghost', width=40, height=34, font_size=18)
        self.more_button.pack(side='right', padx=8)
        tools.bind('<Configure>', lambda event: self.layout_composer_tools())
        self.busy_draft_label = label(self.composer.body, BUSY_DRAFT_HINT, size=10, color=C['muted'],
                                      anchor='w', justify='left')
        self.hint_label = label(self.chat_stack, textvariable=self.chat_hint, size=10, color=C['muted'], wraplength=760)
        self.hint_label.pack(pady=(7, 0))
        self.chat_hint.trace_add('write', lambda *_: self._schedule_composer_size())
        self.setup_button = FlatButton(self.chat_stack, '下载模型或连接 API', lambda: self.show_capabilities('models'),
                                      variant='accent', width=238, height=34, font_size=10)
        page.bind('<Configure>', lambda e: self.layout_chat())

    def apply_text_layout(self):
        scale = text_scale(self.root)
        self._layout_sidebar()
        if hasattr(self, 'settings_panel'):
            self.settings_panel.navigation.configure(width=round(156 * scale))
        self.layout_chat()
        self.layout_composer_tools()
        if hasattr(self, 'project'):
            self.project.resize_cards()

    def _layout_sidebar(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        width = round(min(280 * min(text_scale(self.root), 1.2), max(260, self.root.winfo_width() * .24)))
        if int(self.sidebar.cget('width')) != width:
            self.sidebar.configure(width=width)

    def change_text_size(self, event=None):
        choice = TEXT_SIZES.get(self.text_size_choice.get())
        if choice:
            try:
                save_text_size(choice)
                set_text_size(self.root, choice)
                self.apply_text_layout()
                self.status.set('文字大小已更新。训练端下次打开也会使用这个大小。')
            except (OSError, ValueError) as exc:
                self.status.set('无法保存文字大小：' + str(exc))

    def layout_composer_tools(self):
        tools = self.composer_tools
        width = tools.winfo_width()
        permission = self.project.permissions_button if hasattr(self, 'project') else None
        project_mode = bool(permission and self.project.enabled.get())
        visible = [self.add_button, self.more_button, self.model_button, self.send_button]
        if project_mode:
            visible.insert(1, permission)
        widths = tuple(button.winfo_reqwidth() for button in visible)
        mode = 'tight' if width < sum(widths) + 28 else 'full'
        layout = (mode, widths, project_mode)
        if getattr(self, '_composer_layout', None) == layout:
            return
        self._composer_layout = layout
        for button in (*visible, self.context_button, self.skill_button, *([permission] if permission else [])):
            button.pack_forget()
            button.grid_forget()
        for index in range(7):
            tools.columnconfigure(index, weight=0)
        tools.columnconfigure(3, weight=1)
        self.add_button.grid(row=0, column=0, sticky='w')
        self.more_button.grid(row=0, column=2, sticky='w', padx=2)
        self.send_button.grid(row=0, column=6, sticky='e', padx=(8, 0))
        if project_mode:
            permission.grid(row=0, column=1, sticky='w', padx=3)
        if mode == 'tight':
            self.model_button.grid(row=1, column=0, columnspan=7, sticky='e', pady=(4, 0))
        else:
            self.model_button.grid(row=0, column=5, sticky='e')
        self._schedule_composer_size()

    def _schedule_composer_size(self, _event=None):
        if self._composer_size_id is None and not self._closing:
            self._composer_size_id = self.root.after_idle(self._resize_composer)

    def _resize_composer(self):
        self._composer_size_id = None
        # Hidden pages keep stale allocated sizes; comparing them with requested
        # sizes can keep re-queuing idle callbacks and freeze the settings page.
        if self._closing or self.current_page != 0 or not self.pages[0].winfo_ismapped():
            return
        if self.input.winfo_width() < 32 or self.chat_stack.winfo_height() < 32:
            return
        lines = self.input.count('1.0', '1.0 + 9 display lines', 'displaylines')
        displayed = lines[0] if lines else 1
        line_height = ui_font(self.input, 12).metrics('linespace')
        chrome = max(0, self.composer.winfo_reqheight() - self.input.winfo_reqheight())
        padding = 2 * int(self.input.cget('pady'))
        available = (self.pages[0].winfo_height() * 35 // 100 - chrome - padding) // line_height
        height = min(max(2, displayed), max(2, min(8, available)))
        if int(self.input.cget('height')) != height:
            self.input.configure(height=height)
            self.input.see('insert')
        scroll = displayed > height
        if scroll and not self.input_scrollbar.winfo_manager():
            self.input_scrollbar.pack(side='right', fill='y')
        elif not scroll and self.input_scrollbar.winfo_manager():
            self.input_scrollbar.pack_forget()
        self.update_placeholder()
        if self._memory_drawer:
            self._place_memory_drawer()
        elif self._project_drawer:
            self._place_chat_drawer(self.project_summary, round(260 * text_scale(self.root)))
        elif not self.history and abs(self.chat_stack.winfo_height()
                - min(max(200, self.pages[0].winfo_height()) - 16,
                      max(1, self.chat_stack.winfo_reqheight()))) > 2:
            self.layout_chat()

    def _sync_composer_activity(self):
        if self.busy:
            self.busy_draft_label.pack(fill='x', pady=(4, 0))
        else:
            self.busy_draft_label.pack_forget()
        self._schedule_composer_size()

    def update_placeholder(self):
        if self.input.index('end-1c') != '1.0' or self.root.focus_get() is self.input:
            self.placeholder.place_forget()
        else:
            line = self.input.dlineinfo('1.0')
            x, y = line[:2] if line else (
                self.input.winfo_pixels(self.input.cget('padx')),
                self.input.winfo_pixels(self.input.cget('pady')))
            # Text's placement border already includes its padding. Use widget
            # coordinates so the hint shares the actual text origin. Hide it
            # while editing, including before an IME commits its first character.
            self.placeholder.place(x=x, y=y, bordermode='ignore')

    def layout_chat(self):
        if not hasattr(self, 'chat_stack'):
            return
        page_width = self.pages[0].winfo_width()
        project_mode = hasattr(self, 'project') and self.project.enabled.get()
        memory_open = hasattr(self, 'memories') and self.memories.opened
        scale = text_scale(self.root)
        memory_width = round(328 * scale)
        self._memory_drawer = bool(memory_open and page_width < memory_width + round(420 * scale) + 54)
        self._project_drawer = bool(project_mode and self.show_project_details and not memory_open
                                    and page_width < 1000 * scale and self._project_drawer_requested)
        aside = (0 if self._memory_drawer else memory_width) if memory_open else (
            round(220 * scale) if project_mode and self.show_project_details and page_width >= 1000 * scale else 0)
        available = page_width - aside
        cap = round((1060 if self.history else 1420) * min(scale, 1.1))
        preferred = min(cap, max(round(760 * min(scale, 1.1)), round(available * .68))) if not self.history else cap
        width = max(260, min(preferred, available - (64 if available >= 900 else 32)))
        if memory_open:
            self.project_summary.place_forget()
            if not self._memory_drawer:
                self.memories.frame.place(relx=1, x=-10, y=8, anchor='ne', width=aside - 10,
                                          height=0, relheight=.97)
        elif aside:
            self.project_summary.place(relx=1, x=-14, y=14, anchor='ne', width=aside - 8,
                                       height=min(max(80, self.pages[0].winfo_height() - 28),
                                                  self.summary_content.winfo_reqheight() + 32))
        else:
            self.project_summary.place_forget()
        if hasattr(self, 'memories') and not memory_open:
            self.memories.frame.place_forget()
        has_conversation = bool(self.history) or self._memory_drawer or self._project_drawer
        page_height = max(200, self.pages[0].winfo_height())
        if has_conversation:
            height, y = page_height - 16, 4
        else:
            height = min(page_height - 16, max(1, self.chat_stack.winfo_reqheight()))
            y = max(8, round((page_height - height) * .53))
        self.chat_stack.place(x=max(12, (available - width) // 2), y=y, width=width, height=height, relheight=0)
        self.hint_label.configure(wraplength=width - 24)
        self.hero_title.configure(wraplength=max(200, width - 32))
        self.placeholder.configure(wraplength=max(180, width - 50))
        self.busy_draft_label.configure(wraplength=max(80, width - 32))
        self.attachment_label.configure(wraplength=max(80, width - 140))
        if self.history:
            self.hero.pack_forget()
            self.transcript_area.pack(fill='both', expand=True, before=self.context_bar, pady=(0, 16))
        else:
            self.transcript_area.pack_forget()
            if self._memory_drawer or self._project_drawer:
                self.hero.pack(fill='both', expand=True, before=self.context_bar)
            else:
                self.hero.pack(fill='x', expand=False, before=self.context_bar, pady=(0, 12))
        if self._memory_drawer:
            self._place_memory_drawer()
        elif self._project_drawer:
            self._place_chat_drawer(self.project_summary, round(260 * scale))
        self._schedule_composer_size()

    def _place_memory_drawer(self):
        self._place_chat_drawer(self.memories.frame, round(328 * text_scale(self.root)))

    def _place_chat_drawer(self, frame, preferred_width):
        page = self.pages[0]
        width = min(preferred_width, max(160, page.winfo_width() - 24))
        controls_top = self.context_bar.winfo_rooty() - page.winfo_rooty()
        height = max(1, controls_top - 14)
        frame.place(relx=1, x=-10, y=8, anchor='ne', width=width, height=height, relheight=0)
        frame.lift()

    def toggle_sidebar(self):
        if self.current_page == 2:
            self.settings_panel.toggle_navigation()
            return
        if self.sidebar.winfo_manager():
            self.sidebar.pack_forget()
        else:
            self.sidebar.pack(side='left', fill='y', before=self.main)

    def open_memory(self):
        self.memories.toggle()

    def toggle_project_details(self):
        if not self.project.enabled.get():
            self.status.set('切换到“项目助手”后，这里会显示项目文件和执行结果。')
            return
        self.show_project_details = not bool(self.project_summary.winfo_manager())
        self._project_drawer_requested = self.show_project_details
        if self.memories.opened:
            self.memories.toggle()
        self.layout_chat()

    def refresh_project_summary(self):
        if not hasattr(self, 'project'):
            return
        for child in self.summary_content.winfo_children():
            child.destroy()
        def section(title, text):
            label(self.summary_content, title, size=10, color=C['muted']).pack(anchor='w', pady=(10, 7))
            detail = label(self.summary_content, text, size=11, wraplength=178, justify='left', anchor='w')
            detail.pack(fill='x')
            detail.bind('<Configure>', lambda event: event.widget.configure(wraplength=max(80, event.width)))
        workspace = self.project.workspace.get()
        section('当前项目', Path(workspace).name if workspace else '尚未选择文件夹')
        if workspace:
            FlatButton(self.summary_content, '打开文件夹 ↗', lambda: self.open_project_folder(workspace),
                       variant='ghost', height=30, font_size=10).pack(anchor='w', pady=(5, 2))
        cards = self.project.cards
        latest = self.project.active or (cards[-1] if cards else None)
        section('执行状态', latest.status.get() if latest else '发送一个目标，开始处理')
        files = list(dict.fromkeys(row['path'] for card in cards if card.workspace == workspace
                   for row in (card.result or {}).get('changes', [])))
        tk.Frame(self.summary_content, height=1, bg=C['line']).pack(fill='x', pady=(14, 0))
        section('修改过的文件', '\n'.join('▱ ' + path for path in files[-8:]) or '暂时没有修改')
        if latest:
            checks = latest.activity.count('退出码：0\n')
            failures = latest.activity.count('退出码：') - checks
            section('本轮命令结果', f'{checks} 次正常结束 · {failures} 次需检查' if checks or failures else '暂无命令结果')
            if '[已撤回文件修改]' in latest.activity:
                label(self.summary_content, '已执行撤回，详情见聊天记录。', size=10,
                      color=C['muted'], wraplength=178, justify='left').pack(anchor='w', pady=(8, 0))
        self.summary_scroll.bind_wheel(self.summary_content)
        self.layout_chat()
        self.root.after_idle(self.layout_chat)

    def open_project_folder(self, folder):
        try:
            os.startfile(folder)
        except OSError:
            self.status.set('项目文件夹无法打开，请检查它是否已移动。')

    def enter_send(self, event):
        # Plain Return belongs to Tk and the input method. Windows reserves
        # VK_PROCESSKEY for IME processing, even if a modifier is present.
        if getattr(event, 'keycode', None) == 229:
            return None
        if self.busy:
            self.chat_hint.set(BUSY_DRAFT_HINT)
        else:
            self.send_chat()
        return 'break'

    def _draft_text_changed(self, _event=None):
        if self.input.edit_modified():
            self.input.edit_modified(False)
            self.update_placeholder()
            self._schedule_composer_size()
            self._schedule_draft_save()

    def _schedule_draft_save(self):
        self._cancel_draft_save()
        self._draft_save_id = self.root.after(250, self.flush_draft)

    def _cancel_draft_save(self):
        if self._draft_save_id is not None:
            self.root.after_cancel(self._draft_save_id)
            self._draft_save_id = None

    def flush_draft(self):
        self._cancel_draft_save()
        if self._saved_draft is None:
            return True
        value = (self.input.get('1.0', 'end-1c'), self.image_path, self.skill_file, self.skill.get())
        if value == self._saved_draft:
            return True
        try:
            self.sessions.save_draft(self.draft_session_id, *value)
        except (sqlite3.Error, OSError, ValueError):
            self.status.set('草稿未能保存，当前输入仍保留。请检查数据目录后重试。')
            return False
        self._saved_draft = value
        return True

    def _replace_draft_text(self, value):
        self.input.delete('1.0', 'end')
        if value:
            self.input.insert('1.0', value)
        self.input.edit_reset()
        self.input.edit_modified(False)
        self.update_placeholder()
        self._schedule_composer_size()

    def _refresh_draft_attachments(self):
        names = []
        for title, path in (('图片', self.image_path), ('CSV', self.skill_file)):
            if path:
                name = Path(path).name
                names.append(f'{title}：{name[:32] + "…" if len(name) > 32 else name}')
        if names:
            self.attachment_label.configure(text='  ·  '.join(names))
            self.attachment_bar.pack(fill='x', before=self.input_frame, pady=(0, 3))
        else:
            self.attachment_bar.pack_forget()
        self._schedule_composer_size()

    def _restore_draft(self, identity, draft):
        text, self.image_path, self.skill_file, skill_id = draft
        if skill_id is None and self.skill.get() == 'csv_summary' and not self.skill_file:
            skill_id = 'general'
        if skill_id in {entry['id'] for entry in skill_catalog()}:
            self.skill.set(skill_id)
            self.skill_button.configure(text=get_skill(skill_id)['title'] + ' ▾')
        self.draft_session_id = identity
        self._replace_draft_text(text)
        self._refresh_draft_attachments()
        self._saved_draft = (text, self.image_path, self.skill_file, self.skill.get())

    def clear_submitted_draft(self):
        """Reset the editor after the message and draft consumption commit together."""
        self._cancel_draft_save()
        self.draft_session_id = self.session_id
        self._replace_draft_text('')
        self.image_path = self.skill_file = None
        self._refresh_draft_attachments()
        self._saved_draft = ('', None, None, self.skill.get())

    def fill_prompt(self, text):
        if self.busy:
            return
        position = 'end-1c' if self.input.index('end-1c') != '1.0' else '1.0'
        self.input.insert(position, ('\n\n' if position == 'end-1c' else '') + text)
        self.input.focus_set()

    def attachment_menu(self):
        menu = tk.Menu(self.root, tearoff=0)
        for title, action in [('添加文字资料', self.attach_text), ('添加图片', self.attach_image),
                              ('选择 CSV 文件做统计', self.choose_skill_file), ('移除已选文件', self.clear_attachments),
                              ('录音转成文字', self.attach_audio), ('画一张图', self.draw_image)]:
            menu.add_command(label=title, command=action)
        if self.image_path and self.skill_file:
            menu.add_command(label='仅移除图片', command=lambda: self._remove_draft_attachment('image_path'))
            menu.add_command(label='仅移除 CSV', command=lambda: self._remove_draft_attachment('skill_file'))
        menu.add_separator()
        menu.add_checkbutton(label='工具任务：计算 / 训练摘要', variable=self.tool_mode, command=self.mode_changed)
        menu.tk_popup(self.add_button.winfo_rootx(), self.add_button.winfo_rooty()+38)

    def mode_changed(self):
        if self.tool_mode.get() and self.project.enabled.get():
            self.project.set_mode(False)
        if self.tool_mode.get():
            self.skill.set('general')
            self.sessions.set('client_skill', 'general')
            self.skill_button.configure(text=get_skill('general')['title'] + ' ▾')
        self.chat_hint.set('工具任务只支持计算和读取本项目训练摘要。其他问题会给出建议。' if self.tool_mode.get()
                           else SHORTCUT_HINT)
        self._schedule_draft_save()
        self.refresh_model_state()

    def refresh_model_state(self):
        if hasattr(self, 'project') and self.project.enabled.get():
            self.project.refresh()
        elif hasattr(self, 'memories'):
            self.memories.refresh_mode()
        if self.backend == 'api':
            profile = self.api_settings.public()
            self.model_state.set('联网 API · ' + profile['model'] if profile['key_available'] else '联网 API · 请填写密钥')
            self.model_button.configure(text='API · 联网 ▾')
            return
        # Inspect an already-created runtime; never create/load one to display status.
        import qwen_chat
        instance = getattr(qwen_chat, '_runtime', None)
        process = getattr(instance, '_process', None)
        loaded = process is not None and process.poll() is None
        self.model_state.set('模型运行中' if loaded else '已选模型 · 尚未加载' if self.selected_model else '未启用模型')
        self.model_button.configure(text=model_name(self.manager.catalog, self.selected_model) if self.selected_model else '选择模型')

    def model_menu(self):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=('✓ ' if self.backend == 'api' else '') + '联网 API · ' + self.api_settings.public()['model'], command=self.enable_api)
        menu.add_command(label='配置 API 连接', command=self.show_api_settings)
        menu.add_separator()
        for spec in chat_models(self.manager.catalog):
            identity = spec['id']
            ready = self.manager.ready(identity)
            title = ('✓ ' if self.backend == 'local' and identity == self.selected_model else '') + model_name(self.manager.catalog, identity)
            menu.add_command(label=title + ('' if ready else ' · 未下载'),
                command=(lambda i=identity: self.enable_model(i)) if ready else lambda: self.show_capabilities('models'))
        menu.add_separator()
        menu.add_command(label='查看全部模型与下载大小', command=lambda: self.show_capabilities('models'))
        menu.tk_popup(self.model_button.winfo_rootx(), self.model_button.winfo_rooty()+36)

    def show_api_settings(self):
        self.select_page(2)
        self.settings_panel.select_category('models')
        self.root.update_idletasks()
        self.settings_area.canvas.yview_moveto(0)

    def make_api_client(self):
        from remote_api import APIClient
        profile, key = self.api_settings.connection()
        return APIClient(profile['base_url'], profile['model'], key,
                         max_output_tokens=profile['max_output_tokens'],
                         thinking=profile['thinking'] if profile['provider'] == 'deepseek' else None)

    def enable_api(self):
        if self.busy:
            self.status.set('当前任务结束后再切换到 API。')
            return
        try:
            self.make_api_client()  # Validate configuration only; no network request.
        except Exception:
            self.show_api_settings()
            self.status.set('请先填写有效的 API 地址、模型名和密钥。你的输入已保留。')
            return
        from qwen_chat import shutdown
        shutdown()
        self.backend = 'api'
        self.sessions.set('client_backend', 'api')
        self.refresh_capabilities()
        self.select_page(0)
        from urllib.parse import urlsplit
        host = urlsplit(self.api_settings.public()['base_url']).netloc
        self.status.set('已选择联网 API。发送消息时，当前对话会发往 ' + host + '；服务商可能计费。')

    def use_local_mode(self):
        if self.busy:
            self.status.set('当前任务结束后再切回本机。')
            return
        self.backend = 'local'
        self.sessions.set('client_backend', 'local')
        self.refresh_capabilities()
        self.select_page(0)
        self.status.set('已切回本机模式。选择已下载的模型后再发送，也可以直接使用本机小工具。')

    def skill_menu(self, anchor=None):
        menu = tk.Menu(self.root, tearoff=0)
        for spec in skill_catalog():
            menu.add_command(label=('✓ ' if self.skill.get() == spec['id'] else '') + spec['title'] +
                             (' · 无需模型' if spec['kind'] == 'tool' else ''),
                             command=lambda i=spec['id']: self.select_skill(i))
        menu.add_separator()
        menu.add_command(label='查看技能说明', command=lambda: self.show_capabilities('skills'))
        anchor = anchor or self.skill_button
        menu.tk_popup(anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height())

    def select_skill(self, identity):
        if self.busy:
            self.status.set('当前任务结束后再切换技能。')
            return
        spec = get_skill(identity)
        if self.project.enabled.get():
            self.project.set_mode(False)
        self.skill.set(identity)
        self.sessions.set('client_skill', identity)
        self.tool_mode.set(False)
        self.skill_button.configure(text=spec['title'] + ' ▾')
        self.chat_hint.set(spec['input_hint'])
        self.status.set(spec['description'])
        self._schedule_draft_save()
        self.refresh_capabilities()
        self.select_page(0)

    def choose_skill_file(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title='选择要统计的 CSV 文件', filetypes=[('CSV 表格', '*.csv')])
        if path:
            self.select_skill('csv_summary')
            self.skill_file = str(Path(path).absolute())
            self.chat_hint.set('已选择 ' + Path(path).name + ' · 点击发送开始统计，文件不会上传。')
            self._refresh_draft_attachments()
            self._schedule_draft_save()

    def attach_image(self):
        super().attach_image()
        self._refresh_draft_attachments()
        self._schedule_draft_save()

    def _remove_draft_attachment(self, attribute):
        setattr(self, attribute, None)
        self._refresh_draft_attachments()
        self._schedule_draft_save()

    def clear_attachments(self):
        self.image_path = self.skill_file = None
        self._refresh_draft_attachments()
        self._schedule_draft_save()
        self.chat_hint.set('已移除所选文件，输入内容仍保留。')

    def enable_model(self, identity='text-default'):
        if self.busy:
            self.status.set('当前任务结束后再选择模型。')
            return
        if not is_chat_model(self.manager.catalog, identity):
            self.status.set('请选择聊天模型，训练模型在独立训练端管理。')
            return
        if not self.manager.ready(identity):
            self.show_capabilities('models')
            self.status.set('请先下载推荐模型，下载完成后再点击“使用”。')
            return
        if self.selected_model != identity:
            from qwen_chat import shutdown
            shutdown()
        self.selected_model = identity
        self.backend = 'local'
        self.sessions.set('client_backend', 'local')
        self.sessions.set('client_model', self.selected_model)
        self.refresh_capabilities()
        self.select_page(0)
        self.status.set('已选择 ' + model_name(self.manager.catalog, identity) + '。发送第一条消息时才加载，不会重复下载。')

    def disable_model(self):
        if self.busy:
            self.status.set('请先停止当前任务，再停用模型。')
            return
        from qwen_chat import shutdown
        shutdown()
        self.selected_model = None
        self.backend = 'local'
        self.sessions.set('client_backend', 'local')
        self.sessions.set('client_model', None)
        self.refresh_capabilities()
        self.status.set('已停用模型，文件仍保留，可以随时再次使用。')

    def release_model(self):
        if self.busy:
            self.status.set('先停止当前任务，再释放模型。')
            return
        from qwen_chat import shutdown
        shutdown()
        self.refresh_model_state()
        self.status.set('本机模型已释放，当前仍选择联网 API。' if self.backend == 'api'
                        else '模型已释放。已选择的模型会在下次发送消息时加载。')

    def answer_menu(self):
        menu = tk.Menu(self.root, tearoff=0)
        state = 'disabled' if self.busy else 'normal'
        menu.add_command(label='对话长度…', state=state, command=lambda: self.context_menu(self.more_button))
        if not self.project.enabled.get():
            menu.add_command(label='选择技能…', state=state, command=lambda: self.skill_menu(self.more_button))
        menu.add_separator()
        ready = 'normal' if self.last_answer and not self.busy else 'disabled'
        for title, action in [('复制回答', self.copy_answer), ('保存回答', self.export_answer),
                              ('读给我听', self.read_aloud), ('纠正这次回答', self.open_epistemology)]:
            menu.add_command(label=title, command=action, state=ready)
        if self.last_tool_result:
            menu.add_command(label='查看实际执行记录', command=lambda: self.show_details('实际执行记录', self.last_tool_result))
        menu.tk_popup(self.more_button.winfo_rootx(), self.more_button.winfo_rooty()+36)

    def context_menu(self, anchor=None):
        menu = tk.Menu(self.root, tearoff=0)
        for value in CONTEXTS:
            menu.add_radiobutton(label=value, variable=self.context, value=value, command=self.change_context)
        anchor = anchor or self.context_button
        menu.tk_popup(anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height())

    def change_context(self, event=None):
        super().change_context(event)
        self.context_button.configure(text=self.context.get().split(' · ')[0] + ' ▾')

    def refresh_recent(self):
        self.recent_rows = self.sessions.recent()
        if hasattr(self, 'page_title') and self.current_page == 0:
            self.page_title.set(dict(self.recent_rows).get(self.session_id, '新的对话'))
        for child in self.recent_area.body.winfo_children():
            child.destroy()
        if not self.recent_rows:
            label(self.recent_area.body, '你的对话会出现在这里', size=10, color=C['muted']).pack(anchor='w', padx=14, pady=8)
        projects, chats = {}, []
        for identity, title in self.recent_rows:
            folder = self.sessions.project_settings(identity).get('workspace')
            if folder:
                projects.setdefault(folder, []).append((identity, title))
            else:
                chats.append((identity, title))
        def conversation(identity, title, indent=False):
            button = FlatButton(self.recent_area.body, title, lambda i=identity: self.choose_session(i),
                                variant='selected' if identity == self.session_id else 'ghost', anchor='w', height=38, font_size=11)
            button.pack(fill='x', pady=1, padx=(8 if indent else 0, 0))
        if projects:
            label(self.recent_area.body, '项目', size=10, color=C['muted']).pack(anchor='w', padx=13, pady=(18, 8))
        for folder, rows in projects.items():
            label(self.recent_area.body, '▱  ' + Path(folder).name[:14], size=11).pack(anchor='w', padx=13, pady=(10, 6))
            for identity, title in rows:
                conversation(identity, title, True)
        if chats:
            label(self.recent_area.body, '最近对话', size=10, color=C['muted']).pack(anchor='w', padx=13, pady=(20, 8))
        for identity, title in chats:
            conversation(identity, title)
        self.recent_area.bind_wheel(self.recent_area.body)

    def new_conversation(self):
        if self.busy:
            self.status.set('先停止当前回答，再开始新的对话。')
            return
        if self.load_conversation(None):
            self.select_page(0)

    def choose_session(self, identity):
        if self.busy:
            self.status.set('先停止当前回答，再切换对话。')
            return
        if self.load_conversation(identity):
            self.select_page(0)

    def load_conversation(self, identity):
        if not self.flush_draft():
            return False
        try:
            draft = self.sessions.draft(identity)
            history = self.sessions.messages(identity) if identity else []
            self.sessions.set(ACTIVE_DRAFT_SESSION, identity if identity is not None else '')
        except (sqlite3.Error, OSError, ValueError):
            self.status.set('无法读取对话草稿，当前输入仍保留。')
            return False
        self.session_id = identity
        self.history = history
        self.last_episode = None
        self.last_tool_result = None
        self.last_answer = next((m['content'] for m in reversed(self.history) if m['role'] == 'assistant'), '')
        self._restore_draft(identity, draft)
        project_rows = self.project.load(identity)
        self.write(self.transcript, '')
        for index, message in enumerate(self.history):
            self.append_heading('你' if message['role'] == 'user' else 'Free AI', message['role'])
            if index in project_rows:
                self.project.restore(project_rows[index])
            self.write(self.transcript, message['content'] + '\n\n', True)
        self.update_placeholder()
        self.refresh_recent()
        self.layout_chat()
        self.format_transcript()
        self.memories.refresh()
        return True

    def format_transcript(self):
        # Style the existing text in place: copying, persistence and undo still use its exact source.
        text = self.transcript
        for tag in ('md_marker', 'md_heading', 'md_bold', 'md_code', 'cjk'):
            text.tag_remove(tag, '1.0', 'end')
        assistant_lines = {int(str(index).split('.')[0]) for index in text.tag_ranges('assistant')[::2]}
        user_lines = {int(str(index).split('.')[0]) for index in text.tag_ranges('user')[::2]}
        assistant, code = False, False
        for number in range(1, int(text.index('end-1c').split('.')[0]) + 1):
            line = text.get(f'{number}.0', f'{number}.end')
            if re.search(r'[\u3400-\u9fff]', line) and not code:
                text.tag_add('cjk', f'{number}.0', f'{number}.end')
            if number in user_lines or number in assistant_lines:
                assistant, code = number in assistant_lines, False
                continue
            if not assistant:
                continue
            if line.strip().startswith('```'):
                text.tag_add('md_marker', f'{number}.0', f'{number + 1}.0')
                code = not code
                continue
            if code:
                text.tag_add('md_code', f'{number}.0', f'{number}.end')
                continue
            heading = re.match(r'^(#{1,6})\s+', line)
            if heading:
                text.tag_add('md_marker', f'{number}.0', f'{number}.{heading.end()}')
                text.tag_add('md_heading', f'{number}.{heading.end()}', f'{number}.end')
            for pattern, tag, delimiter in ((r'\*\*(.+?)\*\*', 'md_bold', 2), (r'`([^`]+)`', 'md_code', 1)):
                for match in re.finditer(pattern, line):
                    start, end = match.span()
                    text.tag_add('md_marker', f'{number}.{start}', f'{number}.{start + delimiter}')
                    text.tag_add('md_marker', f'{number}.{end - delimiter}', f'{number}.{end}')
                    text.tag_add(tag, f'{number}.{start + delimiter}', f'{number}.{end - delimiter}')

    def append_heading(self, text, role):
        self.transcript.configure(state='normal')
        self.transcript.insert('end', text + '\n', role)
        self.transcript.configure(state='disabled')

    def send_or_stop(self):
        self.stop_generation() if self.busy else self.send_chat()

    def send_chat(self):
        if self.busy:
            self.chat_hint.set(BUSY_DRAFT_HINT)
            return
        if self.project.enabled.get():
            self.project.send()
            return
        skill_id = self.skill.get()
        skill = get_skill(skill_id)
        direct_tool = skill['kind'] == 'tool'
        use_api = self.backend == 'api' and not direct_tool
        api_client = None
        api_memory = False
        skill_file = self.skill_file if skill_id == 'csv_summary' else None
        message = self.input.get('1.0', 'end').strip()
        if skill_id == 'csv_summary':
            if not skill_file:
                self.chat_hint.set('先点击 +，选择要统计的 CSV 文件，然后发送。')
                return
            if not Path(skill_file).is_file():
                self.chat_hint.set('找不到上次选择的 CSV 文件。请重新选择，当前草稿仍保留。')
                return
            message = ('统计 CSV 文件：' + Path(skill_file).name + '\n' + message).strip()
        if not message:
            return
        if use_api:
            try:
                api_client = self.make_api_client()
                api_memory = self.api_settings.public()['include_memory']
            except Exception:
                self.show_api_settings()
                self.status.set('API 配置或密钥不可用，请重新填写。你的输入已保留。')
                return
        if not direct_tool and not use_api and not is_chat_model(self.manager.catalog, self.selected_model):
            self.show_capabilities('models')
            self.status.set('尚未选择模型。请查看推荐并点击“使用”；你的输入已保留。')
            return
        if not direct_tool and not use_api and not self.manager.ready(self.selected_model):
            self.show_capabilities('models')
            self.status.set('先准备文字能力；你的输入已保留。')
            return
        if self.image_path and not Path(self.image_path).is_file():
            self.chat_hint.set('找不到上次选择的图片。请重新选择或移除图片，当前草稿仍保留。')
            return
        if self.image_path and (direct_tool or use_api or not self.manager.catalog[self.selected_model].get('supports_vision', False)):
            self.chat_hint.set('本版 API 仅支持文字；当前技能或模型不支持这张图片。请切换本地看图模型，或在 + 菜单移除图片。输入仍保留。')
            return
        if self.tool_mode.get() and self.image_path:
            self.chat_hint.set('图片问答请先在 + 菜单关闭“工具任务”。图片仍保留。')
            return
        history = list(self.history)
        picture, context = self.image_path, CONTEXTS[self.context.get()]
        tool_mode = self.tool_mode.get()
        personal_options = self.memories.snapshot(self.backend)
        model_id = self.selected_model
        try:
            identity = self.session_id or self.sessions.create()
            self.sessions.append(identity, 'user', message, draft_from=self.draft_session_id)
        except (sqlite3.Error, OSError, ValueError):
            self.status.set('消息未能保存，草稿仍保留。请检查数据目录后重试。')
            return
        self.session_id = identity
        self.clear_submitted_draft()
        self.pending_chat, self.streamed = identity, ''
        self.last_episode, self.last_tool_result = None, None
        self.history.append({'role': 'user', 'content': message})
        self.append_heading('你', 'user')
        self.write(self.transcript, message + '\n\n', True)
        self.append_heading('Free AI', 'assistant')
        self.layout_chat()
        self.refresh_recent()
        self.chat_hint.set('正在准备回答…')
        started = time.monotonic()
        def token(value):
            self.streamed += value
            self.write(self.transcript, value, True)
            self.chat_hint.set(f'正在回答 · {time.monotonic()-started:.1f} 秒')
        def work():
            if direct_tool:
                result = execute_skill(skill_id, message, file_path=skill_file)
                return {**result, 'tool_details': result}
            from qwen_chat import chat
            if tool_mode:
                from agent_lab import run_agent
                result = run_agent(message, api_memory if use_api else True, planner=lambda prompt: chat(prompt, use_memory=False,
                    context=context, model_id=model_id, api_client=api_client, api_use_memory=False,
                    cancel_event=self.generation_cancel), knowledge=self.knowledge)
                answer = result.get('answer', result.get('observation', '工具未返回结果。'))
                if not isinstance(answer, str):
                    answer = json.dumps(answer, ensure_ascii=False, indent=2)
                status = '已核验' if result.get('verified') else '工具执行完成，内容仍需检查' if result.get('tool_execution_verified') else '尚未核验'
                return {**result, 'answer': answer + '\n\n' + status, 'tool_details': result}
            return chat(message, history, memory_store=self.knowledge, return_details=True, context=context,
                        image_path=picture, model_id=model_id, skill_id=skill_id, cancel_event=self.generation_cancel,
                        api_client=api_client, api_use_memory=api_memory,
                        personal_memory=self.personal_memory, read_personal_memory=personal_options['read'],
                        write_personal_memory=personal_options['write'], memory_evidence='对话：' + identity,
                        on_memory_stage=lambda: self.events.put(('update', lambda _: self.chat_hint.set('回答已生成，正在整理可长期记住的信息…'), None)),
                        on_token=lambda value: self.events.put(('update', token, value)))
        def done(result):
            self.last_episode, self.last_answer = result.get('episode_id'), result['answer']
            self.last_tool_result = result.get('tool_details')
            if not self.streamed:
                self.write(self.transcript, self.last_answer, True)
            memory_notice = ''
            if not direct_tool and not tool_mode:
                memory_notice = self.memories.chat_notice(result, message, backend='api' if use_api else 'local')
            if memory_notice:
                self.last_answer += memory_notice
                self.write(self.transcript, memory_notice, True)
            self.write(self.transcript, '\n\n', True)
            self.sessions.append(identity, 'assistant', self.last_answer)
            self.history.append({'role': 'assistant', 'content': self.last_answer})
            self.format_transcript()
            self.pending_chat = None
            import qwen_chat
            notice = (getattr(api_client, 'last_context_notice', '') if use_api else
                      getattr(getattr(qwen_chat, '_runtime', None), 'last_context_notice', '')) if not direct_tool else result.get('verification_note', '')
            self.chat_hint.set((notice + '  ·  ' if notice else '') + f'已保存 · {time.monotonic()-started:.1f} 秒')
            self.refresh_model_state()
        self.submit('正在执行本机技能' if direct_tool else '正在准备回答', work, done, cancellable=not direct_tool)

    def open_project_assistant(self):
        self.project.set_mode(True)

    def open_epistemology(self):
        if not self.last_episode:
            self.status.set('这条回答暂时没有可关联的经验记录。新回答生成后可以反馈。')
            return
        correction = simpledialog.askstring('纠正这次回答', '写下哪里有误，以及正确的做法。\n反馈会保存在本机，训练端可以继续核实。', parent=self.root)
        if correction:
            try:
                self.knowledge.feedback(self.last_episode, outcome='failure', correction=correction, evidence='用户在客户端反馈')
                self.status.set('反馈已记录，可在训练端的经验改进中查看。')
            except Exception as exc:
                self.status.set(str(exc))

    def draw_image(self):
        if not self.manager.ready('image-model'):
            self.select_page(1)
            self.status.set('先在能力中心准备画图功能。')
            return
        prompt = simpledialog.askstring('画一张图', '描述想画的内容（英文更合适）。\n当前使用处理器画图，可随时停止。', parent=self.root)
        if prompt:
            from qwen_chat import runtime
            self.submit('正在画图', lambda: runtime().generate_image(prompt, cancel_event=self.generation_cancel), self.image_finished, cancellable=True)

    def image_finished(self, path):
        self.status.set('图片已保存：' + str(path))
        os.startfile(path)

    def build_capabilities(self, page):
        area = ScrollArea(page, max_width=1100, scrollbar=True)
        area.pack(fill='both', expand=True, padx=24, pady=(8, 16))
        self.cap_area, self.cap_host = area, area.body
        label(self.cap_host, '挑选模型和技能', size=24, bold=True).pack(anchor='w', pady=(8, 8))
        cap_intro = label(self.cap_host, '模型按需下载，也可以联网搜索 Hugging Face。打开应用不会自动加载模型。', color=C['muted'], justify='left', anchor='w')
        cap_intro.pack(fill='x', pady=(0, 16))
        cap_intro.bind('<Configure>', lambda event: cap_intro.configure(wraplength=max(80, event.width)))
        tabs = tk.Frame(self.cap_host, bg=C['bg'])
        tabs.pack(fill='x', pady=(0, 12))
        self.cap_tabs = {}
        for section, title in [('models', '聊天模型'), ('hf-models', '联网找模型'), ('skills', '技能'), ('extensions', '扩展能力')]:
            button = FlatButton(tabs, title, lambda s=section: self.show_capabilities(s), variant='ghost', width=120, height=36)
            button.pack(side='left', padx=(0, 8))
            self.cap_tabs[section] = button
        def layout_tabs(event=None):
            columns = 4 if tabs.winfo_width() >= sum(b.winfo_reqwidth() for b in self.cap_tabs.values()) + 32 else 2
            if getattr(self, '_cap_tab_columns', None) == columns:
                return
            self._cap_tab_columns = columns
            for button in self.cap_tabs.values():
                button.pack_forget()
                button.grid_forget()
            for index, button in enumerate(self.cap_tabs.values()):
                button.grid(row=index // columns, column=index % columns, sticky='ew', padx=(0, 8), pady=(0, 6))
            for index in range(4):
                tabs.columnconfigure(index, weight=1 if index < columns else 0)
        tabs.bind('<Configure>', layout_tabs)
        self.cap_cards = tk.Frame(self.cap_host, bg=C['bg'])
        self.cap_cards.pack(fill='x')
        from hf_model_panel import HuggingFacePanel
        self.hf_model_panel = HuggingFacePanel(self, self.cap_cards)
        self.download_panel = RoundedFrame(self.cap_host, bg=C['side'], padding=16)
        self.download_panel.pack(fill='x', pady=(12, 8))
        label(self.download_panel.body, textvariable=self.download_status, size=11, wraplength=790, justify='left', anchor='w').pack(fill='x')
        self.download_progress = ttk.Progressbar(self.download_panel.body, maximum=100)
        self.download_progress.pack(fill='x', pady=(12, 8))
        self.pause_button = FlatButton(self.download_panel.body, '暂停下载', self.download_cancel.set, variant='ghost', width=105, height=32, state='disabled', font_size=10)
        self.pause_button.pack(anchor='e')
        self.space = tk.StringVar()
        label(self.cap_host, textvariable=self.space, size=10, color=C['muted'], wraplength=800, anchor='w', justify='left').pack(anchor='w', pady=(10, 16))

    def show_capabilities(self, section='models'):
        if section not in ('models', 'hf-models', 'skills', 'extensions'):
            raise ValueError('未知能力页面')
        self.cap_section = section
        self.refresh_capabilities()
        self.select_page(1)
        self.root.update_idletasks()
        self.cap_area.canvas.yview_moveto(0)

    def capability_card(self, title, description, detail=''):
        card = RoundedFrame(self.cap_cards, padding=(18, 14), radius=16)
        card.pack(fill='x', pady=5)
        right = tk.Frame(card.body, bg=C['surface'])
        right.pack(side='right', padx=(14, 0))
        words = tk.Frame(card.body, bg=C['surface'])
        words.pack(side='left', fill='x', expand=True)
        texts = [label(words, title, size=13, bold=True, anchor='w', justify='left'),
                 label(words, description, size=11, color=C['muted'], anchor='w', justify='left')]
        if detail:
            texts.append(label(words, detail, size=10, color=C['muted'], anchor='w', justify='left'))
        for index, text in enumerate(texts):
            text.pack(fill='x', pady=(5 if index else 0, 0))
        words.bind('<Configure>', lambda event: [text.configure(wraplength=max(80, event.width)) for text in texts])
        return right

    def refresh_capabilities(self):
        for widget in self.cap_cards.winfo_children():
            if widget is self.hf_model_panel:
                widget.pack_forget()
            else:
                widget.destroy()
        self.package_rows = {row['id']: row for row in self.manager.list_packages()}
        for section, button in self.cap_tabs.items():
            button.configure(variant='selected' if section == self.cap_section else 'ghost')
        if self.cap_section == 'hf-models':
            self.hf_model_panel.pack(fill='x')
            self.hf_model_panel.sync_busy()
        elif self.cap_section == 'skills':
            label(self.cap_cards, '内置技能无需额外下载。AI 工作方式可使用本机模型或 API，本机小工具可以直接运行。',
                  size=11, color=C['muted'], wraplength=800, justify='left').pack(anchor='w', pady=(0, 12))
            for spec in skill_catalog():
                right = self.capability_card(spec['title'], spec['description'],
                    ('本机小工具 · 无需模型' if spec['kind'] == 'tool' else 'AI 工作方式 · 需要模型') + '\n' + spec['input_hint'])
                chosen = self.skill.get() == spec['id']
                FlatButton(right, '使用中' if chosen else '使用', lambda i=spec['id']: self.select_skill(i),
                           variant='selected' if chosen else 'secondary', width=90, height=34).pack()
        else:
            if self.cap_section == 'models':
                identities = [spec['id'] for spec in chat_models(self.manager.catalog)]
                FlatButton(self.cap_cards, '不下载模型 · 配置 API 连接', self.show_api_settings,
                           variant='accent', width=270, height=36).pack(anchor='w', pady=(0, 10))
                label(self.cap_cards, '下载大小包含尚未安装的基础组件。内存建议按日常短对话估算，长对话会占用更多。',
                      size=11, color=C['muted'], wraplength=800, justify='left').pack(anchor='w', pady=(0, 12))
            else:
                identities = ['engine-cuda', 'vision', 'speech-model', 'image-model']
            for identity in identities:
                row = self.package_rows[identity]
                is_model = is_chat_model(self.manager.catalog, identity)
                title = model_name(self.manager.catalog, identity) if is_model else row['title']
                if identity == 'text-default':
                    title += ' · 日常推荐'
                detail = ''
                if is_model:
                    detail = '适合：' + '、'.join(row.get('use_cases', [])) if row.get('use_cases') else '联网添加的模型，实际表现与运行兼容性需要验证。'
                    if row.get('recommended_ram_gb'):
                        detail += f"\n内存建议 {row['recommended_ram_gb']} GB · 显卡加速建议 {row.get('recommended_vram_gb', '—')} GB 显存"
                if identity == 'vision':
                    detail = '当前看图扩展搭配 Qwen3.5 4B 使用；下载后需要选用它。'
                right = self.capability_card(title, row['description'], detail)
                chosen = self.backend == 'local' and self.selected_model == identity
                state = ('已选用' if chosen else '已下载') if row['installed'] else '可继续下载' if row['status'] == 'paused' else size_text(row['download_size'])
                label(right, state, size=10, color=C['success'] if row['installed'] else C['muted']).pack(pady=(0, 7))
                actions = tk.Frame(right, bg=C['surface'])
                actions.pack()
                if row['installed']:
                    if is_model:
                        FlatButton(actions, '停用' if chosen else '使用', self.disable_model if chosen else lambda i=identity: self.enable_model(i),
                                   variant='secondary', width=64, height=34, font_size=11).pack(side='left')
                    FlatButton(actions, '管理', lambda i=identity: self.capability_menu(i), variant='ghost', width=65, height=34, font_size=11).pack(side='left')
                else:
                    FlatButton(actions, '继续' if row['status'] == 'paused' else '下载', lambda i=identity: self.start_download(i),
                               variant='secondary', width=72, height=34, font_size=11, state='disabled' if self.downloading else 'normal').pack(side='left')
                    FlatButton(actions, '详情', lambda i=identity: self.package_details(i), variant='ghost', width=60, height=34, font_size=11).pack(side='left')
        self.cap_area.bind_wheel(self.cap_host)
        disk_path = self.manager.root
        while not disk_path.exists() and disk_path.parent != disk_path:
            disk_path = disk_path.parent
        try:
            available = '可用 ' + size_text(shutil.disk_usage(disk_path).free)
        except OSError:
            available = '暂时无法读取剩余空间'
        self.space.set('模型和扩展：' + str(self.manager.root) + '  ·  ' + available +
                       '\n聊天和训练记录：' + str(ARTIFACTS))
        if self.backend == 'api' or (self.selected_model and self.manager.ready(self.selected_model)):
            self.setup_button.pack_forget()
        else:
            self.setup_button.pack(pady=(12, 0))
        self.refresh_model_state()
        self.layout_chat()

    def package_details(self, identity):
        row = self.package_rows[identity]
        self.show_details('来源与说明', f"{row['title']}\n\n{row['description']}\n\n来源：{row['source']}\n"
            f"许可：{row['license']}\n固定版本：{row['version']}\n" +
            (f"量化文件：{row.get('quantized_source', row['source'])}\n" if is_chat_model(self.manager.catalog, identity) else '') +
            '\n下载会校验完整性。模型目录中的配置建议不代表所有模型都已在本机实测。')

    def capability_menu(self, identity):
        row = self.package_rows[identity]
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label='检查完整性', command=lambda: self.submit('正在检查文件', lambda: self.manager.verify(identity), lambda value: self.status.set('完整性检查：' + str(value))))
        menu.add_command(label='来源与许可', command=lambda: self.package_details(identity))
        menu.add_separator()
        menu.add_command(label='移除这项能力', command=lambda: self.remove_capability(identity))
        menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def remove_capability(self, identity):
        if self.busy or self.downloading:
            self.status.set('当前任务结束后再移除能力。')
            return
        if not messagebox.askyesno('移除能力', '移除已下载的能力文件？聊天和经验都会保留。', parent=self.root):
            return
        try:
            from qwen_chat import shutdown
            shutdown()
            from compute_guard import busy_reason
            occupied = busy_reason()
            if occupied:
                self.status.set(occupied + ' 当前能力文件保持不变。')
                return
            self.manager.uninstall(identity)
            if identity == self.selected_model:
                self.selected_model = None
                self.sessions.set('client_model', None)
            self.refresh_capabilities()
        except Exception as exc:
            self.status.set(str(exc))

    def start_download(self, identity):
        self.select_page(1)
        if self.downloading:
            self.status.set('已有下载在进行，暂停或完成后可准备其他能力。')
            return
        self.downloading = identity
        self.download_cancel.clear()
        self.pause_button.configure(state='normal')
        self.download_status.set('正在准备下载…')
        self.refresh_capabilities()
        def work():
            try:
                self.manager.install(identity, progress=lambda item: self.events.put(('update', self.download_event, item)), cancel_event=self.download_cancel)
                message = '下载已完成。模型需点击“使用”才会启用；其他能力按你选择的任务运行。'
            except Exception as exc:
                message = '下载已暂停，文件已保留。' if self.download_cancel.is_set() else '下载未完成：' + str(exc)
            self.events.put(('update', self.download_finished, message))
        threading.Thread(target=work, daemon=True).start()

    def download_finished(self, message):
        self.downloading = None
        self.download_status.set(message)
        self.pause_button.configure(state='disabled')
        self.refresh_capabilities()
        self.status.set(message)

    def build_settings(self, page):
        from client_settings import SettingsPanel
        self.settings_panel = SettingsPanel(self, page)

    def open_project_in_editor(self, folder=None):
        from coding_tools import launch_editor, load_preferences
        try:
            opened = launch_editor(folder or self.project.workspace.get(), load_preferences(self.sessions))
            self.status.set('已用 ' + opened['tool'] + ' 打开项目。')
        except (OSError, ValueError):
            self.status.set('无法打开编辑器。请先选择项目，并在“设置 → 编程工具”检查程序位置。')

    def open_project_terminal(self, folder=None):
        from coding_tools import launch_terminal, load_preferences
        try:
            opened = launch_terminal(folder or self.project.workspace.get(), load_preferences(self.sessions))
            self.status.set('已在项目目录打开 ' + opened['tool'] + '。')
        except (OSError, ValueError):
            self.status.set('无法打开终端。请先选择项目，并检查所选终端程序是否存在。')

    def save_skin(self, event=None):
        choice = CLIENT_SKINS.get(self.skin_choice.get())
        if choice:
            self.sessions.set('client_theme', choice)
            self.skin_notice.set('已保存「' + self.skin_choice.get() + '」。重新打开 Free AI 后生效。')

    def open_model_folder(self):
        try:
            self.manager.root.mkdir(parents=True, exist_ok=True)
            os.startfile(self.manager.root)
        except OSError:
            self.status.set('暂时无法打开模型文件夹，请检查磁盘是否连接以及文件夹权限。')

    def open_training(self):
        if self.busy:
            self.status.set('请先停止当前任务。')
            return
        self.release_model()
        exe = ROOT / 'Free AI Trainer.exe'
        command = [str(exe)] if exe.exists() else [str(Path(sys.executable).with_name('pythonw.exe')), '-E', '-s', '-B', '-X', 'utf8', str(ROOT / 'free_ai_bootstrap.py'), '--trainer']
        self.training_process = subprocess.Popen(command, cwd=ROOT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.status.set('已打开独立训练端。')

    def submit(self, title, work, done, cancellable=False):
        if self.busy:
            return
        self.busy, self.cancellable = True, cancellable
        self._sync_composer_activity()
        self.project.sync_busy()
        if hasattr(self, 'settings_panel'):
            self.settings_panel.sync_busy()
        if hasattr(self, 'hf_model_panel'):
            self.hf_model_panel.sync_busy()
        self.memories.sync_busy()
        self.generation_cancel.clear()
        self.status.set(title + '…')
        self.send_button.configure(text='■' if cancellable else '…', state='normal' if cancellable else 'disabled')
        if cancellable:
            self.stop_task_button.pack(side='right')
        for button in self.buttons:
            button.configure(state='disabled')
        def worker():
            try:
                self.events.put(('done', done, work()))
            except Exception as exc:
                logging.exception('Client task failed: %s', title)
                self.events.put(('error', None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, handler, payload = self.events.get_nowait()
                if kind == 'update':
                    handler(payload)
                    continue
                self.busy = False
                self._sync_composer_activity()
                self.project.sync_busy()
                if hasattr(self, 'settings_panel'):
                    self.settings_panel.sync_busy()
                if hasattr(self, 'hf_model_panel'):
                    self.hf_model_panel.sync_busy()
                self.memories.sync_busy()
                self.send_button.configure(text='↑', state='normal')
                self.stop_task_button.pack_forget()
                for button in self.buttons:
                    button.configure(state='normal')
                if kind == 'error':
                    reason = '已停止，已有内容已保存。' if self.generation_cancel.is_set() else str(payload)
                    self.status.set(reason)
                    self.chat_hint.set(reason)
                    if self.pending_chat:
                        self.save_interrupted_answer(reason)
                        self.write(self.transcript, '\n[' + reason + ']\n\n', True)
                    self.refresh_model_state()
                else:
                    self.status.set('已完成')
                    handler(payload)
                self.update_placeholder()
        except queue.Empty:
            pass
        except Exception:
            logging.exception('Client task update failed')
            self.status.set('处理任务反馈时发生错误，请查看运行记录。')
        finally:
            self.poll_id = self.root.after(60, self.poll)

    def close(self):
        if not self.flush_draft():
            return
        if self.busy:
            self.generation_cancel.set()
            self.status.set('正在停止，请稍候再关闭。')
            return
        self._closing = True
        self.download_cancel.set()
        from qwen_chat import shutdown
        shutdown()
        self.root.after_cancel(self.poll_id)
        if self._composer_size_id is not None:
            self.root.after_cancel(self._composer_size_id)
        self.root.destroy()


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ARTIFACTS / 'desktop.log', encoding='utf-8', level=logging.INFO)
    root = tk.Tk()
    ClientApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
