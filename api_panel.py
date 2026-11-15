"""Small native settings form for an explicit, user-owned API connection."""
import tkinter as tk
from tkinter import ttk
from urllib.parse import urlsplit

from remote_api import APIClient, normalize_base_url
from ui_kit import COLORS as C, FONT, FlatButton, RoundedFrame, label, ui_font


class APISettingsPanel:
    PROVIDERS = {'DeepSeek': 'deepseek', '自定义兼容接口': 'custom'}

    def __init__(self, app, parent):
        self.app = app
        config = app.api_settings.public()
        self.provider = tk.StringVar(value='DeepSeek' if config['provider'] == 'deepseek' else '自定义兼容接口')
        self.base_url = tk.StringVar(value=config['base_url'])
        self.model = tk.StringVar(value=config['model'])
        self.key = tk.StringVar(value='')
        self.remember = tk.BooleanVar(value=config['remember_key'])
        self.include_memory = tk.BooleanVar(value=config['include_memory'])
        self.thinking = tk.BooleanVar(value=config.get('thinking', False))
        self.max_tokens = tk.StringVar(value=str(config['max_output_tokens']))
        self.notice = tk.StringVar()
        card = RoundedFrame(parent, padding=22)
        card.pack(fill='x', pady=(0, 14))
        self.body = card.body
        label(self.body, 'API 连接 · 使用联网模型', size=14, bold=True).pack(anchor='w')
        intro = label(self.body, '聊天会发送到你填写的服务地址，费用由该服务商计算。保存和打开应用都不会自动发起请求。',
                      size=11, color=C['muted'], justify='left', anchor='w')
        intro.pack(fill='x', pady=(8, 14))
        fields = tk.Frame(self.body, bg=C['surface'])
        fields.pack(fill='x')
        fields.columnconfigure(1, weight=1)
        for index, title in enumerate(('服务商', '服务地址', '模型名', 'API 密钥')):
            label(fields, title, size=11).grid(row=index, column=0, sticky='w', padx=(0, 18), pady=7)
        provider = ttk.Combobox(fields, textvariable=self.provider, values=list(self.PROVIDERS), state='readonly', font=ui_font(self.app.root, 11))
        provider.grid(row=0, column=1, sticky='ew', pady=7)
        provider.bind('<<ComboboxSelected>>', self.provider_changed)
        ttk.Entry(fields, textvariable=self.base_url, font=ui_font(self.app.root, 11)).grid(row=1, column=1, sticky='ew', pady=7)
        self.model_picker = ttk.Combobox(fields, textvariable=self.model, values=['deepseek-flash', 'deepseek-v4-pro'], font=ui_font(self.app.root, 11))
        self.model_picker.grid(row=2, column=1, sticky='ew', pady=7)
        ttk.Entry(fields, textvariable=self.key, show='●', font=ui_font(self.app.root, 11)).grid(row=3, column=1, sticky='ew', pady=7)
        key_hint = label(self.body, '已保存的密钥不会回填显示；留空可保留。更换服务地址后，请重新填写。',
                         size=10, color=C['muted'], justify='left', anchor='w')
        key_hint.pack(fill='x', pady=(0, 8))
        ttk.Checkbutton(self.body, text='在本机加密保存密钥（换电脑需重新填写）', variable=self.remember).pack(anchor='w', pady=3)
        ttk.Checkbutton(self.body, text='允许引用本地经验（相关经验会一起发给服务商）', variable=self.include_memory).pack(anchor='w', pady=3)
        self.thinking_check = ttk.Checkbutton(self.body, text='DeepSeek 深入思考（可能更慢）', variable=self.thinking)
        self.thinking_check.pack(anchor='w', pady=3)
        limits = tk.Frame(self.body, bg=C['surface'])
        limits.pack(fill='x', pady=(9, 10))
        label(limits, '回答长度上限', size=11).pack(side='left')
        ttk.Combobox(limits, textvariable=self.max_tokens, values=['512', '1024', '2048', '4096', '8192'], width=9,
                     font=ui_font(self.app.root, 11)).pack(side='left', padx=12)
        label(limits, '越大可回答越长，也可能增加用量', size=10, color=C['muted']).pack(side='left')
        buttons = tk.Frame(self.body, bg=C['surface'])
        buttons.pack(fill='x', pady=(3, 8))
        self.connection_buttons = []
        for title, callback, variant in [('保存设置', self.save, 'secondary'), ('测试连接', self.test_connection, 'secondary'),
                                          ('使用 API', self.enable, 'primary'), ('切回本机', app.use_local_mode, 'ghost')]:
            button = FlatButton(buttons, title, callback, variant=variant, width=112, height=36)
            self.connection_buttons.append(button)
            app.buttons.append(button)
        self.layout_buttons(2)
        buttons.bind('<Configure>', lambda event: self.layout_buttons(
            4 if event.width >= sum(button.winfo_reqwidth() + 8 for button in self.connection_buttons) else 2))
        clear = FlatButton(self.body, '清除已存密钥', self.clear_key, variant='ghost', width=125, height=30, font_size=10)
        clear.pack(anchor='w')
        app.buttons.append(clear)
        notice = label(self.body, textvariable=self.notice, size=10, color=C['muted'], justify='left', anchor='w')
        notice.pack(fill='x', pady=(7, 0))
        for hint in (intro, key_hint, notice):
            hint.bind('<Configure>', lambda event: event.widget.configure(wraplength=min(740, event.width)))
        self.refresh_notice()

    def layout_buttons(self, columns):
        for index, button in enumerate(self.connection_buttons):
            button.grid(row=index // columns, column=index % columns, sticky='w', padx=(0, 8), pady=(0, 5))

    def provider_changed(self, event=None):
        if self.provider.get() == 'DeepSeek':
            self.base_url.set('https://api.deepseek.com')
            self.model.set('deepseek-flash')
            self.model_picker.configure(values=['deepseek-flash', 'deepseek-v4-pro'])
        else:
            self.model_picker.configure(values=[])
        self.thinking_check.configure(state='normal' if self.provider.get() == 'DeepSeek' else 'disabled')
        self.notice.set('修改尚未保存。自定义接口需支持 Chat Completions；请填写完整的接口根地址。')

    def form(self):
        return dict(provider=self.PROVIDERS[self.provider.get()], base_url=normalize_base_url(self.base_url.get()),
                    model=self.model.get().strip(), remember_key=self.remember.get(), include_memory=self.include_memory.get(),
                    max_output_tokens=int(self.max_tokens.get()), thinking=self.thinking.get())

    def refresh_notice(self):
        config = self.app.api_settings.public()
        self.thinking_check.configure(state='normal' if self.provider.get() == 'DeepSeek' else 'disabled')
        self.notice.set(config.get('key_notice') or ('密钥可用。连接测试只读取模型列表，不会发送对话。' if config['key_available']
                        else '尚未填写密钥。先在服务商处开通 API，再将密钥填在此处。'))

    def save(self):
        if self.app.busy:
            return False
        try:
            self.app.api_settings.save(**self.form(), key=self.key.get().strip() or None)
            self.key.set('')
            self.refresh_notice()
            self.app.refresh_model_state()
            self.app.status.set('API 设置已保存；点击“使用 API”后，发送消息才会调用。')
            return True
        except Exception:
            self.notice.set('无法保存。请检查服务地址、模型名和回答长度；密钥只能保存在本机支持的加密存储中。')
            return False

    def test_connection(self):
        if self.app.busy:
            return
        try:
            values = self.form()
            key = self.key.get().strip()
            if not key:
                saved, saved_key = self.app.api_settings.connection()
                if values['base_url'] != saved['base_url']:
                    self.notice.set('服务地址已更改，请为这个地址重新填写密钥。')
                    return
                key = saved_key
            client = APIClient(values['base_url'], values['model'], key, max_output_tokens=values['max_output_tokens'],
                               thinking=values['thinking'] if values['provider'] == 'deepseek' else None)
        except Exception:
            self.notice.set('请检查服务地址、密钥、模型名和回答长度。连接测试不会使用其他地址保存的密钥。')
            return
        host = urlsplit(values['base_url']).netloc
        def done(result):
            if not result.get('connected'):
                self.notice.set(result.get('message') or '连接未成功，请检查服务地址和密钥。')
                return
            models = result.get('models', [])
            self.model_picker.configure(values=models)
            selected = '' if result.get('selected_model_listed') else '当前所填模型不在列表中，可重新选择或手动核对。'
            self.notice.set(f'已连接 {host}，读取到 {len(models)} 个模型。{selected}仅验证连接，未发送生成请求；设置尚需保存。')
        self.app.submit('正在测试 API 连接', client.test_connection, done)

    def enable(self):
        if self.save():
            self.app.enable_api()

    def clear_key(self):
        if self.app.busy:
            return
        try:
            self.app.api_settings.clear_key()
            self.key.set('')
            self.refresh_notice()
            self.app.refresh_model_state()
            self.app.status.set('API 密钥已清除，服务地址和模型名仍保留。')
        except Exception:
            self.notice.set('无法清除密钥，请检查数据目录是否可写。')
