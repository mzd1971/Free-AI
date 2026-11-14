"""Free AI 2: native desktop, lightweight shell, explicitly downloaded abilities."""
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog, simpledialog
from tkinter.scrolledtext import ScrolledText

from app_state import Sessions, CONTEXTS
from paths import ARTIFACTS, ROOT
from legacy_desktop import DesktopApp as LegacyApp

LOG = ARTIFACTS / 'desktop.log'
BG, INK, MUTED, ACCENT = '#f5f7fb', '#1c2c43', '#64748b', '#425fe8'

def size_text(value):
    return f'{value / 1_000_000_000:.2f} GB' if value >= 1_000_000_000 else f'{value / 1_000_000:.1f} MB'


class DesktopApp(LegacyApp):
    def __init__(self, root):
        from capability_manager import PackageManager
        from epistemology import KnowledgeStore
        self.root, self.events = root, queue.Queue()
        self.busy = False
        self.buttons, self.nav_buttons = [], []
        self.last_episode = None
        self.knowledge = KnowledgeStore()
        self.sessions = Sessions(ARTIFACTS)
        self.manager = PackageManager()
        self.download_cancel = threading.Event()
        self.generation_cancel = threading.Event()
        self.downloading = None
        self.image_path = None
        self.last_answer = ''
        self.pending_chat = None
        self.training_process = None
        self.session_id = None
        self.history = []
        self.context = tk.StringVar(value=self.sessions.get('context', next(iter(CONTEXTS))))
        if self.context.get() not in CONTEXTS:
            self.context.set(next(iter(CONTEXTS)))
        self.chat_hint = tk.StringVar(value='回答仅在本机生成；重要内容请核实。')
        root.title('Free AI')
        root.tk.call('tk', 'scaling', 1.5)
        root.geometry(f'{min(1400, root.winfo_screenwidth()-70)}x{min(940, root.winfo_screenheight()-90)}+25+25')
        root.minsize(1040, 720)
        root.configure(bg=BG)
        if (ROOT / 'assets/free-ai.ico').exists():
            root.iconbitmap(str(ROOT / 'assets/free-ai.ico'))
        root.option_add('*Font', ('Microsoft YaHei UI', 12))
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 12), background=BG, foreground=INK)
        style.configure('TButton', padding=(13, 9), borderwidth=0, background='#e8edf6')
        style.map('TButton', background=[('active', '#dbe4fa')])
        style.configure('Primary.TButton', foreground='white', background=ACCENT)
        style.map('Primary.TButton', background=[('active', '#344ed0'), ('disabled', '#a5b2ed')])
        style.configure('TEntry', fieldbackground='white', padding=7)
        style.configure('TCombobox', fieldbackground='white', padding=5)
        style.configure('TNotebook', borderwidth=0)
        style.layout('TNotebook.Tab', [])
        style.configure('Treeview', rowheight=35, font=('Microsoft YaHei UI', 12))
        self.sidebar = tk.Frame(root, bg='#18263d', width=215)
        self.sidebar.pack(side='left', fill='y')
        self.sidebar.pack_propagate(False)
        tk.Label(self.sidebar, text='free ai', bg='#18263d', fg='white', font=('Segoe UI', 27, 'bold')).pack(anchor='w', padx=22, pady=(24, 2))
        tk.Label(self.sidebar, text='轻装开始，按需成长', bg='#18263d', fg='#aabbcf', font=('Microsoft YaHei UI', 11)).pack(anchor='w', padx=22, pady=(0, 22))
        self.nav_host = tk.Frame(self.sidebar, bg='#18263d')
        self.nav_host.pack(fill='x')
        tk.Frame(self.sidebar, bg='#334158', height=1).pack(fill='x', padx=20, pady=18)
        tk.Button(self.sidebar, text='＋ 新的对话', command=self.new_conversation, bg='#293c59', fg='white', bd=0, pady=10, font=('Microsoft YaHei UI', 12)).pack(fill='x', padx=16)
        tk.Label(self.sidebar, text='最近的对话', bg='#18263d', fg='#aabbcf', font=('Microsoft YaHei UI', 10)).pack(anchor='w', padx=22, pady=(20, 7))
        self.recent_list = tk.Listbox(self.sidebar, bg='#18263d', fg='#d1deef', selectbackground='#344c70', bd=0,
            highlightthickness=0, exportselection=False, font=('Microsoft YaHei UI', 11), activestyle='none')
        self.recent_list.pack(fill='both', expand=True, padx=16, pady=4)
        self.recent_list.bind('<<ListboxSelect>>', self.choose_conversation)
        tk.Label(self.sidebar, text='2.0  ·  数据留在本机', bg='#18263d', fg='#93a6c1', font=('Microsoft YaHei UI', 10)).pack(anchor='w', padx=22, pady=18)
        workspace = ttk.Frame(root)
        workspace.pack(side='left', fill='both', expand=True)
        header = ttk.Frame(workspace, padding=(26, 22, 26, 12))
        header.pack(fill='x')
        self.heading = tk.StringVar(value='今天想完成什么？')
        ttk.Label(header, textvariable=self.heading, font=('Microsoft YaHei UI', 23, 'bold')).pack(anchor='w')
        self.subtitle = tk.StringVar(value='聊想法、改文字、看资料。从一件小事开始。')
        ttk.Label(header, textvariable=self.subtitle, foreground=MUTED).pack(anchor='w', pady=(7, 0))
        footer = ttk.Frame(workspace, padding=(24, 8))
        footer.pack(side='bottom', fill='x')
        self.status = tk.StringVar(value='就绪')
        ttk.Label(footer, textvariable=self.status, foreground=MUTED, wraplength=850).pack(side='left')
        self.progress = ttk.Progressbar(footer, mode='indeterminate', length=95)
        self.progress.pack(side='right')
        self.stop_task_button = ttk.Button(footer, text='停止当前任务', command=self.stop_generation, state='disabled')
        self.stop_task_button.pack(side='right', padx=10)
        self.tabs = ttk.Notebook(workspace)
        self.tabs.pack(fill='both', expand=True, padx=16)
        self.chat_tab(self.tabs)
        self.capabilities_tab(self.tabs)
        from epistemology_ui import EpistemologyPanel
        self.epistemology_index = len(self.nav_buttons)
        self.epistemology = EpistemologyPanel(self, self.page(self.tabs, '经验与改进'))
        self.learning_tab(self.tabs)
        self.agent_tab(self.tabs)
        self.settings_tab(self.tabs)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = self.callback_error
        root.after(100, self.poll)
        self.refresh_recent()
        if self.recent_rows:
            self.load_conversation(self.recent_rows[0][0])
        else:
            self.new_conversation()
        self.refresh_capabilities()
        self.select_page(0)

    def page(self, tabs, title):
        frame = ttk.Frame(tabs, padding=12)
        tabs.add(frame, text=title)
        index = len(self.nav_buttons)
        button = tk.Button(self.nav_host, text=title, command=lambda: self.select_page(index), anchor='w',
            bg='#18263d', fg='#d1deef', activebackground='#344c70', activeforeground='white', relief='flat',
            padx=20, pady=11, cursor='hand2', font=('Microsoft YaHei UI', 12))
        button.pack(fill='x', padx=12, pady=2)
        self.nav_buttons.append(button)
        return frame

    def select_page(self, index):
        self.tabs.select(index)
        for pos, button in enumerate(self.nav_buttons):
            button.configure(bg=ACCENT if pos == index else '#18263d', fg='white' if pos == index else '#d1deef')
        titles = [('今天想完成什么？', '聊想法、改文字、看资料。从一件小事开始。'),
                  ('让 AI 多一点本领', '只下载需要的能力。所有大小都会提前告诉你。'),
                  ('把每次经历变成进步', '记录事实、核实经验，下次再用。'),
                  ('训练与进步', '日常使用和模型训练分开，避免每个人都安装庞大的训练工具。'),
                  ('一步一步完成任务', '看得到执行过程，也要检查实际结果。'),
                  ('按你的习惯使用', '管理本机空间、对话长度和使用说明。')]
        self.heading.set(titles[index][0])
        self.subtitle.set(titles[index][1])

    def chat_tab(self, tabs):
        page = self.page(tabs, '聊天')
        self.welcome = tk.Frame(page, bg='#eaf0ff', padx=18, pady=14)
        self.welcome.pack(fill='x', pady=(0, 10))
        self.ready_label = tk.StringVar(value='首次使用，先准备文字能力')
        tk.Label(self.welcome, textvariable=self.ready_label, bg='#eaf0ff', fg='#263e8a', font=('Microsoft YaHei UI', 13, 'bold')).pack(side='left')
        self.prepare_button = ttk.Button(self.welcome, text='下载文字能力', style='Primary.TButton', command=lambda: self.start_download('text-default'))
        self.prepare_button.pack(side='right')
        row = ttk.Frame(page)
        row.pack(fill='x', pady=4)
        ttk.Label(row, text='本次能记住的内容', foreground=MUTED).pack(side='left')
        self.context_box = ttk.Combobox(row, textvariable=self.context, values=list(CONTEXTS), state='readonly', width=20)
        self.context_box.pack(side='left', padx=10)
        self.context_box.bind('<<ComboboxSelected>>', self.change_context)
        ttk.Label(row, text='文字模式 · 默认不加载看图和语音', foreground=MUTED, font=('Microsoft YaHei UI', 10)).pack(side='right')
        self.transcript = self.text(page)
        self.transcript.tag_configure('user', foreground='#3955d7', font=('Microsoft YaHei UI', 12, 'bold'))
        self.input = self.text(page, 3, True)
        self.input.bind('<Control-Return>', lambda e: (self.send_chat(), 'break')[-1])
        row = ttk.Frame(page)
        row.pack(fill='x')
        self.button(row, '发送', self.send_chat)
        self.stop_generation_button = ttk.Button(row, text='停止回答', command=self.stop_generation, state='disabled')
        self.stop_generation_button.pack(side='left', padx=(0, 8))
        self.button(row, '添加资料', self.attach_text)
        self.button(row, '添加图片', self.attach_image)
        self.button(row, '语音转文字', self.attach_audio)
        self.button(row, '读给我听', self.read_aloud)
        more = ttk.Frame(page)
        more.pack(fill='x', pady=(8, 0))
        self.button(more, '纠正这次回答', self.open_epistemology)
        self.button(more, '复制回答', self.copy_answer)
        self.button(more, '保存为文件', self.export_answer)
        self.button(more, '画一张图', self.draw_image)
        ttk.Label(page, textvariable=self.chat_hint, foreground=MUTED, wraplength=950).pack(anchor='w', pady=8)

    def refresh_recent(self):
        self.recent_rows = self.sessions.recent()
        self.recent_list.delete(0, 'end')
        for _, title in self.recent_rows:
            self.recent_list.insert('end', title)

    def new_conversation(self):
        if self.busy:
            self.status.set('请先停止当前回答，再创建新对话。')
            return
        self.load_conversation(self.sessions.create())
        self.refresh_recent()
        if hasattr(self, 'tabs'):
            self.select_page(0)

    def choose_conversation(self, event=None):
        if self.busy:
            return
        selected = self.recent_list.curselection()
        if selected:
            self.load_conversation(self.recent_rows[selected[0]][0])
            self.select_page(0)

    def load_conversation(self, identity):
        self.session_id = identity
        self.history = self.sessions.messages(identity)
        self.last_episode = None
        self.last_answer = next((m['content'] for m in reversed(self.history) if m['role'] == 'assistant'), '')
        self.write(self.transcript, ''.join(('你' if m['role'] == 'user' else 'Free AI') + '：\n' + m['content'] + '\n\n' for m in self.history)
                   or '你好，我是 Free AI。\n\n可以让我帮你整理想法、改写文字、解释代码。\n首次使用请先下载文字能力，之后可以离线聊天。\n')
        self.input.delete('1.0', 'end')
        self.image_path = None

    def send_chat(self):
        if self.busy:
            return
        if self.training_process is not None and self.training_process.poll() is None:
            self.status.set('请先关闭训练工作台，再开始聊天，避免同时占用显存。')
            return
        message = self.input.get('1.0', 'end').strip()
        if not message:
            return
        if not self.manager.ready('text-default'):
            self.select_page(1)
            self.status.set('先下载基础文字能力。你的输入仍保留在聊天页。')
            return
        history, identity, picture, context = list(self.history), self.session_id, self.image_path, CONTEXTS[self.context.get()]
        self.generation_cancel.clear()
        self.streamed = ''
        self.pending_chat = identity
        self.write(self.transcript, f'你：\n{message}\n\nFree AI：\n', True)
        self.sessions.append(identity, 'user', message)
        self.history.append({'role': 'user', 'content': message})
        self.input.delete('1.0', 'end')
        self.image_path = None
        self.refresh_recent()
        self.stop_generation_button.configure(state='normal')
        self.chat_hint.set('正在准备模型，首次加载可能需要一些时间…')
        def token(value):
            self.streamed += value
            self.write(self.transcript, value, True)
            self.status.set('正在回答…可以随时停止')
        def work():
            from qwen_chat import chat
            return chat(message, history, memory_store=self.knowledge, return_details=True, context=context,
                        image_path=picture, cancel_event=self.generation_cancel,
                        on_token=lambda text: self.events.put(('update', token, text)))
        def done(result):
            self.last_episode = result.get('episode_id')
            self.last_answer = result['answer']
            if not self.streamed:
                self.write(self.transcript, self.last_answer, True)
            self.write(self.transcript, '\n\n', True)
            self.sessions.append(identity, 'assistant', self.last_answer)
            self.history.append({'role': 'assistant', 'content': self.last_answer})
            self.pending_chat = None
            self.stop_generation_button.configure(state='disabled')
            from qwen_chat import runtime
            notice = getattr(runtime(), 'last_context_notice', '')
            self.chat_hint.set((notice + ' · ' if notice else '') + '回答已保存。发现错误时，可点“纠正这次回答”。')
        self.submit('正在加载并回答', work, done, cancellable=True)

    def save_interrupted_answer(self, reason):
        if self.pending_chat is None:
            return
        self.last_answer = (self.streamed.rstrip() + '\n\n' if self.streamed else '') + '[' + reason + ']'
        self.sessions.append(self.pending_chat, 'assistant', self.last_answer)
        self.history.append({'role': 'assistant', 'content': self.last_answer})
        self.pending_chat = None

    def stop_generation(self):
        self.generation_cancel.set()
        self.status.set('正在停止当前任务，请稍候…')

    def change_context(self, event=None):
        if self.busy:
            self.context.set(self.sessions.get('context', next(iter(CONTEXTS))))
            self.status.set('当前任务结束后才能调整对话长度。')
            return
        self.sessions.set('context', self.context.get())
        self.chat_hint.set('对话长度已调整，无需重复下载模型。下次回答时生效；较长档位会占用更多内存。')

    def attach_text(self):
        path = filedialog.askopenfilename(parent=self.root, title='添加文字资料', filetypes=[('文字资料', '*.txt *.md *.csv *.json *.py'), ('所有文件', '*.*')])
        if path:
            try:
                source = Path(path)
                if source.stat().st_size > 2_000_000:
                    raise ValueError('这份资料超过 2 MB。请先选择相关段落，避免一次放入过多内容。')
                text = source.read_text(encoding='utf-8-sig')
                self.input.insert('end', f'\n\n以下是资料《{source.name}》的内容，仅作为参考：\n{text}\n\n我的要求：')
                self.chat_hint.set('资料只在本机读取。若超过当前长度，会提示你调整，不会静默截断本次资料。')
            except Exception as exc:
                messagebox.showerror('无法添加资料', str(exc), parent=self.root)

    def attach_image(self):
        if not self.manager.ready('vision'):
            self.select_page(1)
            self.status.set('图片功能需要先下载“看懂图片”。文字能力仍可独立使用。')
            return
        path = filedialog.askopenfilename(parent=self.root, title='选择图片', filetypes=[('图片', '*.png *.jpg *.jpeg *.bmp')])
        if path:
            self.image_path = path
            self.chat_hint.set('已添加图片：' + Path(path).name + '。输入问题后发送。')

    def attach_audio(self):
        if not self.manager.ready('speech-model'):
            self.select_page(1)
            self.status.set('请先下载“听懂语音”，再选择 WAV 录音。')
            return
        path = filedialog.askopenfilename(parent=self.root, title='选择 WAV 录音', filetypes=[('WAV 录音', '*.wav')])
        if path:
            from qwen_chat import runtime
            self.submit('正在把录音转成文字', lambda: runtime().transcribe(path, cancel_event=self.generation_cancel),
                        lambda text: self.input.insert('end', text), cancellable=True)

    def copy_answer(self):
        if self.last_answer:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.last_answer)
            self.status.set('回答已复制')

    def draw_image(self):
        if not self.manager.ready('image-model'):
            self.select_page(1)
            self.packages.selection_set('image-model')
            self.package_selected()
            self.status.set('请先下载可选画图能力。它不会随文字模型一起下载。')
            return
        prompt = simpledialog.askstring('画一张图', '描述想画的内容（英文描述更合适）。\n此版本使用 CPU，生成可能较慢。', parent=self.root)
        if prompt:
            from qwen_chat import runtime
            self.submit('正在本机画图', lambda: runtime().generate_image(prompt, cancel_event=self.generation_cancel), self.image_finished, cancellable=True)

    def image_finished(self, path):
        self.status.set('图片已保存：' + str(path))
        self.write(self.transcript, '图片已生成并保存：' + str(path) + '\n\n', True)
        os.startfile(path)

    def export_answer(self):
        if not self.last_answer:
            return
        path = filedialog.asksaveasfilename(parent=self.root, title='保存回答', defaultextension='.md', initialdir=ARTIFACTS, filetypes=[('Markdown 文档', '*.md'), ('文本', '*.txt')])
        if path:
            Path(path).write_text(self.last_answer, encoding='utf-8')
            self.status.set('已保存：' + path)

    def read_aloud(self):
        if not self.last_answer:
            return
        def work():
            script = '[Console]::InputEncoding = New-Object System.Text.UTF8Encoding; Add-Type -AssemblyName System.Speech; $voice = New-Object System.Speech.Synthesis.SpeechSynthesizer; $voice.Speak([Console]::In.ReadToEnd()); $voice.Dispose()'
            # User text is stdin, never PowerShell source or a command argument.
            with subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], stdin=subprocess.PIPE,
                encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as proc:
                deadline = time.monotonic() + 300
                spoken = self.last_answer
                while True:
                    if self.generation_cancel.is_set() or time.monotonic() > deadline:
                        proc.terminate()
                        proc.wait(timeout=5)
                        raise RuntimeError('朗读已停止。')
                    try:
                        proc.communicate(input=spoken, timeout=.2)
                        if proc.returncode:
                            raise RuntimeError('朗读未成功，请检查 Windows 是否安装了相应语音。')
                        break
                    except subprocess.TimeoutExpired:
                        spoken = None
        self.submit('正在朗读（使用 Windows 已安装语音）', work, lambda _: self.status.set('朗读结束'), cancellable=True)

    def capabilities_tab(self, tabs):
        page = self.page(tabs, '能力中心')
        self.space = tk.StringVar()
        ttk.Label(page, textvariable=self.space, foreground=MUTED, wraplength=900).pack(anchor='w', pady=(0, 12))
        columns = ('name', 'size', 'state')
        table = ttk.Frame(page)
        table.pack(fill='x')
        self.packages = ttk.Treeview(table, columns=columns, show='headings', height=6, selectmode='browse')
        for name, title, width in [('name', '想让 AI 做什么', 260), ('size', '下载大小', 130), ('state', '当前状态', 180)]:
            self.packages.heading(name, text=title)
            self.packages.column(name, width=width, minwidth=90)
        self.packages.pack(side='left', fill='x', expand=True)
        scrollbar = ttk.Scrollbar(table, command=self.packages.yview)
        scrollbar.pack(side='right', fill='y')
        self.packages.configure(yscrollcommand=scrollbar.set)
        self.packages.bind('<<TreeviewSelect>>', self.package_selected)
        self.package_description = tk.StringVar(value='选择一种能力，查看用途和需要的空间。')
        ttk.Label(page, textvariable=self.package_description, foreground=INK, wraplength=900).pack(anchor='w', pady=14)
        actions = ttk.Frame(page)
        actions.pack(fill='x')
        self.download_button = ttk.Button(actions, text='下载并准备', style='Primary.TButton', command=self.download_selected)
        self.download_button.pack(side='left', padx=(0, 10))
        self.pause_button = ttk.Button(actions, text='暂停下载', command=self.download_cancel.set, state='disabled')
        self.pause_button.pack(side='left', padx=(0, 10))
        ttk.Button(actions, text='检查完整性', command=self.verify_selected).pack(side='left', padx=(0, 10))
        ttk.Button(actions, text='移除所选能力', command=self.remove_selected).pack(side='left')
        ttk.Button(actions, text='来源与许可', command=self.package_source).pack(side='left', padx=10)
        self.download_status = tk.StringVar(value='所有下载均由你点击开始。关闭应用后，未完成的文件会保留以便继续。')
        ttk.Label(page, textvariable=self.download_status, foreground=MUTED, wraplength=900).pack(anchor='w', pady=(18, 8))
        self.download_progress = ttk.Progressbar(page, mode='determinate', maximum=100)
        self.download_progress.pack(fill='x')
        ttk.Label(page, text='长对话：调整聊天长度即可，无需下载。朗读：使用 Windows 已安装的语音。', foreground=MUTED, wraplength=900).pack(anchor='w', pady=(12, 0))
        self.download_log = ScrolledText(page, height=3, wrap='word', font=('Microsoft YaHei UI', 11), relief='flat', bg='white', padx=12, pady=10)
        self.download_log.pack(fill='both', expand=True, pady=12)
        self.download_log.configure(state='disabled')

    def refresh_capabilities(self):
        selected = self.packages.selection()
        self.packages.delete(*self.packages.get_children())
        self.package_rows = {row['id']: row for row in self.manager.list_packages()}
        for row in self.package_rows.values():
            if row['id'] in ('engine-cpu', 'speech-engine', 'image-engine'):
                continue  # Shared engines are managed by their parent capabilities.
            state = {'installed': '已就绪', 'paused': '已暂停 · 可继续', 'repair': '需要修复', 'downloading': '下载中', 'not_installed': '待下载'}.get(row['status'], row['status'])
            self.packages.insert('', 'end', iid=row['id'], values=(row['title'], size_text(row['size']), state))
        if selected and selected[0] in self.package_rows:
            self.packages.selection_set(selected[0])
        elif 'text-default' in self.package_rows:
            self.packages.selection_set('text-default')
        self.package_selected()
        free = shutil.disk_usage(ARTIFACTS).free
        self.space.set(f'保存位置：{ARTIFACTS}\n剩余空间：{size_text(free)} · 能力文件与聊天、经验分开保存')
        ready = self.manager.ready('text-default')
        self.ready_label.set('文字能力已准备好 · Qwen3.5 4B' if ready else '首次使用：文字模型约 2.74 GB，下载后可离线使用')
        self.prepare_button.configure(text='管理能力' if ready else '下载文字能力', command=(lambda: self.select_page(1)) if ready else (lambda: self.start_download('text-default')))

    def package_source(self):
        selected = self.packages.selection()
        if selected:
            row = self.package_rows[selected[0]]
            text = f"{row['title']}\n\n来源：{row['source']}\n许可：{row['license']}\n固定版本：{row['version']}\n\n"
            text += '下载后自动检查 SHA-256，文件不完整时不会启用。\n\n'
            for asset in row['assets']:
                text += asset['filename'] + '\n' + asset['url'] + '\n\n'
            self.show_details('来源与许可', text)

    def package_selected(self, event=None):
        selected = self.packages.selection()
        if not selected:
            return
        row = self.package_rows[selected[0]]
        dependencies = row.get('dependencies', [])
        extra = sum(self.package_rows[d]['size'] for d in dependencies if d in self.package_rows and not self.package_rows[d]['installed'])
        needed = self.manager.download_size(row['id'])
        self.package_description.set(row['description'] + f"\n本次合计约 {size_text(needed)}（含缺少的共享组件）。许可：{row['license']}。")
        self.download_button.configure(text='已准备好' if row['installed'] else '下载 / 继续准备', state='disabled' if row['installed'] or self.downloading else 'normal')

    def download_selected(self):
        selected = self.packages.selection()
        if selected:
            self.start_download(selected[0])

    def start_download(self, identity):
        self.select_page(1)
        if self.downloading:
            self.status.set('已有下载正在进行，完成或暂停后可选择其他能力。')
            return
        self.downloading = identity
        self.download_cancel.clear()
        self.pause_button.configure(state='normal')
        self.download_button.configure(state='disabled')
        self.download_status.set('正在检查文件与剩余空间…')
        self.packages.selection_set(identity)
        def work():
            try:
                self.manager.install(identity, progress=lambda e: self.events.put(('update', self.download_event, e)), cancel_event=self.download_cancel)
                message = '已完成下载与完整性检查，可以使用。'
            except Exception as exc:
                message = '已暂停，点击下载可继续。' if self.download_cancel.is_set() else '下载未完成：' + str(exc)
            self.events.put(('update', self.download_finished, message))
        threading.Thread(target=work, daemon=True).start()

    def download_event(self, event):
        total, done = event.get('total', 0), event.get('downloaded', 0)
        self.download_progress['value'] = min(100, done * 100 / total) if total else 0
        phase = {'downloading': '正在下载', 'download': '正在下载', 'verifying': '检查完整性', 'verify': '检查完整性', 'extracting': '正在安装', 'installed': '已准备好'}.get(event.get('phase'), event.get('phase', '准备中'))
        text = event.get('message') or phase
        if total:
            text += f" · {size_text(done)} / {size_text(total)}"
        if event.get('speed', 0):
            text += f" · {size_text(event['speed'])}/秒"
        self.download_status.set(text)
        self.status.set('能力正在后台准备，窗口可继续操作。')

    def download_finished(self, message):
        self.downloading = None
        self.pause_button.configure(state='disabled')
        self.download_status.set(message)
        self.write(self.download_log, message + '\n', True)
        self.refresh_capabilities()
        self.status.set(message)

    def verify_selected(self):
        selected = self.packages.selection()
        if selected and not self.downloading:
            identity = selected[0]
            self.submit('正在检查已下载文件', lambda: self.manager.verify(identity), lambda result: self.status.set('完整性检查完成：' + str(result)))

    def remove_selected(self):
        selected = self.packages.selection()
        if not selected or self.downloading or self.busy:
            return
        identity = selected[0]
        row = self.package_rows[identity]
        if not messagebox.askyesno('移除能力文件', f"移除“{row['title']}”？\n该能力以后需要重新下载。聊天、经验和学习成果会保留。", parent=self.root):
            return
        try:
            from qwen_chat import shutdown
            shutdown()
            self.manager.uninstall(identity)
            self.refresh_capabilities()
        except Exception as exc:
            messagebox.showerror('无法移除', str(exc), parent=self.root)

    def learning_tab(self, tabs):
        page = self.page(tabs, '训练与进步')
        ttk.Label(page, text='先记录和核实经验，再决定是否训练模型', font=('Microsoft YaHei UI', 17, 'bold')).pack(anchor='w', pady=(2, 12))
        ttk.Label(page, text='日常聊天默认使用新的文字模型。原来 0.6B 的学习成果保留，\n不会自动套用到不同模型上。', foreground=MUTED).pack(anchor='w', pady=8)
        ttk.Button(page, text='打开入门训练工作台', style='Primary.TButton', command=self.open_training).pack(anchor='w', pady=12)
        ttk.Button(page, text='准备训练工具（按需下载）', command=self.prepare_training).pack(anchor='w', pady=5)
        ttk.Button(page, text='查看经验与改进', command=self.open_epistemology).pack(anchor='w', pady=12)
        self.training_notice = tk.StringVar(value='训练工具单独准备，不计入轻量安装包。下载量较大，需要联网；已有完整环境可直接使用。')
        ttk.Label(page, textvariable=self.training_notice, foreground=MUTED, wraplength=900).pack(anchor='w', pady=14)
        self.training_log = self.text(page, height=8)
        self.write(self.training_log, '推荐顺序\n\n1. 用没有教过的新问题检查回答。\n2. 将错误和正确做法记录到经验库。\n3. 核实资料，积累教学题目。\n4. 在训练工作台进行入门实验，并比较训练前后结果。\n\n默认聊天模型的高级微调不包含在本次轻量版中；入门训练沿用原来的独立小模型。')

    def prepare_training(self):
        from training_extension import install_training
        self.submit('正在准备训练工具，可能需要较长时间', lambda: install_training(
            lambda text: self.events.put(('update', lambda s: (self.training_notice.set(s), self.write(self.training_log, s + '\n', True)), text)),
            cancel_event=self.generation_cancel),
            lambda _: self.training_notice.set('训练工具已准备好。请在能力中心准备入门训练模型。'), cancellable=True)

    def open_training(self):
        if self.busy:
            self.status.set('请先停止当前任务，再打开训练工作台。')
            return
        if self.training_process is not None and self.training_process.poll() is None:
            self.status.set('训练工作台已打开，请切换到该窗口。')
            return
        from training_extension import training_python, prepare_starter_data
        interpreter = training_python()
        if not interpreter:
            messagebox.showinfo('需要准备训练工具', '先点击“准备训练工具”。它们不会随普通安装包一起下载。', parent=self.root)
            return
        if not (ROOT / 'models/Qwen3-0.6B/model.safetensors').is_file() and not self.manager.ready('legacy-training-model'):
            self.select_page(1)
            self.packages.selection_set('legacy-training-model')
            self.package_selected()
            self.status.set('请先准备独立的入门训练模型；它不会替换默认聊天模型。')
            return
        prepare_starter_data()
        from qwen_chat import shutdown
        shutdown()
        self.training_process = subprocess.Popen([str(interpreter), '-E', '-s', '-X', 'utf8', str(ROOT / 'legacy_desktop.py')], cwd=ROOT,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.status.set('已打开独立训练工作台。训练期间请避免同时生成回答，以免显存不足。')

    def agent_tab(self, tabs):
        page = self.page(tabs, '任务实验')
        ttk.Label(page, text='可核验工具：计算、读取最近训练结果。其他任务先给出建议，不会假装操作过文件。', foreground=MUTED, wraplength=900).pack(anchor='w')
        self.task = self.text(page, 3, True)
        self.task.insert('1.0', '计算 237*19-84。')
        row = ttk.Frame(page)
        row.pack(fill='x', pady=8)
        self.memory = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text='开始前参考已核实的经验', variable=self.memory).pack(side='left')
        row = ttk.Frame(page)
        row.pack(fill='x', pady=8)
        self.button(row, '开始办理', self.run_agent)
        self.button(row, '纠正与查看经验', self.open_epistemology)
        self.button(row, '计算工具示范', lambda: self.memory_job(True))
        self.agent_details = None
        self.button(row, '查看执行记录', lambda: self.show_details('实际执行记录', self.agent_details))
        self.agent_output = self.text(page)

    def run_agent(self):
        task, memory, context = self.task.get('1.0', 'end').strip(), self.memory.get(), CONTEXTS[self.context.get()]
        def work():
            from agent_lab import run_agent
            from qwen_chat import chat
            return run_agent(task, memory, planner=lambda p: chat(p, use_memory=False, context=context, cancel_event=self.generation_cancel), knowledge=self.knowledge)
        self.submit('正在判断任务并执行可用工具', work, self.show_agent, cancellable=True)

    def settings_tab(self, tabs):
        page = self.page(tabs, '设置与帮助')
        ttk.Label(page, text='文件放在哪里', font=('Microsoft YaHei UI', 16, 'bold')).pack(anchor='w', pady=10)
        ttk.Label(page, text=str(ARTIFACTS), wraplength=900, foreground=MUTED).pack(anchor='w', pady=8)
        ttk.Button(page, text='打开数据文件夹', command=lambda: os.startfile(ARTIFACTS)).pack(anchor='w', pady=8)
        ttk.Button(page, text='选择新的保存位置', command=self.choose_storage).pack(anchor='w', pady=8)
        ttk.Label(page, text='安装包只带界面和必要运行文件；首次使用下载文字能力。\n模型下载完成后，文字聊天可离线运行。\n长对话不重复下载模型，但会使用更多内存。\n看图与听音只有在添加对应文件时才运行。', foreground=MUTED, wraplength=900).pack(anchor='w', pady=20)
        ttk.Button(page, text='释放模型占用的内存', command=self.release_model).pack(anchor='w', pady=8)
        ttk.Button(page, text='打开使用说明', command=lambda: os.startfile(ROOT / '使用说明.md')).pack(anchor='w', pady=8)

    def choose_storage(self):
        selected = filedialog.askdirectory(parent=self.root, title='选择非 C 盘数据文件夹')
        if not selected:
            return
        destination = Path(selected).resolve()
        if destination.drive.upper() == 'C:':
            messagebox.showinfo('请选择其他磁盘', '按你的设置，模型和学习数据不保存在 C 盘。', parent=self.root)
            return
        if not messagebox.askyesno('下次启动使用新位置', '已有数据不会自动移动。\n若要继续使用旧数据，请先将数据文件夹完整复制到所选位置。\n下次启动后，下载和记录将保存到：\n' + str(destination), parent=self.root):
            return
        temporary = ROOT / 'storage.json.tmp'
        temporary.write_text(json.dumps({'data_root': str(destination)}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(ROOT / 'storage.json')
        self.status.set('新位置已保存，重启后生效；当前数据仍在原位置。')

    def release_model(self):
        if not self.busy:
            from qwen_chat import shutdown
            shutdown()
            self.status.set('模型已卸载，下次回答时会重新加载。')

    def submit(self, label, work, done, cancellable=False):
        if self.busy:
            return
        self.generation_cancel.clear()
        self.stop_task_button.configure(state='normal' if cancellable else 'disabled')
        self.stop_generation_button.configure(state='normal' if cancellable else 'disabled')
        super().submit(label, work, done)

    def poll(self):
        try:
            while True:
                kind, handler, payload = self.events.get_nowait()
                if kind == 'update':
                    handler(payload)
                    continue
                self.busy = False
                self.progress.stop()
                self.stop_generation_button.configure(state='disabled')
                self.stop_task_button.configure(state='disabled')
                for button in self.buttons:
                    button.configure(state='normal')
                if kind == 'error':
                    text = '已停止当前任务。' if self.generation_cancel.is_set() else str(payload)
                    self.status.set(text)
                    self.chat_hint.set(text)
                    if self.pending_chat is not None:
                        self.save_interrupted_answer(text)
                        self.write(self.transcript, '\n[' + text + ']\n\n', True)
                    if not self.generation_cancel.is_set():
                        self.write(self.download_log, '任务未完成：' + text + '\n', True)
                else:
                    self.status.set('已完成 · 记录保存在本机')
                    handler(payload)
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def close(self):
        if self.busy:
            self.generation_cancel.set()
            self.status.set('已请求停止当前任务，结束后可关闭窗口。')
            return
        self.download_cancel.set()
        from qwen_chat import shutdown
        shutdown()
        self.root.destroy()


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG, encoding='utf-8', level=logging.INFO)
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('FreeAI.Desktop.2')
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    app = DesktopApp(root)
    if '--experience' in sys.argv:
        root.after(300, app.open_epistemology)
    root.mainloop()

if __name__ == '__main__':
    main()
