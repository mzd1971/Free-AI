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
    COLORS, CLIENT_LIGHT_COLORS, FlatButton, RoundedFrame, ScrollArea, background, label,
    set_text_size, setup_theme, text_scale, ui_font,
)
from ui_preferences import TEXT_SIZES, load_text_size, save_text_size


DATA_NAMES = {'sft_train.jsonl': '练习题', 'sft_validation.jsonl': '挑选成果用的新题',
              'sft_test.jsonl': '最后检查用的新题', 'sft_general_test.jsonl': '基础能力检查题'}
TRAINER_CONTENT_WIDTH = 1200
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
        acceptance = {'status': 'needs_review'}
        if summary.get('completed') is True:
            try:
                from training_evaluation import evaluate_run
                acceptance = evaluate_run(path)
            except (OSError, ValueError, TypeError, KeyError):
                pass
        rows.append({'path': path, 'name': path.name, 'summary': summary, 'acceptance': acceptance,
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
    from training_evaluation import evaluate_run
    return {'path': directory, 'summary': summary, 'comparisons': comparisons,
            'acceptance': evaluate_run(directory)}


def acceptance_label(report):
    return {'passed': '验收通过', 'failed': '验收未通过',
            'needs_review': '需要核实'}.get(report.get('status'), '需要核实')


def result_text(result):
    summary = result['summary']
    def number(key, digits=4):
        value = summary.get(key)
        return f'{value:.{digits}f}' if isinstance(value, (int, float)) else '未记录'
    acceptance = result.get('acceptance', {'status': 'needs_review'})
    reasons = '；'.join(str(item) for item in acceptance.get('reasons', []))
    verdict = acceptance_label(acceptance)
    if acceptance.get('status') == 'passed':
        verdict += ' · 仅代表本批检查题达标，可以加入客户端后试用。'
    scores = []
    for group, title in (('domain', '新题'), ('general', '基础能力')):
        metric = acceptance.get('metrics', {}).get(group, {})
        count = metric.get('evaluable', 0)
        if count:
            scores.append(f"{title}通过数：{metric.get('before_passed', 0)}/{count} → "
                          f"{metric.get('after_passed', 0)}/{count}（共 {metric.get('total', 0)} 题）")
        else:
            scores.append(f'{title}：暂无可自动评分的完整对照')
    return (verdict + ('\n' + reasons if reasons else '') + '\n\n'
            + '\n'.join(scores) + '\n'
            f"验证题回答偏差：训练前 {number('baseline_validation_loss')} → 训练后 {number('best_validation_loss')}\n"
            '这是回答偏差的数值，越低越好；它不等于正确率。\n'
            f"保留第 {summary.get('best_epoch', '未记录')} 轮成果 · 调整 {summary.get('updates', '未记录')} 次 · "
            f"训练用时 {number('training_seconds', 1)} 秒\n"
            f"另有 {len(result['comparisons'])} 道未参与训练的题目，可逐题查看前后回答。")


class TrainingKnowledgePanel(EpistemologyPanel):
    def __init__(self, app, page):
        super().__init__(app, page, responsive=True, auto_refresh=False)
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
        studio_palette = {**CLIENT_LIGHT_COLORS, 'bg': '#f5f7fa', 'side': '#f9fafb'}
        setup_theme(root, 'Free AI · 训练工作台', size=(1440, 920), min_size=(760, 560),
                    text_size=preference, theme=self.theme, palette=studio_palette if self.theme == 'light' else None)
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
        self.checked_dataset = None
        self.adapter = tk.StringVar()
        self.epochs = tk.StringVar(value='1')
        self.learning_rate = tk.StringVar(value='0.0001')
        self.agent_target = tk.BooleanVar(value=False)
        self.phase = tk.StringVar(value='等待开始')
        self.data_notice = tk.StringVar(value='练习题和检查题分开保存。')
        self.preparation_notice = tk.StringVar()
        self.result_notice = tk.StringVar(value='完成后可查看验收结论与回答对比。')
        self.result = None
        self.run_rows = {}
        self.finetune_details = None
        self.training_details = None
        self.last_episode = None
        self._build()
        self.dataset.trace_add('write', self._dataset_changed)
        self.phase.trace_add('write', lambda *_: self.refresh_studio())
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = self.callback_error
        self.refresh_setup()
        self.refresh_runs()
        self.select_page(0)
        self.poll_id = root.after(80, self.poll)

    def frame(self, parent, color=None, **kwargs):
        return tk.Frame(parent, bg=color or self.colors['bg'], **kwargs)

    def _build(self):
        from trainer_studio import build_shell
        build_shell(self)
        self._training_page(self.train_scroll.body)
        self._records_page(self.records_scroll.body)
        self._knowledge_page(self.knowledge_scroll.body)
        for page in self.pages:
            page.bind_wheel(page.body)
        self.root.bind('<<FreeAITextSizeChanged>>', self._queue_reflow, add='+')

    def refresh_studio(self, **kwargs):
        from trainer_studio import refresh_studio
        refresh_studio(self, **kwargs)

    def _dataset_changed(self, *args):
        self.checked_dataset = None
        self.data_notice.set('数据位置已变更，请重新检查。')
        self.refresh_studio()

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

    def _queue_reflow(self, event=None):
        if getattr(self, '_layout_after_id', None):
            self.root.after_cancel(self._layout_after_id)
        self._layout_after_id = self.root.after_idle(self.reflow)

    def reflow(self):
        self._layout_after_id = None
        from trainer_studio import arrange_studio
        self._resize_sidebar()
        arrange_studio(self)
        for row in self.control_rows:
            row.reflow()
        self._arrange_comparisons()
        self._arrange_record_stats()
        self.knowledge_panel.arrange_practice()

    def _resize_sidebar(self):
        from trainer_studio import resize_sidebar
        resize_sidebar(self)

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
        self.wrap_label(page, title, size=26, bold=True).pack(fill='x')
        self.wrap_label(page, subtitle, size=11, color=self.colors['muted']).pack(fill='x', pady=(5, 18))

    def card(self, parent, title):
        container = RoundedFrame(parent, bg=self.colors['surface'], radius=14,
                                 padding=(20, 16), border=self.colors['card_border'])
        container.pack(fill='x', pady=(0, 14))
        box = container.body
        box.container = container
        self.wrap_label(box, title, size=14, bold=True).pack(fill='x', pady=(0, 10))
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
                         highlightcolor=self.colors['focus'],
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
        from trainer_studio import build_training_page
        build_training_page(self, page)

    def toggle_online_data(self):
        if self.dataset_panel.winfo_manager():
            self.dataset_panel.pack_forget()
            self.online_data_button.configure(text='查找在线数据')
        else:
            self.dataset_panel.pack(fill='x', pady=(12, 4))
            self.online_data_button.configure(text='收起在线数据')
            self.dataset_panel.sync_busy()
            self.train_scroll.bind_wheel(self.dataset_panel)
        from trainer_studio import arrange_studio
        arrange_studio(self)

    def imported_dataset(self, result):
        directory = Path(result['path'])
        counts = bridge.inspect_data(directory)
        self.dataset.set(str(directory))
        self.checked_dataset = self.dataset.get()
        self.settings.set('trainer_dataset', str(directory))
        self.data_notice.set('已导入并检查：' + ' · '.join(f'{DATA_NAMES[name]} {count} 条' for name, count in counts.items()))
        self.status.set('训练数据已准备好。核对内容后，再点击“开始训练”。')
        self.write(self.log, '\n在线数据保存位置：' + str(directory) + '\n来源与导入记录：' + str(result['report']) + '\n', True)
        self.refresh_studio()

    def _records_page(self, page):
        self.heading(page, '训练记录', '查看验收依据与回答对比。')
        self.record_totals = {name: tk.StringVar(value='0') for name in ('all', 'passed', 'review', 'failed')}
        self.record_stats = self.frame(page)
        self.record_stats.pack(fill='x', pady=(0, 12))
        self.record_tiles = []
        for key, caption in (('all', '全部训练'), ('passed', '验收通过'), ('review', '待核实'), ('failed', '未通过 / 未完成')):
            tile = RoundedFrame(self.record_stats, bg=self.colors['surface'], radius=12,
                                border=self.colors['card_border'], padding=(16, 11))
            label(tile.body, textvariable=self.record_totals[key], size=23, bold=True,
                  color=self.colors['accent'] if key == 'passed' else self.colors['text']).pack(anchor='w')
            label(tile.body, caption, size=10, color=self.colors['muted']).pack(anchor='w', pady=(3, 0))
            self.record_tiles.append(tile)
        self.record_stats.bind('<Configure>', self._arrange_record_stats)
        self._arrange_record_stats()
        library = self.card(page, '训练历史')
        toolbar = self.button_row(library)
        toolbar.pack(fill='x', pady=(0, 12))
        self.record_filter = tk.StringVar(value='全部记录')
        self.record_filter_picker = ttk.Combobox(toolbar, textvariable=self.record_filter, state='readonly',
            values=('全部记录', '验收通过', '需要核实', '验收未通过', '未完成'), width=12,
            font=ui_font(self.root, 11), style='Studio.TCombobox')
        self.record_filter_picker.bind('<<ComboboxSelected>>', lambda event: self.refresh_runs())
        self.button(toolbar, '刷新', self.refresh_runs, variant='ghost', width=75)
        self.button(toolbar, '查看文件', self.open_selected_run, variant='ghost', width=100)
        self.button(toolbar, '验收说明', self.acceptance_help, variant='ghost', width=100)
        self.run_tree = self.tree(library, columns=('status', 'name'), show='headings', height=5)
        self.run_tree.heading('status', text='验收状态')
        self.run_tree.column('status', width=130, minwidth=90, stretch=False)
        self.run_tree.heading('name', text='训练名称')
        self.run_tree.column('name', width=680, minwidth=240)
        self.run_tree.bind('<<TreeviewSelect>>', self.select_run)
        self.records_empty = self.frame(library, self.colors['surface'])
        self.records_empty_caption = tk.StringVar(value='还没有训练记录')
        self.wrap_label(self.records_empty, textvariable=self.records_empty_caption, size=16, bold=True).pack(fill='x', pady=(20, 6))
        self.wrap_label(self.records_empty, '完成一次训练后，结果会自动保存在这里。', size=11,
                        color=self.colors['muted']).pack(fill='x', pady=(0, 18))
        empty_actions = self.button_row(self.records_empty)
        empty_actions.pack(fill='x', pady=(0, 18))
        self.button(empty_actions, '前往训练工作室', lambda: self.select_page(0), variant='accent', width=190)
        self.button(empty_actions, '显示全部记录', self.clear_record_filter, variant='ghost', width=155)
        self.result_area = self.frame(page)
        summary_card = self.card(self.result_area, '验收结果')
        self.record_summary = tk.StringVar(value='选择一条训练记录，查看验收依据。')
        self.wrap_label(summary_card, textvariable=self.record_summary, size=11).pack(fill='x', pady=(2, 10))
        result_tools = self.button_row(summary_card)
        result_tools.pack(fill='x')
        self.publish_button = self.button(result_tools, '加入客户端模型', self.publish_result, variant='primary', width=185)
        self.publish_button.configure(state='disabled')
        self.button(result_tools, '详细检查结果', lambda: self.show_details('训练详细记录', self.finetune_details), variant='ghost', width=150)
        answers = self.card(self.result_area, '回答对比')
        self.question = tk.StringVar()
        self.question_picker = ttk.Combobox(answers, textvariable=self.question, state='readonly',
                                            font=ui_font(self.root, 11), style='Studio.TCombobox')
        self.question_picker.pack(fill='x', pady=(0, 14))
        self.question_picker.bind('<<ComboboxSelected>>', self.select_question)
        self.comparisons = self.frame(answers, self.colors['surface'])
        self.comparisons.pack(fill='x')
        self.before_panel = self.frame(self.comparisons, self.colors['surface'])
        self.after_panel = self.frame(self.comparisons, self.colors['surface'])
        label(self.before_panel, '训练前', size=12, bold=True, color=self.colors['muted']).pack(anchor='w', pady=(0, 6))
        label(self.after_panel, '训练后', size=12, bold=True, color=self.colors['accent']).pack(anchor='w', pady=(0, 6))
        self.before_answer = self.text(self.before_panel, height=7)
        self.after_answer = self.text(self.after_panel, height=7)
        self.comparisons.bind('<Configure>', self._arrange_comparisons)
        self._arrange_comparisons()
        self.reference = tk.StringVar(value='选择一道检查题，查看训练前后的变化。')
        self.wrap_label(answers, textvariable=self.reference, size=11, color=self.colors['muted']).pack(fill='x', pady=(12, 0))

    def _arrange_record_stats(self, event=None):
        columns = 4 if self.record_stats.winfo_width() >= 780 * text_scale(self.root) else 2
        for column in range(4):
            self.record_stats.columnconfigure(column, weight=int(column < columns), uniform='stats' if column < columns else '')
        for index, tile in enumerate(self.record_tiles):
            tile.grid(row=index // columns, column=index % columns, sticky='nsew',
                      padx=(0, 12 if index % columns < columns - 1 else 0), pady=(0, 8))

    def clear_record_filter(self):
        self.record_filter.set('全部记录')
        self.refresh_runs()

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
        self.heading(page, '经验库', '保存实际结果，核对依据，把一次纠错变成可复用的做法。')
        self.knowledge_panel = TrainingKnowledgePanel(self, page)
        self.knowledge_panel.notice.set('训练结果与客户端的使用记录会保存在同一个经验库中。选择记录后即可纠错。')

    def select_page(self, index):
        from trainer_studio import PAGE_TITLES
        if not 0 <= index < len(self.pages):
            raise ValueError('未知页面')
        for page in self.pages:
            page.pack_forget()
        self.pages[index].pack(fill='both', expand=True)
        for number, button in enumerate(self.nav):
            button.configure(variant='selected' if number == index else 'ghost')
        self.page_index = index
        self.page_caption.set(PAGE_TITLES[index])
        if index == 1:
            self.refresh_runs()
        elif index == 2 and not self.busy:
            self.knowledge_panel.refresh()

    def toggle_parameters(self):
        if self.parameters.winfo_manager():
            self.parameters.pack_forget()
            self.parameters_button.configure(text='更多设置')
        else:
            self.parameters.pack(fill='x')
            self.parameters_button.configure(text='收起设置')

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
        self.preparation_notice.set('本地微调 · 模型约 1.5 GB' +
                                    (' · 训练工具需另行下载数 GB' if not tools_ready else ''))
        self.tools_button.configure(text='训练工具已就绪' if tools_ready else '准备训练工具')
        self.model_button.configure(text='模型已下载' if model_ready else '下载模型',
                                    state='disabled' if model_ready or self.busy else 'normal')
        self.use_model_button.configure(text='已选用' if selected else '选用模型',
                                       state='normal' if model_ready and not selected and not self.busy else 'disabled')
        self._queue_reflow()
        self.refresh_studio(tools_ready=tools_ready, model_ready=model_ready)

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
            self.status.set('已有模型文件。请选择“选用模型”。')
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
        self.checked_dataset = None
        def done(counts):
            if self.dataset.get() != directory:
                self.status.set('已检查原文件夹；当前数据位置已变化，请重新检查。')
                return
            self.checked_dataset = directory
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
            self.status.set('尚未选择训练模型。请点击“选用模型”。')
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
        from trainer_studio import set_log_visible
        set_log_visible(self, True)
        self.phase.set('等待开始')
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
                self.status.set('训练已安全停止，本次任务未完成。')
                self.result_notice.set('已安全停止。可在训练记录中查看本次运行。')
            else:
                path = Path(result['run_dir'])
                loaded = load_result(path)
                self.phase.set('训练已结束 · ' + acceptance_label(loaded['acceptance']))
                self.status.set(self.phase.get())
                self.result_notice.set('结果已保存 · ' + acceptance_label(loaded['acceptance']))
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
        self.refresh_studio()

    def _set_stop(self, state):
        for button in self.stop_buttons:
            button.configure(state=state)

    def refresh_runs(self):
        selected = self.run_tree.selection()
        selected_path = self.run_rows.get(selected[0], {}).get('path') if selected else None
        rows = training_runs()
        statuses = [acceptance_label(row.get('acceptance', {})) if row['completed'] else '未完成' for row in rows]
        counts = {'all': len(rows), 'passed': statuses.count('验收通过'),
                  'review': statuses.count('需要核实'),
                  'failed': statuses.count('验收未通过') + statuses.count('未完成')}
        for key, value in counts.items():
            self.record_totals[key].set(str(value))
        chosen_filter = self.record_filter.get()
        if chosen_filter != '全部记录':
            rows = [row for row, status in zip(rows, statuses) if status == chosen_filter]
        self.run_rows = {str(index): row for index, row in enumerate(rows)}
        self.run_tree.delete(*self.run_tree.get_children())
        for identity, row in self.run_rows.items():
            status = acceptance_label(row.get('acceptance', {})) if row['completed'] else '未完成'
            self.run_tree.insert('', 'end', iid=identity, values=(status, row['name']))
        self.run_tree.configure(height=min(6, max(2, len(rows))))
        if rows:
            self.records_empty.pack_forget()
            self.run_tree.master.pack(fill='x')
        else:
            self.run_tree.master.pack_forget()
            self.records_empty_caption.set('还没有训练记录' if counts['all'] == 0 else '没有符合条件的记录')
            self.records_empty.pack(fill='x')
        identity = next((key for key, row in self.run_rows.items() if row['path'] == selected_path), None)
        if identity is not None:
            self.run_tree.selection_set(identity)
            self.select_run()
        else:
            self.result = None
            self.result_area.pack_forget()
            self._refresh_publish_button()

    def latest_result(self):
        self.record_filter.set('全部记录')
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
            self.result = None
            self.result_area.pack_forget()
            self._refresh_publish_button()
            return
        self.result_area.pack(fill='x', pady=(8, 0))
        row = self.run_rows[selected[0]]
        self.finetune_details = row.get('summary', {})
        if not row['completed']:
            self.result = None
            self._refresh_publish_button()
            self.record_summary.set('这次训练未完成全部检查，不能据此判断是否进步。可打开文件夹查看保留的记录。')
            self.question_picker.configure(values=())
            self.question.set('')
            self.write(self.before_answer, '')
            self.write(self.after_answer, '')
            self.reference.set('未完成的实验不会被当成可用成果。')
            return
        try:
            self.result = load_result(row['path'])
            self.finetune_details = {**self.result['summary'], 'acceptance': self.result['acceptance']}
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
            self.result = None
            self.status.set('无法读取这次结果：' + str(exc))
            self.record_summary.set('结果文件无法读取，请打开文件夹检查原始记录。')
            self.question_picker.configure(values=())
            self.question.set('')
            self.reference.set('当前记录无法用于验收。')
            self.write(self.before_answer, '')
            self.write(self.after_answer, '')
        self._refresh_publish_button()

    def _refresh_publish_button(self):
        if hasattr(self, 'publish_button'):
            eligible = (not self.busy and self.result is not None
                        and self.result.get('acceptance', {}).get('status') == 'passed')
            self.publish_button.configure(state='normal' if eligible else 'disabled')

    def publish_result(self):
        if self.busy:
            return
        if self.result is None or self.result.get('acceptance', {}).get('status') != 'passed':
            self.status.set('请先选择通过验收的训练记录。没有评分依据的成果需要先补充检查。')
            return
        directory = self.result['path']
        from training_publish import publish_run
        def progress(event):
            self.events.put(('update', self.status.set, event.get('message', '正在准备训练成果')))
        def done(identity):
            self.status.set('已加入客户端模型：' + self.manager.catalog[identity]['title'] +
                            '。在客户端输入框的模型菜单中选择使用。')
            self.write(self.log, '\n' + self.status.get() + '\n', True)
            self._refresh_publish_button()
        self.submit('正在复查并转换训练成果',
                    lambda: publish_run(self.manager, directory, progress=progress, cancel_event=self.cancel_event),
                    done, kind='publish', cancellable=True)

    def acceptance_help(self):
        self.show_details('训练成果如何验收',
            '训练结束和效果达标是两件事。系统分别检查新题表现、基础能力以及验证题偏差。\n\n'
            '每组至少需要 5 道可评分的检查题，可评分覆盖率与训练后通过率均至少 80%，'
            '而且两组得分与验证题偏差都不能退步。达标只代表这批检查题，不代表所有任务都变强。\n\n'
            '在 sft_test.jsonl 和 sft_general_test.jsonl 的题目中加入 evaluation 字段：\n'
            '精确答案："evaluation": {"type": "exact"}\n'
            '完整 JSON 答案："evaluation": {"type": "json"}\n'
            '人工查看："evaluation": {"type": "manual"}\n\n'
            '精确答案只整理首尾空白、换行和 Unicode 表示，不把相似表述当作相等。'
            'JSON 按完整结构和值比较，不要求对象字段顺序相同。没有规则的开放式回答不会自动打分。\n\n'
            '通过验收后点击“加入客户端模型”。目前支持本工作台产生的 Qwen3-0.6B LoRA 成果，'
            '客户端会加载匹配的基础模型与补丁，不会把补丁套到别的模型上。'
            '旧记录缺少权重与检查结果的校验资料时，需要重新训练生成完整记录。')

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
        case = next((case for case in self.result.get('acceptance', {}).get('cases', [])
                     if case['id'] == row.get('id')), {})
        statuses = {'passed': '通过', 'failed': '未通过', 'token_limit': '输出被截断',
                    'missing': '缺少结果', 'invalid': '结果无效', 'unscored': '人工查看'}
        score = ' · '.join(f"{title}：{statuses.get(case.get(key), '未评分')}"
                           for key, title in (('before', '训练前'), ('after', '训练后')))
        self.reference.set(f"{group} · {score}\n问题：{row.get('prompt', '')}\n参考回答：{row.get('response', '未记录')}")

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
        self.phase.set('等待开始')
        for button in self.buttons:
            button.configure(state='disabled')
        self._set_stop('normal' if cancellable else 'disabled')
        self.refresh_studio()
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
                    from trainer_studio import set_log_visible
                    set_log_visible(self, True)
                else:
                    self.status.set('本次操作已完成。')
                    callback(value)
                self._refresh_publish_button()
                self.refresh_setup()
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
        if getattr(self, '_layout_after_id', None):
            self.root.after_cancel(self._layout_after_id)
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
