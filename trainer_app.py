"""A focused training workspace. Model libraries run only in the training interpreter."""
import json
import logging
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from uuid import uuid4

from paths import ARTIFACTS, DATA_ROOT, ROOT, RUNS_ROOT
from app_state import Sessions
from capability_manager import PackageManager
from model_catalog import is_chat_model
from epistemology import KnowledgeStore
from epistemology_ui import EpistemologyPanel
import training_desktop as bridge
import training_extension as extension
from ui_kit import (
    COLORS, FlatButton, RoundedFrame, ScrollArea, background, label,
    set_text_size, setup_theme, text_scale, ui_font,
)
from ui_preferences import TEXT_SIZES, load_text_size, save_text_size


DATA_NAMES = {'sft_train.jsonl': '练习题', 'sft_validation.jsonl': '挑选成果用的新题',
              'sft_test.jsonl': '最后检查用的新题', 'sft_general_test.jsonl': '基础能力检查题'}
TRAINER_CONTENT_WIDTH = 1120
TRAINER_THEMES = {'跟随客户端': 'client', '白蓝': 'light', '经典深色': 'dark', '边缘行者 · 霓虹': 'cyberpunk'}


class ControlRow(tk.Frame):
    """Keep related controls together and wrap them when the available width changes."""
    def __init__(self, parent):
        super().__init__(parent, bg=background(parent))
        self.bind('<Configure>', self.reflow)

    def reflow(self, event=None):
        x = y = line_height = 0
        width = self.winfo_width()
        for widget in self.winfo_children():
            requested = min(widget.winfo_reqwidth(), width)
            if x and x + requested > width:
                x, y, line_height = 0, y + line_height + 5, 0
            widget.place(x=x, y=y, width=requested)
            x += requested + 8
            line_height = max(line_height, widget.winfo_reqheight())
        height = y + line_height + 5
        if int(self.cget('height')) != height:
            self.configure(height=height)


def training_runs(directory=RUNS_ROOT):
    """List completed and interrupted experiments without mistaking either for the other."""
    directory = Path(directory)
    rows = []
    for path in [*directory.glob('qwen-lora-*'), *directory.glob('qwen-agent-lora-*')]:
        if not path.is_dir():
            continue
        summary = {}
        try:
            summary = json.loads((path / 'summary.json').read_text(encoding='utf-8'))
            if not isinstance(summary, dict):
                summary = {}
        except (OSError, ValueError):
            pass
        rows.append({'path': path, 'name': path.name, 'summary': summary,
                     'completed': summary.get('completed') is True, 'modified': path.stat().st_mtime_ns})
    return sorted(rows, key=lambda row: row['modified'], reverse=True)


def load_result(directory):
    directory = Path(directory)
    summary = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
    if not isinstance(summary, dict) or summary.get('completed') is not True:
        raise ValueError('这次训练尚未完成全部检查，不能作为已完成的成果。')
    path = directory / 'comparison.json'
    comparisons = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else []
    if not isinstance(comparisons, list) or any(not isinstance(row, dict) for row in comparisons):
        raise ValueError('训练前后对比记录格式不正确。原始文件仍然保留。')
    return {'path': directory, 'summary': summary, 'comparisons': comparisons}


def result_text(result):
    summary = result['summary']
    def number(key, digits=4):
        value = summary.get(key)
        return f'{value:.{digits}f}' if isinstance(value, (int, float)) else '未记录'
    return (f"检查用的新题：训练前 {number('baseline_validation_loss')} → 训练后 {number('best_validation_loss')}\n"
            '这是回答偏差的数值，越低越好；它不等于正确率。\n'
            f"保留第 {summary.get('best_epoch', '未记录')} 轮成果 · 调整 {summary.get('updates', '未记录')} 次 · "
            f"训练用时 {number('training_seconds', 1)} 秒\n"
            f"另有 {len(result['comparisons'])} 道未参与训练的题目，可逐题查看前后回答。")


class TrainingKnowledgePanel(EpistemologyPanel):
    def __init__(self, app, page):
        super().__init__(app, page, responsive=True)
        self.summary_scope_notice = (
            '让 AI 总结时，使用客户端当前选择的模型或 API。若选择 API，将把所选这一条记录的题目、'
            '结果与反馈发送到配置服务；其他记录和聊天不会发送。')
        app.wrap_label(page, self.summary_scope_notice, size=11, color=app.colors['muted']).pack(
            fill='x', pady=(0, 6), before=self.notebook)

    def _summary_notice(self, message):
        self.notice.set(message)
        self.app.status.set(message)

    def _propose_model(self):
        if self.app.busy:
            self.app.status.set('当前任务还在进行，请先等待结束或停止。')
            return
        identity = self.selected_episode
        if self.app.settings.get('client_backend') == 'api':
            self._propose_api(identity)
            return
        model_id = self.app.settings.get('client_model')
        if not is_chat_model(self.app.manager.catalog, model_id) or not self.app.manager.ready(model_id):
            self._summary_notice('AI 总结需要客户端的文字模型。请先在 Free AI 客户端下载并选择模型；手工纠错仍可使用。')
            return
        def work():
            self._require(identity, '使用记录')
            from epistemology import propose_with_model
            from qwen_chat import chat, shutdown
            try:
                return propose_with_model(self.store, [identity],
                    lambda prompt: chat(prompt, use_memory=False, model_id=model_id, cancel_event=self.app.cancel_event))
            finally:
                shutdown()
        self.app.submit('正在整理待确认建议', work, lambda _: self._finish(
            '建议已保存，核对适用条件与检查方法后才能启用。', identity), cancellable=True)

    def _propose_api(self, identity):
        from api_settings import APISettings, APISettingsError
        from remote_api import APIClient, APIError, APICancelled
        from urllib.parse import urlsplit

        # Check the explicit selection before touching credentials or a service.
        try:
            self._require(identity, '要总结的使用记录')
            self.store.episode(identity)
        except ValueError as exc:
            self._summary_notice(str(exc))
            return
        settings = APISettings(ARTIFACTS)
        key_help = ('训练端不能使用客户端仅本次保留的密钥。请回 Free AI 客户端填写密钥，'
                    '勾选“记住密钥”并保存连接，再回来总结；手工纠错仍可使用。')
        try:
            # One atomic snapshot keeps a credential bound to its destination
            # even when the client edits its connection at the same time.
            profile, key = settings.connection()
            if not profile['remember_key'] or not key:
                self._summary_notice(key_help)
                return
            client = APIClient(profile['base_url'], profile['model'], key,
                max_output_tokens=profile['max_output_tokens'],
                thinking=profile['thinking'] if profile['provider'] == 'deepseek' else None)
        except APISettingsError:
            self._summary_notice('训练端无法读取已保存的 API 密钥。请回客户端重新填写并保存；手工纠错仍可使用。')
            return
        except (APIError, ValueError):
            self._summary_notice('API 配置暂不可用，请回客户端检查地址、模型和密钥并保存后重试。')
            return
        host = urlsplit(profile['base_url']).netloc
        notice = '将仅把所选这一条记录发送到 ' + host + ' 总结；服务商可能计费，建议仍需你确认。'
        self.notice.set(notice)

        def generate(prompt):
            # No conversation history, retrieval, or local runtime participates.
            answer = client.generate(prompt, history=None, cancel_event=self.app.cancel_event)
            if self.app.cancel_event.is_set():
                raise APICancelled('已停止总结，这次回答没有保存为建议。')
            return answer

        def work():
            from epistemology import propose_with_model
            return propose_with_model(self.store, [identity], generate)

        self.app.submit(notice, work, lambda _: self._finish(
            'API 建议已保存在本机，核对适用条件与检查方法后才能启用。', identity), cancellable=True)


class TrainerApp:
    def __init__(self, root):
        self.colors = COLORS
        self.root = root
        self.settings = Sessions(ARTIFACTS)
        theme_choice = self.settings.get('trainer_theme', 'client')
        if theme_choice not in TRAINER_THEMES.values():
            theme_choice = 'client'
        self.theme = self.settings.get('client_theme', 'light') if theme_choice == 'client' else theme_choice
        if self.theme not in ('cyberpunk', 'dark', 'light'):
            self.theme = 'light'
        preference = load_text_size()
        setup_theme(root, 'Free AI · 训练工作台', size=(1320, 880), min_size=(760, 560), text_size=preference, theme=self.theme)
        self.theme_choice = tk.StringVar(root, value=next(name for name, value in TRAINER_THEMES.items() if value == theme_choice))
        self.theme_notice = tk.StringVar(root)
        self.text_size = tk.StringVar(root, value=next(name for name, value in TEXT_SIZES.items() if value == preference))
        self.control_rows = []
        self.busy = False
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.stop_path = None
        self.current_job = None
        self.buttons = []
        self.stop_buttons = []
        self.pages = []
        self.nav = []
        self.manager = PackageManager()
        self.knowledge = KnowledgeStore()
        selected_model = self.settings.get('trainer_model', '')
        self.trainer_model = tk.StringVar(value=selected_model if selected_model == 'legacy-training-model' else '')
        self.status = tk.StringVar(value='先选择训练模型，再检查练习题。')
        saved_dataset = self.settings.get('trainer_dataset')
        try:
            dataset_path = saved_dataset if isinstance(saved_dataset, str) and Path(saved_dataset).is_dir() else str(DATA_ROOT)
        except (OSError, ValueError):
            dataset_path = str(DATA_ROOT)
        self.dataset = tk.StringVar(value=dataset_path)
        self.adapter = tk.StringVar()
        self.epochs = tk.StringVar(value='1')
        self.learning_rate = tk.StringVar(value='0.0001')
        self.agent_target = tk.BooleanVar(value=False)
        self.phase = tk.StringVar(value='等待开始')
        self.data_notice = tk.StringVar(value='练习题和检查题分开保存，避免只会背答案。')
        self.preparation_notice = tk.StringVar()
        self.result_notice = tk.StringVar(value='完成后会自动比较训练前后的回答，并保留原始模型。')
        self.result = None
        self.run_rows = {}
        self.finetune_details = None
        self.training_details = None
        self.last_episode = None
        self._build()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = self.callback_error
        self.refresh_setup()
        self.refresh_runs()
        self.select_page(0)
        self.poll_id = root.after(80, self.poll)

    def frame(self, parent, color=None, **kwargs):
        return tk.Frame(parent, bg=color or self.colors['bg'], **kwargs)

    def _build(self):
        c = self.colors
        body = self.frame(self.root)
        body.pack(fill='both', expand=True)
        side = self.frame(body, c['side'], width=174, padx=12, pady=28)
        self.sidebar = side
        side.pack(side='left', fill='y')
        side.pack_propagate(False)
        self._resize_sidebar()
        self.brand_label = label(side, 'Free AI', size=25, bold=True, color=c.get('brand', c['text']))
        self.brand_label.pack(anchor='w', padx=8)
        label(side, '训练工作台', size=12, color=c['muted']).pack(anchor='w', padx=8, pady=(3, 30))
        for index, title in enumerate(('训练', '训练记录', '经验改进')):
            button = FlatButton(side, text=title, command=lambda i=index: self.select_page(i),
                                variant='ghost', width=138, height=44, anchor='w')
            button.pack(fill='x', pady=4)
            self.nav.append(button)
        self.wrap_label(side, '聊天请使用 Free AI 客户端', size=11, color=c['muted']).pack(
            side='bottom', fill='x', padx=8, pady=8)
        viewport = self.frame(body)
        viewport.pack(side='left', fill='both', expand=True)
        self.main = self.frame(viewport)
        self.main.place(relx=.5, y=16, relheight=1, height=-32, anchor='n')
        viewport.bind('<Configure>', lambda event: self.main.place_configure(
            width=max(1, min(TRAINER_CONTENT_WIDTH, event.width - 32))))
        tools = self.button_row(self.main)
        tools.pack(side='top', fill='x', pady=(0, 10))
        theme_group = self.frame(tools)
        label(theme_group, '主题', size=11, color=c['muted']).pack(side='left', padx=(0, 8))
        self.theme_picker = ttk.Combobox(theme_group, textvariable=self.theme_choice, state='readonly',
                                        values=tuple(TRAINER_THEMES), width=20, font=ui_font(self.root))
        self.theme_picker.pack(side='left')
        self.theme_picker.bind('<<ComboboxSelected>>', self.change_theme)
        size_group = self.frame(tools)
        label(size_group, '文字大小', size=11, color=c['muted']).pack(side='left', padx=(8, 8))
        self.text_size_picker = ttk.Combobox(size_group, textvariable=self.text_size, state='readonly',
                                            values=tuple(TEXT_SIZES), width=9, font=ui_font(self.root))
        self.text_size_picker.pack(side='left')
        self.text_size_picker.current(tuple(TEXT_SIZES).index(self.text_size.get()))
        self.text_size_picker.bind('<<ComboboxSelected>>', self.change_text_size)
        self.theme_notice_label = self.wrap_label(self.main, textvariable=self.theme_notice, size=10, color=c['muted'])
        footer = self.frame(self.main)
        footer.pack(side='bottom', fill='x', pady=(10, 0))
        self.stop_button = FlatButton(footer, text='停止当前任务', command=self.stop_training,
                                      variant='secondary', width=145, height=36, state='disabled')
        self.stop_button.pack(side='right', padx=(10, 0))
        self.wrap_label(footer, textvariable=self.status, size=11, color=c['muted']).pack(
            side='left', fill='x', expand=True)
        self.stop_buttons.append(self.stop_button)
        self.page_holder = self.frame(self.main)
        self.page_holder.pack(fill='both', expand=True)
        self.page_holder.pack_propagate(False)
        self.pages = [ScrollArea(self.page_holder, scrollbar=True) for _ in range(3)]
        self.train_scroll, self.records_scroll, self.knowledge_scroll = self.pages
        self._training_page(self.train_scroll.body)
        self._records_page(self.records_scroll.body)
        self._knowledge_page(self.knowledge_scroll.body)
        for page in self.pages:
            page.bind_wheel(page.body)
        self.root.bind('<<FreeAITextSizeChanged>>', lambda event: self.root.after_idle(self.reflow), add='+')

    def change_text_size(self, event=None):
        choice = TEXT_SIZES[self.text_size.get()]
        set_text_size(self.root, choice)
        try:
            save_text_size(choice)
        except OSError:
            self.status.set('本次已应用字号，未能保存设置。')
            logging.exception('Unable to save desktop text size')

    def change_theme(self, event=None):
        choice = TRAINER_THEMES.get(self.theme_choice.get())
        if choice is None:
            return
        try:
            self.settings.set('trainer_theme', choice)
        except Exception:
            logging.exception('Unable to save trainer theme')
            self.status.set('主题偏好未能保存。')
            return
        self.theme_notice.set('已保存主题，下次打开训练端时生效。')
        self.theme_notice_label.pack(fill='x', pady=(0, 8), before=self.page_holder)

    def reflow(self):
        self._resize_sidebar()
        for row in self.control_rows:
            row.reflow()
        self._arrange_comparisons()
        self.knowledge_panel.arrange_practice()

    def _resize_sidebar(self):
        title_width = max(ui_font(self.sidebar, 25, bold=True).measure('Free AI'),
                          ui_font(self.sidebar, 12).measure('训练工作台'))
        self.sidebar.configure(width=max(174, title_width + 48))

    def button_row(self, parent):
        row = ControlRow(parent)
        self.control_rows.append(row)
        return row

    @staticmethod
    def wrap_label(parent, text='', **kwargs):
        widget = label(parent, text, wraplength=480, anchor='w', justify='left', **kwargs)
        widget.bind('<Configure>', lambda event: widget.configure(wraplength=max(1, event.width - 4)))
        return widget

    def heading(self, page, title, subtitle):
        self.wrap_label(page, title, size=25, bold=True).pack(fill='x')
        self.wrap_label(page, subtitle, size=12, color=self.colors['muted']).pack(fill='x', pady=(6, 16))

    def card(self, parent, title):
        container = RoundedFrame(parent, bg=self.colors['surface'], radius=16,
                                      padding=(20, 14), border=self.colors['line'])
        container.pack(fill='x', pady=(0, 12))
        box = container.body
        box.container = container
        self.wrap_label(box, title, size=15, bold=True).pack(fill='x', pady=(0, 8))
        return box

    def button(self, parent, text, command, variant='secondary', width=None):
        button = FlatButton(parent, text=text, command=command, variant=variant, width=width, height=38)
        if isinstance(parent, ControlRow):
            parent.reflow()
        else:
            button.pack(side='left', padx=(0, 8))
        self.buttons.append(button)
        return button

    def text(self, parent, height=5, editable=False):
        frame = self.frame(parent, background(parent))
        frame.pack(fill='both', expand=True, pady=4)
        widget = tk.Text(frame, width=28, height=height, wrap='word', font=ui_font(parent),
                         bg=self.colors['surface'], fg=self.colors['text'], insertbackground=self.colors['text'],
                         relief='flat', highlightthickness=1, highlightbackground=self.colors['line'],
                         padx=12, pady=10, undo=editable)
        bar = ttk.Scrollbar(frame, orient='vertical', command=widget.yview)
        bar.pack(side='right', fill='y')
        widget.configure(yscrollcommand=bar.set)
        widget.pack(side='left', fill='both', expand=True)
        if not editable:
            widget.configure(state='disabled')
        return widget

    def tree(self, parent, *, pady=0, **options):
        frame = self.frame(parent, background(parent))
        frame.pack(fill='x', pady=pady)
        frame.columnconfigure(0, weight=1)
        widget = ttk.Treeview(frame, **options)
        vertical = ttk.Scrollbar(frame, orient='vertical', command=widget.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=widget.xview)
        widget.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        widget.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        return widget

    @staticmethod
    def write(widget, value, append=False):
        widget.configure(state='normal')
        if not append:
            widget.delete('1.0', 'end')
        widget.insert('end', str(value))
        widget.see('end')
        widget.configure(state='disabled')

    def _training_page(self, page):
        self.heading(page, '让 AI 学会你的做法', '从一小批可靠的题目开始，每次训练都用没教过的新题检查。')
        self.setup_card = self.card(page, '选择训练模型')
        self.wrap_label(self.setup_card, textvariable=self.preparation_notice, size=11,
                        color=self.colors['muted']).pack(fill='x')
        self.wrap_label(self.setup_card, '模型保存位置：' + str(self.manager.root), size=11,
                        color=self.colors['muted']).pack(fill='x', pady=(5, 0))
        row = self.button_row(self.setup_card)
        row.pack(fill='x', pady=(8, 0))
        self.tools_button = self.button(row, '准备训练工具', self.prepare_tools, width=180)
        self.model_button = self.button(row, '下载入门训练模型', self.prepare_model, width=210)
        self.use_model_button = self.button(row, '使用这个模型', self.use_training_model,
                                            variant='primary', width=180)
        first = self.card(page, '1   准备练习题')
        self.data_card = first
        ttk.Entry(first, textvariable=self.dataset, font=ui_font(first)).pack(fill='x', pady=(0, 8))
        row = self.button_row(first)
        row.pack(fill='x')
        self.button(row, '选择文件夹', self.choose_data)
        self.button(row, '检查数据', self.check_data)
        self.button(row, '题目格式', self.data_help, variant='ghost', width=100)
        self.online_data_button = self.button(row, '联网找训练数据', self.toggle_online_data, variant='accent', width=180)
        self.wrap_label(first, textvariable=self.data_notice, size=11, color=self.colors['muted']).pack(fill='x')
        from hf_dataset_panel import HuggingFaceDatasetPanel
        self.dataset_panel = HuggingFaceDatasetPanel(self, first, self.imported_dataset)
        second = self.card(page, '2   开始训练')
        self.wrap_label(second, '选择模型后才能开始训练。学习成果单独保存，不会替换日常聊天模型。',
                        size=11, color=self.colors['muted']).pack(fill='x')
        row = self.button_row(second)
        row.pack(fill='x', pady=(10, 0))
        self.start_button = self.button(row, '开始训练', self.finetune, variant='primary', width=140)
        stop_button = FlatButton(row, text='停止', command=self.stop_training,
                                 variant='secondary', width=90, height=38, state='disabled')
        self.stop_buttons.append(stop_button)
        self.button(row, '更多设置', self.toggle_parameters, variant='ghost', width=115)
        self.wrap_label(second, textvariable=self.phase, size=12, color=self.colors['muted']).pack(fill='x')
        self.parameters = self.frame(second, self.colors['surface'])
        row = self.button_row(self.parameters)
        row.pack(fill='x', pady=(12, 4))
        epochs = self.frame(row, self.colors['surface'])
        label(epochs, '完整学习次数').pack(side='left', padx=(0, 8))
        ttk.Spinbox(epochs, from_=1, to=10, textvariable=self.epochs, width=6).pack(side='left')
        rate = self.frame(row, self.colors['surface'])
        label(rate, '每次调整幅度').pack(side='left', padx=(0, 8))
        ttk.Entry(rate, textvariable=self.learning_rate, width=10).pack(side='left')
        ttk.Checkbutton(row, text='保存为工具使用练习成果', variable=self.agent_target)
        self.wrap_label(self.parameters, '接着以前的成果学习（可留空）', size=11).pack(fill='x')
        row = self.frame(self.parameters, self.colors['surface'])
        row.pack(fill='x', pady=4)
        button = self.button(row, '选择成果', self.choose_adapter)
        button.pack_configure(side='right', padx=(8, 0))
        ttk.Entry(row, textvariable=self.adapter).pack(side='left', fill='x', expand=True)
        third = self.card(page, '3   查看真实变化')
        self.wrap_label(third, textvariable=self.result_notice, size=12, color=self.colors['muted']).pack(fill='x')
        row = self.button_row(third)
        row.pack(fill='x', pady=(8, 0))
        self.button(row, '查看最近结果', self.latest_result, variant='ghost', width=145)
        self.button(row, '打开成果文件夹', self.open_outputs, variant='ghost', width=155)
        label(page, '运行日志', size=14, bold=True).pack(anchor='w', pady=(3, 0))
        self.log = self.text(page, height=4)
        self.write(self.log, '检查数据后即可训练。停止时会等待当前步骤结束；未完成的实验不会被标成成功。')

    def toggle_online_data(self):
        if self.dataset_panel.winfo_manager():
            self.dataset_panel.pack_forget()
            self.online_data_button.configure(text='联网找训练数据')
        else:
            self.dataset_panel.pack(fill='x', pady=(12, 4))
            self.online_data_button.configure(text='收起在线数据')
            self.dataset_panel.sync_busy()
            self.train_scroll.bind_wheel(self.dataset_panel)

    def imported_dataset(self, result):
        directory = Path(result['path'])
        counts = bridge.inspect_data(directory)
        self.dataset.set(str(directory))
        self.settings.set('trainer_dataset', str(directory))
        self.data_notice.set('已导入并检查：' + ' · '.join(f'{DATA_NAMES[name]} {count} 条' for name, count in counts.items()))
        self.status.set('训练数据已准备好。核对内容后，再点击“开始训练”。')
        self.write(self.log, '\n在线数据保存位置：' + str(directory) + '\n来源与导入记录：' + str(result['report']) + '\n', True)

    def _records_page(self, page):
        self.heading(page, '每一次训练，都留有依据', '看训练前后回答的变化，也检查有没有退步。数字变小并不代表每个回答都正确。')
        toolbar = self.button_row(page)
        toolbar.pack(fill='x', pady=(0, 10))
        self.button(toolbar, '刷新记录', self.refresh_runs)
        self.button(toolbar, '打开所选文件夹', self.open_selected_run)
        self.button(toolbar, '查看详细数值', lambda: self.show_details('训练详细记录', self.finetune_details))
        self.run_tree = self.tree(page, pady=(0, 12), columns=('status', 'name'),
                                  show='headings', height=4, selectmode='browse')
        self.run_tree.heading('status', text='结果')
        self.run_tree.column('status', width=110, stretch=False)
        self.run_tree.heading('name', text='训练记录')
        self.run_tree.column('name', width=700, minwidth=400)
        self.run_tree.bind('<<TreeviewSelect>>', self.select_run)
        self.record_summary = tk.StringVar(value='完成一次训练后，这里会显示实际检查结果。')
        self.wrap_label(page, textvariable=self.record_summary, size=12).pack(fill='x', pady=(0, 12))
        self.question = tk.StringVar()
        self.question_picker = ttk.Combobox(page, textvariable=self.question, state='readonly')
        self.question_picker.pack(fill='x', ipady=6, pady=(0, 10))
        self.question_picker.bind('<<ComboboxSelected>>', self.select_question)
        self.comparisons = self.frame(page)
        self.comparisons.pack(fill='x')
        self.before_panel, self.after_panel = self.frame(self.comparisons), self.frame(self.comparisons)
        label(self.before_panel, '训练前', size=14, bold=True).pack(anchor='w')
        label(self.after_panel, '训练后', size=14, bold=True).pack(anchor='w')
        self.before_answer = self.text(self.before_panel, height=6)
        self.after_answer = self.text(self.after_panel, height=6)
        self.comparisons.bind('<Configure>', self._arrange_comparisons)
        self._arrange_comparisons()
        self.reference = tk.StringVar(value='选择一道检查题，查看题目、参考回答以及前后变化。')
        self.wrap_label(page, textvariable=self.reference, size=11, color=self.colors['muted']).pack(fill='x', pady=(10, 0))

    def _arrange_comparisons(self, event=None):
        wide = self.comparisons.winfo_width() >= round(680 * text_scale(self.root))
        if wide == getattr(self, '_wide_comparisons', None):
            return
        self._wide_comparisons = wide
        self.comparisons.columnconfigure(0, weight=1, uniform='answers' if wide else '')
        self.comparisons.columnconfigure(1, weight=int(wide), uniform='answers' if wide else '')
        self.before_panel.grid(row=0, column=0, sticky='nsew', padx=(0, 7 if wide else 0))
        self.after_panel.grid(row=0 if wide else 1, column=int(wide), sticky='nsew',
                              padx=(7 if wide else 0, 0), pady=(0 if wide else 8, 0))

    def _knowledge_page(self, page):
        self.heading(page, '把一次纠错，变成可复用的做法', '保存实际结果，核对依据，再决定哪些做法值得下次使用。')
        self.knowledge_panel = TrainingKnowledgePanel(self, page)
        self.knowledge_panel.notice.set('训练结果与客户端的使用记录会保存在同一个经验库中。选择记录后即可纠错。')

    def select_page(self, index):
        if not 0 <= index < len(self.pages):
            raise ValueError('未知页面')
        for page in self.pages:
            page.pack_forget()
        self.pages[index].pack(fill='both', expand=True)
        for number, button in enumerate(self.nav):
            button.configure(variant='selected' if number == index else 'ghost')
        self.page_index = index
        if index == 1:
            self.refresh_runs()

    def toggle_parameters(self):
        if self.parameters.winfo_manager():
            self.parameters.pack_forget()
        else:
            self.parameters.pack(fill='x')

    def choose_data(self):
        if self.busy:
            return
        selected = filedialog.askdirectory(parent=self.root, title='选择练习题文件夹')
        if selected:
            self.dataset.set(selected)
            self.data_notice.set('已选择新文件夹，请先检查数据。')

    def choose_adapter(self):
        selected = filedialog.askdirectory(parent=self.root, title='选择以前的学习成果（含 adapter_config.json）')
        if selected:
            self.adapter.set(selected)

    def data_help(self):
        self.show_details('准备自己的题目',
            '文件夹中需要四份 UTF-8 文本文件，每行写一道题：\n\n' +
            '\n'.join(f'{name}：{title}' for name, title in DATA_NAMES.items()) +
            '\n\n每行格式：\n{"id":"唯一编号","prompt":"问题","response":"核实过的参考回答"}\n\n'
            '四份文件中的题号和问题都不能重复。检查题不要拿来反复调参。\n'
            '单题过长会在训练前检查时提示，请保留关键内容。')

    def refresh_setup(self):
        tools_ready = extension.training_python() is not None
        model_ready = self.model_ready()
        selected = self.trainer_model.get() == 'legacy-training-model'
        self.preparation_notice.set('推荐 Qwen3-0.6B · 独立的入门文字训练模型 · 约 1.5 GB\n' +
            ('模型文件已下载' if model_ready else '模型文件未下载') + ' · ' +
            ('已选择，仅开始训练时加载' if selected else '尚未选择，下载后请点“使用这个模型”') + '\n' +
            ('训练工具已准备好' if tools_ready else '训练工具尚未准备，需另行下载约数 GB'))
        self.tools_button.configure(text='训练工具已就绪' if tools_ready else '准备训练工具')
        self.model_button.configure(text='模型已下载' if model_ready else '下载入门训练模型',
                                    state='disabled' if model_ready or self.busy else 'normal')
        self.use_model_button.configure(text='已选择此模型' if selected else '使用这个模型',
                                       state='normal' if model_ready and not selected and not self.busy else 'disabled')
        self.root.after_idle(self.tools_button.master.reflow)

    def use_training_model(self):
        if self.busy:
            return
        if not self.model_ready():
            self.status.set('请先下载推荐的入门训练模型，再选择使用。')
            self.refresh_setup()
            return
        self.settings.set('trainer_model', 'legacy-training-model')
        self.trainer_model.set('legacy-training-model')
        self.refresh_setup()
        self.status.set('已选择 Qwen3-0.6B。现在没有加载权重，点击“开始训练”后才会使用。')

    def model_ready(self):
        return (ROOT / 'models/Qwen3-0.6B/model.safetensors').is_file() or self.manager.ready('legacy-training-model')

    def prepare_tools(self):
        def emit(text):
            self.events.put(('update', lambda value: self.write(self.log, value + '\n', True), text))
        self.submit('正在准备训练工具', lambda: extension.install_training(emit, self.cancel_event),
                    lambda _: self.refresh_setup(), cancellable=True)

    def prepare_model(self):
        if self.model_ready():
            self.refresh_setup()
            self.status.set('已有模型文件可直接复用，无需重新下载。请点“使用这个模型”。')
            return
        def progress(event):
            total = event.get('total', 0)
            amount = event.get('downloaded', 0)
            text = f'入门模型：{amount / 1e6:.0f} / {total / 1e6:.0f} MB' if total else event.get('message', '正在准备入门模型')
            self.events.put(('update', self.phase.set, text))
        self.submit('正在下载入门训练模型', lambda: self.manager.install('legacy-training-model', progress, self.cancel_event),
                    lambda _: self.refresh_setup(), cancellable=True)

    def check_data(self):
        directory = self.dataset.get()
        def done(counts):
            self.settings.set('trainer_dataset', directory)
            self.data_notice.set('检查通过 · ' + ' · '.join(f'{DATA_NAMES[name]} {count} 道' for name, count in counts.items()))
            self.write(self.log, '文件格式、空内容和重复项检查通过。教学内容仍需你核实，检查题不会用于训练。')
        self.submit('正在检查练习题', lambda: bridge.inspect_data(directory), done)

    def finetune(self):
        if self.busy:
            return
        if self.trainer_model.get() != 'legacy-training-model':
            self.refresh_setup()
            self.select_page(0)
            self.status.set('尚未选择训练模型。请先在推荐卡片中点击“使用这个模型”。')
            return
        interpreter = extension.training_python()
        if interpreter is None or not self.model_ready():
            self.refresh_setup()
            self.select_page(0)
            self.status.set('请先准备训练工具和独立的入门模型。')
            return
        from compute_guard import busy_reason
        occupied = busy_reason()
        if occupied:
            self.status.set(occupied + ' 请先在客户端设置中释放模型。')
            return
        stop_path = ARTIFACTS / ('stop-' + uuid4().hex + '.flag')
        settings = (self.dataset.get(), self.epochs.get(), self.learning_rate.get(),
                    self.adapter.get().strip(), self.agent_target.get(), stop_path)
        self.stop_path = stop_path
        self.training_details = {'events': []}
        self.finetune_details = self.training_details
        self.write(self.log, '开始检查教学数据并准备训练。原始模型与以前的成果都会保留。\n')
        def work():
            try:
                command = bridge.training_command(*settings)
                command[0] = str(interpreter)
                if self.cancel_event.is_set():
                    return {'cancelled': True}
                return bridge.train_process(command, lambda event: self.events.put(('update', self.training_event, event)))
            except Exception as exc:
                try:
                    self.knowledge.record('本地训练未完成：' + settings[0], str(exc), domain='training',
                        outcome='failure', verification='objective', source='training_run',
                        evidence={'epochs': settings[1], 'learning_rate': settings[2]}, training_eligible=False)
                except Exception:
                    logging.exception('Unable to save training failure')
                raise
            finally:
                stop_path.unlink(missing_ok=True)
        def done(result):
            self.training_details['result'] = result
            if result.get('cancelled'):
                self.phase.set('已安全停止')
                self.result_notice.set('这次训练未完成全部检查，不会标记为可用成果；原始记录仍然保留。')
            else:
                self.phase.set('训练与检查已完成')
                path = Path(result['run_dir'])
                loaded = load_result(path)
                self.result_notice.set(result_text(loaded))
                self.refresh_runs()
        self.submit('正在训练，显卡暂由训练工作台使用', work, done, kind='training', cancellable=True)

    def training_event(self, event):
        if self.training_details is not None:
            self.training_details['events'].append(event)
        kind = event.get('event')
        if kind == 'loaded':
            text = f"模型已加载 · {event.get('gpu', '')}"
        elif kind == 'train':
            text = f"第 {event['epoch']} 轮 · 已调整 {event['update']} 次 · 练习题偏差 {event['mean_loss']:.4f}"
        elif kind == 'epoch_end':
            text = f"第 {event['epoch']} 轮完成 · 新题偏差 {event['validation_loss']:.4f}（不是正确率）"
        elif kind in ('baseline', 'evaluation'):
            text = ('训练前检查' if kind == 'baseline' else '训练后检查') + ' · ' + str(event.get('id', ''))
        elif kind == 'training_experience':
            self.last_episode = event.get('episode_id')
            text = '本次训练已记入经验库，待确认建议仍需核实。'
        elif kind == 'log':
            text = event.get('text', '')
        elif kind == 'memory_warning':
            text = '训练成果已保存；经验记录未成功：' + event.get('message', '')
        elif event.get('completed'):
            text = '训练结果已保存，正在整理记录。'
        else:
            return
        if kind != 'log':
            self.phase.set(text)
        self.write(self.log, text + '\n', True)

    def stop_training(self):
        if not self.busy:
            return
        self.cancel_event.set()
        if self.current_job == 'training' and self.stop_path:
            self.stop_path.parent.mkdir(parents=True, exist_ok=True)
            self.stop_path.touch()
            self.status.set('已请求安全停止，等待当前训练或检查步骤结束。')
        elif self.current_job == 'hf-dataset':
            self.status.set('正在停止数据请求，未完成的导入不会选作训练数据。')
        else:
            self.status.set('正在停止准备任务，已下载的文件会保留。')
        self._set_stop('disabled')

    def _set_stop(self, state):
        for button in self.stop_buttons:
            button.configure(state=state)

    def refresh_runs(self):
        selected = self.run_tree.selection()
        rows = training_runs()
        self.run_rows = {str(index): row for index, row in enumerate(rows)}
        self.run_tree.delete(*self.run_tree.get_children())
        for identity, row in self.run_rows.items():
            self.run_tree.insert('', 'end', iid=identity, values=('已完成' if row['completed'] else '未完成', row['name']))
        if selected and selected[0] in self.run_rows:
            self.run_tree.selection_set(selected[0])

    def latest_result(self):
        self.select_page(1)
        completed = next((identity for identity, row in self.run_rows.items() if row['completed']), None)
        if completed is None:
            self.status.set('还没有完成全部检查的训练。')
            return
        self.run_tree.selection_set(completed)
        self.run_tree.see(completed)
        self.select_run()

    def select_run(self, event=None):
        selected = self.run_tree.selection()
        if not selected or selected[0] not in self.run_rows:
            return
        row = self.run_rows[selected[0]]
        if not row['completed']:
            self.result = None
            self.record_summary.set('这次训练未完成全部检查，不能据此判断是否进步。可打开文件夹查看保留的记录。')
            self.question_picker.configure(values=())
            self.question.set('')
            self.write(self.before_answer, '')
            self.write(self.after_answer, '')
            self.reference.set('未完成的实验不会被当成可用成果。')
            return
        try:
            self.result = load_result(row['path'])
            self.finetune_details = self.result['summary']
            self.record_summary.set(result_text(self.result))
            values = [f"{index + 1}. {item.get('prompt', '')[:90]}" for index, item in enumerate(self.result['comparisons'])]
            self.question_picker.configure(values=values)
            if values:
                self.question_picker.current(0)
                self.select_question()
            else:
                self.question.set('没有保存逐题回答对比')
                self.write(self.before_answer, '')
                self.write(self.after_answer, '')
        except Exception as exc:
            self.status.set('无法读取这次结果：' + str(exc))

    def select_question(self, event=None):
        if self.result is None:
            return
        index = self.question_picker.current()
        if not 0 <= index < len(self.result['comparisons']):
            return
        row = self.result['comparisons'][index]
        for field, widget in (('before', self.before_answer), ('after', self.after_answer)):
            value = row.get(field, {})
            text = value.get('text', '未记录') if isinstance(value, dict) else str(value)
            if isinstance(value, dict) and value.get('hit_token_limit'):
                text += '\n\n[本次回答达到长度上限，可能尚未说完。]'
            self.write(widget, text)
        group = '基础能力检查' if row.get('group') == 'general' else '新题检查'
        self.reference.set(f"{group} · 问题：{row.get('prompt', '')}\n参考回答：{row.get('response', '未记录')}")

    def open_outputs(self):
        RUNS_ROOT.mkdir(parents=True, exist_ok=True)
        os.startfile(RUNS_ROOT)

    def open_selected_run(self):
        selected = self.run_tree.selection()
        if selected and selected[0] in self.run_rows:
            os.startfile(self.run_rows[selected[0]]['path'])

    def show_details(self, title, value):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry('900x620')
        window.configure(bg=self.colors['bg'])
        text = self.text(window, height=20)
        self.write(text, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str))

    def submit(self, title, work, done, *, kind='task', cancellable=False):
        if self.busy:
            self.status.set('当前任务还在进行，请先等待结束或停止。')
            return
        self.busy = True
        if hasattr(self, 'dataset_panel'):
            self.dataset_panel.sync_busy()
        self.current_job = kind
        self.cancel_event.clear()
        self.status.set(title)
        for button in self.buttons:
            button.configure(state='disabled')
        self._set_stop('normal' if cancellable else 'disabled')
        def run():
            try:
                self.events.put(('done', done, work()))
            except Exception as exc:
                self.events.put(('error', None, exc))
        threading.Thread(target=run, name='freeai-training-task', daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, callback, value = self.events.get_nowait()
                if kind == 'update':
                    callback(value)
                    continue
                self.busy = False
                if hasattr(self, 'dataset_panel'):
                    self.dataset_panel.sync_busy()
                self.current_job = None
                self.stop_path = None
                self._set_stop('disabled')
                for button in self.buttons:
                    button.configure(state='normal')
                if kind == 'error':
                    message = str(value)
                    self.status.set(message)
                    self.phase.set('已停止' if self.cancel_event.is_set() else '本次任务未完成')
                    self.write(self.log, '\n' + message + '\n', True)
                else:
                    self.status.set('已完成，记录保存在本机。')
                    callback(value)
        except queue.Empty:
            pass
        except Exception as exc:
            self.status.set('界面更新未完成：' + str(exc))
            logging.exception('Trainer callback failed')
        self.poll_id = self.root.after(80, self.poll)

    def callback_error(self, kind, value, traceback):
        logging.error('Trainer UI error', exc_info=(kind, value, traceback))
        self.status.set(str(value))

    def close(self):
        if self.busy:
            self.stop_training()
            self.status.set('正在停止当前任务。结束后可以关闭，训练记录会保留。')
            return
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    extension.prepare_starter_data()
    logging.basicConfig(filename=ARTIFACTS / 'trainer.log', encoding='utf-8', level=logging.INFO)
    root = tk.Tk()
    TrainerApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
