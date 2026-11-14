"""Native Tk desktop client. Calls local Python models directly; no HTTP server."""
import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText
from pathlib import Path
from paths import ARTIFACTS, DATA_ROOT

ROOT = Path(__file__).resolve().parent
LOG = ARTIFACTS / 'desktop.log'


class DesktopApp:
    def __init__(self, root):
        self.root = root
        # Match point-sized fonts to this pixel layout on Windows high-DPI desktops.
        root.tk.call('tk', 'scaling', 1.5)
        self.events = queue.Queue()
        self.busy = False
        self.history = []
        self.last_episode = None
        from epistemology import KnowledgeStore
        self.knowledge = KnowledgeStore()
        self.buttons = []
        root.title('Free AI · 入门训练工作台')
        icon = ROOT / 'assets' / 'free-ai.ico'
        if icon.is_file():
            root.iconbitmap(str(icon))
        root.geometry(f'{min(1380, root.winfo_screenwidth()-80)}x{min(930, root.winfo_screenheight()-100)}+30+30')
        root.minsize(1020, 700)
        root.configure(bg='#f3f5fa')
        root.option_add('*Font', ('Microsoft YaHei UI', 11))
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 11), background='#f3f5fa', foreground='#24324a', bordercolor='#dde3ee', lightcolor='#ffffff', darkcolor='#dde3ee')
        style.layout('TNotebook.Tab', [])
        style.configure('TNotebook', borderwidth=0, background='#f3f5fa')
        style.configure('TButton', padding=(12, 8), background='#e7ecf5', borderwidth=0)
        style.map('TButton', background=[('active', '#dbe4f7')], foreground=[('disabled', '#98a3b4')])
        style.configure('Primary.TButton', background='#5165e8', foreground='white', font=('Microsoft YaHei UI', 11, 'bold'))
        style.map('Primary.TButton', background=[('active', '#4053ce'), ('disabled', '#a6afeb')], foreground=[('disabled', '#f5f6ff')])
        style.configure('TCheckbutton', background='#f3f5fa')
        style.configure('TEntry', fieldbackground='white', foreground='#24324a', padding=6)
        style.configure('TSpinbox', fieldbackground='white', foreground='#24324a', padding=6)
        style.configure('TLabelframe', borderwidth=1, relief='solid')
        style.configure('TLabelframe.Label', foreground='#63728b', font=('Microsoft YaHei UI', 11, 'bold'))
        self.sidebar = tk.Frame(root, bg='#17243a', width=205)
        self.sidebar.pack(side='left', fill='y')
        self.sidebar.pack_propagate(False)
        tk.Label(self.sidebar, text='Free AI', bg='#17243a', fg='white', font=('Segoe UI', 23, 'bold')).pack(anchor='w', padx=20, pady=(28, 3))
        tk.Label(self.sidebar, text='你的本地 AI 学习伙伴', bg='#17243a', fg='#a8b7d1', font=('Microsoft YaHei UI', 10)).pack(anchor='w', padx=20, pady=(0, 28))
        self.nav_buttons = []
        tk.Label(self.sidebar, text=f'本地保存位置\n\n{ARTIFACTS}\n\nQwen3 · 教 AI 学习', justify='left', bg='#17243a', fg='#a8b7d1', font=('Microsoft YaHei UI', 9), wraplength=165).pack(side='bottom', anchor='w', padx=20, pady=28)
        workspace = ttk.Frame(root)
        workspace.pack(side='left', fill='both', expand=True)
        ttk.Label(workspace, text='训练你的 AI', font=('Microsoft YaHei UI', 23, 'bold')).pack(anchor='w', padx=28, pady=(24, 6))
        ttk.Label(workspace, text='从一份教学数据开始，让每次学习都有记录可查。', foreground='#758299').pack(anchor='w', padx=30, pady=(0, 16))
        tabs = ttk.Notebook(workspace)
        tabs.pack(fill='both', expand=True, padx=12)
        self.tabs = tabs
        self.finetune_tab(tabs)
        self.chat_tab(tabs)
        from epistemology_ui import EpistemologyPanel
        self.epistemology_index = len(self.nav_buttons)
        self.epistemology = EpistemologyPanel(self, self.page(tabs, '经验与改进'))
        self.agent_tab(tabs)
        self.training_tab(tabs)
        self.docs_tab(tabs)
        footer = ttk.Frame(workspace)
        footer.pack(side='bottom', fill='x', padx=24, pady=12, before=tabs)
        self.status = tk.StringVar(value='就绪。首次生成需要加载模型，请稍候。')
        ttk.Label(footer, textvariable=self.status).pack(side='left')
        self.progress = ttk.Progressbar(footer, mode='indeterminate', length=140)
        self.progress.pack(side='right')
        self.select_page(0)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = self.callback_error
        root.after(100, self.poll)

    def page(self, tabs, title):
        frame = ttk.Frame(tabs, padding=16)
        tabs.add(frame, text=title)
        index = len(self.nav_buttons)
        button = tk.Button(self.sidebar, text=title, command=lambda: self.select_page(index), anchor='w',
                           bg='#17243a', fg='#c5d0e2', activebackground='#2c3c59', activeforeground='white',
                           relief='flat', bd=0, padx=20, pady=13, cursor='hand2', font=('Microsoft YaHei UI', 11))
        button.pack(fill='x', padx=10, pady=3)
        self.nav_buttons.append(button)
        return frame

    def select_page(self, index):
        self.tabs.select(index)
        for pos, button in enumerate(self.nav_buttons):
            button.configure(bg='#5165e8' if pos == index else '#17243a', fg='white' if pos == index else '#c5d0e2')

    def text(self, parent, height=10, editable=False):
        widget = ScrolledText(parent, height=height, wrap='word', bg='white', fg='#30405a',
                              insertbackground='#5165e8', relief='flat', padx=16, pady=14,
                              font=('Microsoft YaHei UI', 12), selectbackground='#dce3ff', selectforeground='#24324a')
        widget.pack(fill='both', expand=not editable, pady=8)
        if not editable:
            widget.configure(state='disabled')
        return widget

    @staticmethod
    def write(widget, value, append=False):
        widget.configure(state='normal')
        if not append:
            widget.delete('1.0', 'end')
        widget.insert('end', value)
        if append:
            widget.see('end')
        else:
            widget.yview_moveto(0)
        widget.configure(state='disabled')

    def show_details(self, title, value):
        """Keep complete machine-readable results available without filling the main page with JSON."""
        if value is None:
            messagebox.showinfo('暂无详细记录', '完成一次操作后，这里会显示完整技术记录。', parent=self.root)
            return
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry(f'{min(900, self.root.winfo_screenwidth()-120)}x{min(680, self.root.winfo_screenheight()-140)}')
        window.minsize(620, 420)
        ttk.Label(window, text=title, font=('Microsoft YaHei UI', 16, 'bold')).pack(anchor='w', padx=18, pady=(16, 6))
        ttk.Label(window, text='只读的完整技术数据，可用于排查和留档。', foreground='#63728b').pack(anchor='w', padx=18)
        detail = ScrolledText(window, wrap='word', bg='white', fg='#30405a', relief='flat', padx=14, pady=12,
                              font=('Consolas', 10), selectbackground='#dce3ff', selectforeground='#24324a')
        detail.pack(fill='both', expand=True, padx=18, pady=12)
        detail.insert('1.0', content)
        detail.configure(state='disabled')

    def button(self, parent, label, command):
        button = ttk.Button(parent, text=label, command=command,
                            style='Primary.TButton' if label in ('开始教 AI 学习', '发送', '开始办理', '开始学习演示') else 'TButton')
        button.pack(side='left', padx=(0, 10))
        self.buttons.append(button)
        return button

    def submit(self, label, work, done):
        if self.busy:
            return
        self.busy = True
        self.status.set(label + '…')
        self.progress.start(12)
        for button in self.buttons:
            button.configure(state='disabled')
        def worker():
            try:
                self.events.put(('done', done, work()))
            except Exception as exc:
                logging.exception('Desktop task failed: %s', label)
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
                self.progress.stop()
                for button in self.buttons:
                    button.configure(state='normal')
                if kind == 'error':
                    self.status.set('操作失败，可修正后重试。')
                    messagebox.showerror('操作失败', f'{payload}\n\n日志：{LOG}', parent=self.root)
                else:
                    self.status.set('已完成 · 数据保存在本机')
                    handler(payload)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def chat_tab(self, tabs):
        page = self.page(tabs, '中文聊天')
        ttk.Label(page, text='使用已训练的中文助手；实验模型仍可能答错。Ctrl+Enter 发送。').pack(anchor='w')
        self.transcript = self.text(page)
        self.input = self.text(page, 3, True)
        self.input.bind('<Control-Return>', lambda event: (self.send_chat(), 'break')[-1])
        row = ttk.Frame(page)
        row.pack(fill='x')
        self.button(row, '发送', self.send_chat)
        self.button(row, '清空对话', self.clear_chat)
        self.button(row, '纠正与提炼经验', self.open_epistemology)
        self.base = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text='对照：还没教过的 AI', variable=self.base).pack(side='left')
        self.chat_hint = tk.StringVar(value='自动查找不同问题类型的经验；生成结果待核实，纠正后可总结成做法。')
        ttk.Label(page, textvariable=self.chat_hint, foreground='#63728b').pack(anchor='w', pady=6)

    def open_epistemology(self):
        self.epistemology.refresh(self.last_episode)
        self.select_page(self.epistemology_index)

    def clear_chat(self):
        self.history.clear()
        self.write(self.transcript, '')

    def send_chat(self):
        if self.busy:
            return
        message = self.input.get('1.0', 'end').strip()
        if not message:
            return
        base, history = self.base.get(), list(self.history)
        self.write(self.transcript, f'你：{message}\n\n', True)
        self.input.delete('1.0', 'end')
        def work():
            from legacy_qwen_chat import chat
            return chat(message, history, use_base=base, memory_store=self.knowledge, return_details=True)
        def done(result):
            answer = result['answer']
            self.last_episode = result['episode_id']
            hook = result['trace'][0]
            self.chat_hint.set(f"已找到 {len(hook['rule_ids'])} 条可用做法、{len(hook['episode_ids'])} 条经验 · 本回答尚未核实")
            self.history.extend([{'role': 'user', 'content': message}, {'role': 'assistant', 'content': answer}])
            self.history = self.history[-6:]
            self.write(self.transcript, f'AI：{answer}\n\n', True)
        self.submit('模型生成中', work, done)

    def agent_tab(self, tabs):
        page = self.page(tabs, '自动办事')
        ttk.Label(page, text='AI 会先查找相关经验；目前可自动计算和读取最近学习结果，其他回答仍需你核实。').pack(anchor='w')
        self.task = self.text(page, 3, True)
        self.task.insert('1.0', '计算 237*19-84。')
        self.memory = tk.BooleanVar(value=True)
        self.trained = tk.BooleanVar(value=True)
        row = ttk.Frame(page)
        row.pack(fill='x')
        ttk.Checkbutton(row, text='办事前查找经验', variable=self.memory).pack(side='left')
        ttk.Checkbutton(row, text='使用以前训练的学习成果', variable=self.trained).pack(side='left')
        row = ttk.Frame(page)
        row.pack(fill='x', pady=8)
        self.button(row, '开始办理', self.run_agent)
        self.button(row, '纠正与查看经验', self.open_epistemology)
        self.button(row, '计算工具示范', lambda: self.memory_job(True))
        self.agent_details = None
        self.button(row, '查看详细记录', lambda: self.show_details('自动办事详细记录', self.agent_details))
        self.agent_output = self.text(page)

    def run_agent(self):
        task = self.task.get('1.0', 'end').strip()
        memory, trained = self.memory.get(), self.trained.get()
        def work():
            from agent_lab import run_agent, category_of
            from legacy_qwen_chat import chat, load_latest
            # The arithmetic-only adapter is not a general-purpose writing/code model.
            selected_agent = trained and category_of(task) == 'arithmetic'
            _, _, path = load_latest(agent_model=selected_agent)
            result = run_agent(task, memory, planner=lambda p: chat(p, agent_model=selected_agent, use_memory=False), knowledge=self.knowledge)
            result['model_path'] = str(path)
            return result
        self.submit('AI 正在办理', work, self.show_agent)

    def show_agent(self, result):
        if result.get('episode_id'):
            self.last_episode = result['episode_id']
        self.agent_details = result
        if result.get('demonstration'):
            answer = f"计算工具示范已完成。\n可用做法：{len(result.get('rules', []))} 条\n使用记录：{len(result.get('experiences', []))} 条"
        else:
            answer = result.get('answer', result.get('observation', '未返回结果。'))
        if isinstance(answer, str):
            try:
                parsed = json.loads(answer)
                answer = parsed.get('value', parsed) if isinstance(parsed, dict) else parsed
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(answer, (dict, list)):
            answer = json.dumps(answer, ensure_ascii=False, indent=2)
        if result.get('verified'):
            check = '答案已核验。'
        elif result.get('tool_execution_verified'):
            check = '工具已成功运行；答案本身未经过独立核对。'
        else:
            check = '结果尚未核验，请检查后到“经验与改进”反馈。'
        self.write(self.agent_output, f"办理结果：\n{answer}\n\n核验情况：{check}\n\n需要排查时，可点“查看详细记录”。")

    def memory_job(self, demo, disable=None):
        def work():
            from agent_lab import ExperienceStore, practice
            store = ExperienceStore()
            if disable:
                store.disable(disable)
            result = {'demonstration': practice(store)} if demo else {}
            return {**result, 'rules': store.rules(), 'experiences': store.recent()}
        self.submit('读取经验', work, self.show_agent)

    def training_tab(self, tabs):
        page = self.page(tabs, '学习过程演示')
        ttk.Label(page, text='从零教一个小型文字模型，用来观察学习过程；不会覆盖你的 Qwen 学习成果。').pack(anchor='w')
        row = ttk.Frame(page)
        row.pack(fill='x', pady=10)
        ttk.Label(row, text='练习次数').pack(side='left')
        self.steps = tk.StringVar(value='200')
        ttk.Entry(row, textvariable=self.steps, width=8).pack(side='left', padx=10)
        ttk.Label(row, text='每次调整幅度（建议不改）').pack(side='left')
        self.rate = tk.StringVar(value='0.003')
        ttk.Entry(row, textvariable=self.rate, width=10).pack(side='left', padx=10)
        self.button(row, '开始学习演示', self.train)
        self.button(row, '打开演示结果', lambda: os.startfile(ARTIFACTS / 'outputs'))
        self.train_output = self.text(page)
        self.write(self.train_output, '每次会创建一份独立演示，显示练习题和新题的错误程度，以及学习前后的续写变化。\n数值越低越好，但它不是回答正确率。Qwen 的学习结果可在“说明与记录”查看。')

    def train(self):
        steps, rate = self.steps.get(), self.rate.get()
        def display(event):
            row = event['rows'][-1]
            self.write(self.train_output, f"练习次数：{row['step']}\n练习题错误程度：{row['train_loss']:.4f}\n新题错误程度：{row['validation_loss']:.4f}（越低越好，不是正确率）\n保存位置：{event['run_dir']}\n\n学习前：\n{event['before']}\n\n学习后：\n{event['after']}")
        def work():
            from ailab import train_run
            for event in train_run(int(steps), float(rate)):
                self.events.put(('update', display, event))
            return event
        self.submit('正在进行学习演示，请勿关闭窗口', work, display)

    def finetune_tab(self, tabs):
        page = self.page(tabs, '训练我的 AI')
        ttk.Label(page, text='QWEN3–0.6B   /   教 AI 学习', foreground='#5165e8', font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w', pady=(0, 10))
        data_card = ttk.LabelFrame(page, text=' 01  数据与起点 ', padding=10)
        data_card.pack(fill='x', pady=(0, 12))
        self.dataset = tk.StringVar(value=str(DATA_ROOT))
        self.adapter = tk.StringVar(value='')
        for label, variable in [('练习题文件夹', self.dataset), ('以前的学习成果（可不选）', self.adapter)]:
            row = ttk.Frame(data_card)
            row.pack(fill='x', pady=5)
            ttk.Label(row, text=label, width=22).pack(side='left')
            ttk.Entry(row, textvariable=variable).pack(side='left', fill='x', expand=True, padx=8)
            def browse(var=variable):
                selected = filedialog.askdirectory(parent=self.root)
                if selected:
                    var.set(selected)
            self.button(row, '选择', browse)
        row = ttk.Frame(data_card)
        row.pack(fill='x', pady=5)
        self.button(row, '中文练习题', lambda: self.choose_dataset(False))
        self.button(row, '自动办事练习题', lambda: self.choose_dataset(True))
        self.button(row, '检查数据', self.check_data)
        self.button(row, '打开数据文件夹', lambda: os.startfile(self.dataset.get()))
        self.button(row, '自备题目说明', lambda: self.show_details('自备题目的文件格式',
            '先用内置练习题即可开始。自己整理题目时，文件夹需要以下 4 个 UTF-8 JSONL 文件：\n'
            'sft_train.jsonl：练习题\nsft_validation.jsonl：挑选学习成果用的新题\n'
            'sft_test.jsonl：最后检查用的新题\nsft_general_test.jsonl：检查基础能力的新题\n\n'
            '每行一题，格式：{"id":"唯一编号","prompt":"问题","response":"正确回答"}\n'
            '题号和问题不能重复；参考回答需要事先核实。单题最多 512 tokens，超长时会提示修改。'))
        parameter_card = ttk.LabelFrame(page, text=' 02  学习设置 ', padding=10)
        parameter_card.pack(fill='x', pady=(0, 10))
        row = ttk.Frame(parameter_card)
        row.pack(fill='x', pady=5)
        self.epochs = tk.StringVar(value='1')
        self.learning_rate = tk.StringVar(value='0.0001')
        ttk.Label(row, text='完整学习次数').pack(side='left')
        ttk.Spinbox(row, from_=1, to=10, textvariable=self.epochs, width=5).pack(side='left', padx=8)
        ttk.Label(row, text='每次调整幅度（建议不改）').pack(side='left')
        ttk.Entry(row, textvariable=self.learning_rate, width=12).pack(side='left', padx=8)
        self.agent_target = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text='保存为自动办事专用学习成果', variable=self.agent_target).pack(side='left')
        row = ttk.Frame(page)
        row.pack(fill='x', pady=8)
        self.button(row, '开始教 AI 学习', self.finetune)
        self.stop_button = ttk.Button(row, text='安全停止', command=self.stop_training, state='disabled')
        self.stop_button.pack(side='left', padx=(0, 10))
        self.button(row, '打开学习成果文件夹', lambda: os.startfile(ARTIFACTS / 'outputs'))
        self.button(row, '查看最近结果', self.latest_result)
        self.finetune_details = None
        self.button(row, '查看详细记录', lambda: self.show_details('学习详细记录', self.finetune_details))
        stats = tk.Frame(page, bg='#f3f5fa')
        stats.pack(fill='x', pady=(2, 8))
        self.metrics = {}
        for index, (key, label, value) in enumerate([('phase', '当前进度', '准备就绪'), ('loss', '练习题错误程度', '—'), ('validation', '新题错误程度', '—'), ('vram', '已用显卡内存', '—')]):
            card = tk.Frame(stats, bg='white', padx=14, pady=10, highlightbackground='#e5e9f2', highlightthickness=1)
            card.grid(row=0, column=index, sticky='nsew', padx=(0, 8 if index < 3 else 0))
            stats.columnconfigure(index, weight=1, uniform='metric')
            tk.Label(card, text=label, bg='white', fg='#7b879c', font=('Microsoft YaHei UI', 11)).pack(anchor='w')
            variable = tk.StringVar(value=value)
            tk.Label(card, textvariable=variable, bg='white', fg='#3347b0', font=('Microsoft YaHei UI', 16, 'bold')).pack(anchor='w', pady=(5, 0))
            self.metrics[key] = variable
        bottom = ttk.Frame(page)
        bottom.pack(fill='both', expand=True)
        log_panel = ttk.Frame(bottom)
        log_panel.pack(side='left', fill='both', expand=True)
        ttk.Label(log_panel, text='学习记录', foreground='#63728b').pack(anchor='w')
        self.finetune_output = self.text(log_panel, 5)
        self.finetune_output.configure(width=45, font=('Microsoft YaHei UI', 11))
        graph_panel = ttk.Frame(bottom, width=245)
        graph_panel.pack(side='right', fill='y', padx=(12, 0), before=log_panel)
        graph_panel.pack_propagate(False)
        ttk.Label(graph_panel, text='越低越好 · 不是正确率', foreground='#63728b').pack(anchor='w')
        self.loss_graph = tk.Canvas(graph_panel, bg='white', highlightthickness=0, width=245, height=160)
        self.loss_graph.pack(fill='both', expand=True, pady=8)
        self.loss_points = []
        self.loss_graph.bind('<Configure>', lambda event: self.draw_loss())
        self.write(self.finetune_output, '第一次使用：\n1. 选择“中文练习题”，点击“检查数据”。\n2. 保留默认设置，点击“开始教 AI 学习”。\n3. 完成后到“中文聊天”，用没教过的新问题检查效果。\n\n默认完整学习 1 次；学得太多可能只会背题。错误程度越低通常越好，但不等于回答正确率。\n\n想用自己的题目，请看“自备题目说明”。目前支持文字和工具使用，不包含听、说、看图、画图训练。原始模型会保留，每次学习成果单独保存。')
        self.stop_path = None

    def choose_dataset(self, agent):
        self.dataset.set(str(ARTIFACTS / 'datasets' / 'agent-sft-v2' if agent else DATA_ROOT))
        self.agent_target.set(agent)

    def draw_loss(self):
        canvas = self.loss_graph
        canvas.delete('all')
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 80 or height < 80:
            return
        if not self.loss_points:
            canvas.create_text(width/2, height/2, text='开始学习后\n显示真实错误程度', fill='#8d98aa', justify='center', font=('Microsoft YaHei UI', 10))
            return
        max_value = max(max(p[1:]) for p in self.loss_points) * 1.15 or 1
        left, top, right, bottom = 38, 22, width-15, height-35
        for frac in (0, 0.5, 1):
            y = bottom - frac*(bottom-top)
            canvas.create_line(left, y, right, y, fill='#e9edf5')
            canvas.create_text(left-5, y, text=f'{frac*max_value:.2f}', anchor='e', fill='#8793a6', font=('Segoe UI', 8))
        for series, color in ((1, '#5165e8'), (2, '#18a58d')):
            points = []
            for index, p in enumerate(self.loss_points):
                x = left + index*(right-left)/max(1, len(self.loss_points)-1)
                y = bottom - p[series]/max_value*(bottom-top)
                points.extend((x, y))
                canvas.create_oval(x-3, y-3, x+3, y+3, fill=color, outline=color)
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=2)
        canvas.create_text(left, height-14, anchor='w', text='● 练习题', fill='#5165e8', font=('Microsoft YaHei UI', 9))
        canvas.create_text(right, height-14, anchor='e', text='● 新题', fill='#18a58d', font=('Microsoft YaHei UI', 9))

    def check_data(self):
        directory = self.dataset.get()
        def work():
            from training_desktop import inspect_data
            return inspect_data(directory)
        def done(counts):
            self.finetune_details = {'data_directory': directory, 'file_counts': counts}
            lines = [f'• {name}：{count} 条' for name, count in counts.items()]
            self.write(self.finetune_output, '数据格式、空内容和重复项检查通过。\n这不代表教学内容已经人工审核。\n\n' + '\n'.join(lines))
        self.submit('检查数据', work, done)

    def latest_result(self):
        from paths import RUNS_ROOT
        files = sorted([*RUNS_ROOT.glob('qwen-lora-*/summary.json'), *RUNS_ROOT.glob('qwen-agent-lora-*/summary.json')], key=lambda p: p.stat().st_mtime_ns, reverse=True)
        if not files:
            self.write(self.finetune_output, '尚无完成的实验。')
            return
        summary = json.loads(files[0].read_text(encoding='utf-8'))
        summary['run_dir'] = str(files[0].parent)
        self.finetune_details = summary
        self.metrics['phase'].set('历史结果')
        self.metrics['validation'].set(f"{summary['best_validation_loss']:.4f}")
        self.metrics['vram'].set(f"{summary['peak_allocated_gb']:.2f} GiB")
        metrics = json.loads((files[0].parent / 'metrics.json').read_text(encoding='utf-8'))
        self.loss_points = [(row['epoch'], row['train_loss'], row['validation_loss']) for row in metrics['epochs']]
        if self.loss_points:
            self.metrics['loss'].set(f'{self.loss_points[-1][1]:.4f}')
        self.draw_loss()
        self.write(self.finetune_output,
                   f"最近一次学习已完成。\n\n最佳轮次：第 {summary['best_epoch']} 次完整学习\n"
                   f"新题错误程度：{summary['best_validation_loss']:.4f}（越低越好，不是正确率）\n"
                   f"完成调整：{summary['updates']} 次\n用时：{summary['training_seconds']:.1f} 秒\n"
                   f"最高显卡内存占用：{summary['peak_allocated_gb']:.2f} GiB\n保存位置：{summary['run_dir']}\n\n"
                   '完整数值可点“查看详细记录”。')

    def finetune(self):
        if self.busy:
            return
        from training_desktop import training_command, train_process
        from uuid import uuid4
        stop_path = ARTIFACTS / ('stop-' + uuid4().hex + '.flag')
        settings = (self.dataset.get(), self.epochs.get(), self.learning_rate.get(),
                    self.adapter.get().strip(), self.agent_target.get(), stop_path)
        self.stop_path = stop_path
        self.finetune_details = {'events': []}
        self.loss_points.clear()
        self.draw_loss()
        for key, value in self.metrics.items():
            value.set('准备加载' if key == 'phase' else '—')
        self.stop_button.configure(state='normal')
        self.write(self.finetune_output, '开始准备，正在加载原始 AI 并检查每条教学内容的长度…\n')
        def update(event):
            self.finetune_details['events'].append(event)
            if event.get('event') == 'desktop_finished':
                self.stop_button.configure(state='disabled')
                self.stop_path = None
                self.metrics['phase'].set('已结束')
                return
            kind = event.get('event')
            if kind == 'train':
                self.metrics['phase'].set(f"第 {event['epoch']} 轮")
                self.metrics['loss'].set(f"{event['mean_loss']:.4f}")
                self.metrics['vram'].set(f"{event['allocated_gb']:.2f} GiB")
                text = f"第{event['epoch']}次学习 · 已调整{event['update']}次 · 练习题错误程度 {event['mean_loss']:.4f} · 已用显卡内存 {event['allocated_gb']:.2f} GiB"
            elif kind == 'epoch_end':
                self.metrics['validation'].set(f"{event['validation_loss']:.4f}")
                self.metrics['loss'].set(f"{event['train_loss']:.4f}")
                self.loss_points.append((event['epoch'], event['train_loss'], event['validation_loss']))
                self.draw_loss()
                text = f"第{event['epoch']}次学习完成 · 新题错误程度 {event['validation_loss']:.4f}（越低越好，不是正确率） · 当前最佳是第{event['best_epoch']}次"
            elif kind == 'loaded':
                counts = '，'.join(f'{name} {count}条' for name, count in event['data_counts'].items())
                text = f"AI 加载完成 · 运行设备：{event['gpu']}\n保存位置：{event['run']}\n教学数据：{counts}"
            elif kind in ('baseline', 'evaluation'):
                self.metrics['phase'].set('学习前检查' if kind == 'baseline' else '学习后检查')
                text = ('学习前检查：' if kind == 'baseline' else '学习后检查：') + event['id']
            elif kind == 'training_experience':
                self.last_episode = event['episode_id']
                text = '本次学习结果已记入“经验与改进”；新的待确认建议仍需你审核。'
            elif kind == 'log':
                text = event['text']
            else:
                text = '收到一条技术记录，可在“查看详细记录”中查看。'
            self.write(self.finetune_output, text + '\n', True)
        def work():
            try:
                command = training_command(*settings)
                # Release any earlier inference models before the training subprocess uses CUDA.
                if 'legacy_qwen_chat' in sys.modules:
                    import gc
                    import torch
                    sys.modules['legacy_qwen_chat']._cached.clear()
                    gc.collect()
                    torch.cuda.empty_cache()
                return train_process(command, lambda event: self.events.put(('update', update, event)))
            except Exception as exc:
                try:
                    self.knowledge.record('本地LoRA训练失败：' + settings[0], str(exc), domain='training',
                        outcome='failure', verification='objective', evidence={'epochs': settings[1], 'learning_rate': settings[2]},
                        source='training_run', training_eligible=False)
                except Exception:
                    logging.exception('Unable to save training failure experience')
                raise
            finally:
                self.events.put(('update', update, {'event': 'desktop_finished'}))
                if stop_path.exists():
                    stop_path.unlink()
        def done(result):
            self.finetune_details['result'] = result
            self.metrics['phase'].set('已停止' if result.get('cancelled') else '学习完成')
            self.write(self.finetune_output, '\n' + ('已安全停止。未完成的学习不会作为可用成果加载。' if result.get('cancelled') else '学习和检查已完成。可到“中文聊天”或“自动办事”使用最新成果。') + '\n', True)
        self.submit('正在教 AI 学习，进度见记录', work, done)

    def stop_training(self):
        if self.stop_path:
            self.stop_path.touch()
            self.stop_button.configure(state='disabled')
            self.status.set('已请求停止，等待当前学习步骤或结果检查结束…')

    def docs_tab(self, tabs):
        page = self.page(tabs, '说明与记录')
        files = sorted((ROOT / 'docs').glob('*.md'))
        choices = ttk.Combobox(page, state='readonly', values=[p.stem for p in files], width=55)
        choices.pack(anchor='w')
        output = self.text(page)
        def show(event=None):
            self.write(output, files[choices.current()].read_text(encoding='utf-8'))
            output.yview_moveto(0)
        choices.bind('<<ComboboxSelected>>', show)
        if files:
            choices.current(len(files)-1)
            show()

    def callback_error(self, kind, value, tb):
        logging.error('UI callback failed', exc_info=(kind, value, tb))
        messagebox.showerror('界面错误', f'{value}\n日志：{LOG}', parent=self.root)

    def close(self):
        if self.busy:
            messagebox.showinfo('任务进行中', '请等待当前生成或训练完成，再关闭窗口。', parent=self.root)
            return
        self.root.destroy()


def main():
    if not Path(ARTIFACTS.anchor).exists():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('训练磁盘未连接', f'请连接训练磁盘：{ARTIFACTS}\n程序不会改用C盘保存数据。', parent=root)
        root.destroy()
        return
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    # pythonw has no console streams; some ML libraries still write warnings.
    with LOG.open('a', encoding='utf-8', buffering=1) as stream:
        sys.stdout = sys.stderr = stream
        logging.basicConfig(stream=stream, level=logging.INFO)
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('FreeAI.Desktop.1')
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
        root = tk.Tk()
        app = DesktopApp(root)
        if '--experience' in sys.argv:
            root.after(500, app.open_epistemology)
        root.mainloop()


if __name__ == '__main__':
    main()
