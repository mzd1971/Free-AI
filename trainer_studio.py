"""Native layout for the training workbench and its shared navigation."""

import tkinter as tk
from tkinter import ttk

from hf_dataset_panel import HuggingFaceDatasetPanel
from paths import ROOT
import training_extension as extension
from ui_controls import SwitchRow
from ui_kit import FlatButton, RoundedFrame, ScrollArea, label, ui_font
from ui_preferences import TEXT_SIZES


PAGE_TITLES = ('训练工作室', '训练记录', '经验库')


def build_shell(app):
    from trainer_app import TRAINER_CONTENT_WIDTH, TRAINER_THEMES

    c = app.colors
    body = app.frame(app.root)
    body.pack(fill='both', expand=True)
    app.sidebar = side = app.frame(body, c['side'], width=224)
    side.pack(side='left', fill='y')
    side.pack_propagate(False)
    brand = app.frame(side, c['side'])
    brand.pack(fill='x', padx=18, pady=(20, 27))
    try:
        app.brand_image = tk.PhotoImage(file=str(ROOT / 'assets/free-ai-ui.png')).subsample(2)
        tk.Label(brand, image=app.brand_image, bg=c['side'], bd=0).pack(side='left', padx=(0, 8))
    except tk.TclError:
        pass
    app.brand_label = label(brand, 'Free AI', size=25, bold=True, color=c.get('brand', c['text']))
    app.brand_label.pack(side='left')

    label(side, '训练工作台', size=10, bold=True, color=c['muted']).pack(anchor='w', padx=20, pady=(0, 8))
    for index, (title, icon) in enumerate(zip(PAGE_TITLES, ('grid', 'copy', 'memory'))):
        button = FlatButton(side, title, lambda i=index: app.select_page(i), icon=icon,
                            variant='ghost', radius=8, anchor='w', height=36, font_size=11)
        button.pack(fill='x', padx=12, pady=2)
        app.nav.append(button)

    side_info = app.frame(side, c['side'])
    side_info.pack(side='bottom', fill='x', padx=16, pady=16)
    app.frame(side_info, c['line'], height=1).pack(fill='x', pady=(0, 10))
    app.wrap_label(side_info, '日常对话请使用 Free AI 客户端。', size=10, color=c['muted']).pack(fill='x')
    resize_sidebar(app)

    right = app.frame(body)
    right.pack(side='left', fill='both', expand=True)
    header = app.frame(right, c['surface'], padx=20, pady=10)
    header.pack(fill='x')
    app.frame(right, c['line'], height=1).pack(fill='x')
    header.columnconfigure(0, weight=1)
    crumb = app.frame(header, c['surface'])
    app.page_caption = tk.StringVar(app.root, value=PAGE_TITLES[0])
    label(crumb, textvariable=app.page_caption, size=11, bold=True).pack(side='left')

    prefs = app.frame(header, c['surface'])
    theme_group = app.frame(prefs, c['surface'])
    theme_group.pack(side='left', fill='x', expand=True)
    label(theme_group, '主题', size=10, color=c['muted']).pack(side='left', padx=(0, 6))
    _style_fields(app)
    app.theme_picker = ttk.Combobox(theme_group, textvariable=app.theme_choice, state='readonly',
                                    values=tuple(TRAINER_THEMES), width=14, font=ui_font(app.root),
                                    style='Studio.TCombobox')
    app.theme_picker.pack(side='left', fill='x', expand=True)
    app.theme_picker.bind('<<ComboboxSelected>>', app.change_theme)
    size_group = app.frame(prefs, c['surface'])
    size_group.pack(side='right', padx=(12, 0))
    label(size_group, '字号', size=10, color=c['muted']).pack(side='left', padx=(0, 6))
    app.text_size_picker = ttk.Combobox(size_group, textvariable=app.text_size, state='readonly',
                                        values=tuple(TEXT_SIZES), width=5, font=ui_font(app.root),
                                        style='Studio.TCombobox')
    app.text_size_picker.pack(side='left')
    app.text_size_picker.bind('<<ComboboxSelected>>', app.change_text_size)

    header_layout = None

    def arrange_header(event=None):
        nonlocal header_layout
        inline = header.winfo_width() >= crumb.winfo_reqwidth() + prefs.winfo_reqwidth() + 54
        if inline == header_layout:
            return
        header_layout = inline
        crumb.grid(row=0, column=0, sticky='w')
        prefs.grid(row=int(not inline), column=int(inline), sticky='e' if inline else 'ew',
                   padx=(10, 0) if inline else 0, pady=0 if inline else (7, 0))

    header.bind('<Configure>', arrange_header)
    app.arrange_studio_header = arrange_header
    arrange_header()

    viewport = app.frame(right)
    viewport.pack(fill='both', expand=True)
    app.main = app.frame(viewport)
    app.main.place(relx=.5, relheight=1, anchor='n')
    viewport.bind('<Configure>', lambda event: app.main.place_configure(
        width=max(1, min(TRAINER_CONTENT_WIDTH, event.width - 28))))
    footer = app.frame(app.main)
    footer.pack(side='bottom', fill='x')
    app.frame(footer, c['line'], height=1).pack(fill='x')
    footer_body = app.frame(footer, pady=5)
    footer_body.pack(fill='x')
    app.stop_button = FlatButton(footer_body, '停止任务', app.stop_training, icon='stop',
                                 variant='ghost', height=32, radius=8, font_size=10, state='disabled')
    app.stop_button.pack(side='right', padx=(8, 4))
    app.stop_buttons.append(app.stop_button)
    app.wrap_label(footer_body, textvariable=app.status, size=10, color=c['muted']).pack(
        side='left', fill='x', expand=True, padx=(5, 0))
    app.theme_notice_label = app.wrap_label(app.main, textvariable=app.theme_notice, size=10, color=c['muted'])
    app.page_holder = app.frame(app.main)
    app.page_holder.pack(fill='both', expand=True)
    app.page_holder.pack_propagate(False)
    app.pages = [ScrollArea(app.page_holder, scrollbar=True) for _ in PAGE_TITLES]
    app.train_scroll, app.records_scroll, app.knowledge_scroll = app.pages
    for page in app.pages:
        page.body.configure(padx=6, pady=18)


def resize_sidebar(app):
    brand_width = ui_font(app.sidebar, 25, bold=True).measure('Free AI')
    nav_width = max(button.winfo_reqwidth() for button in app.nav)
    app.sidebar.configure(width=max(216, brand_width + 76, nav_width + 24))


def _style_fields(app):
    c = app.colors
    style = ttk.Style(app.root)
    for element in ('TEntry', 'TSpinbox', 'TCombobox'):
        name = 'Studio.' + element
        style.configure(name, borderwidth=1, padding=(7, 5), fieldbackground=c['input'],
                        bordercolor=c['line'], lightcolor=c['line'], darkcolor=c['line'])
        style.map(name, bordercolor=[('focus', c['focus'])])


def build_training_page(app, page):
    c = app.colors
    app.heading(page, '训练工作室', '用练习题微调模型，通过留出题检验效果。')

    step_strip = RoundedFrame(page, bg=c['surface'], border=c['card_border'], radius=11, padding=4)
    step_strip.pack(fill='x', pady=(0, 14))
    app.studio_steps = []
    sections = (('训练模型', 'memory'), ('练习数据', 'folder'), ('训练配置', 'settings'))
    for index, (title, icon) in enumerate(sections):
        step = FlatButton(step_strip.body, title, lambda i=index: _scroll_to_step(app, i),
                          icon=icon, variant='ghost', radius=8, height=34, font_size=11)
        step_strip.body.columnconfigure(index, weight=1, uniform='studio-step')
        step.grid(row=0, column=index, sticky='ew', padx=2)
        app.studio_steps.append(step)

    app.studio_columns = columns = app.frame(page)
    columns.pack(fill='x')
    app.studio_form = form = app.frame(columns)
    app.studio_overview = app.frame(columns)
    overview = app.frame(app.studio_overview)
    # Measure the height at the assigned width without letting wrapped text resize the columns.
    overview.place(relwidth=1)
    overview.bind('<Configure>', lambda event: app.studio_overview.configure(height=event.height))
    app.setup_card = model = app.card(form, '训练模型')
    label(model, 'Qwen3-0.6B', size=16, bold=True).pack(anchor='w')
    app.wrap_label(model, textvariable=app.preparation_notice, size=11, color=c['muted']).pack(
        fill='x', pady=(3, 10))
    row = app.button_row(model)
    row.pack(fill='x')
    app.tools_button = app.button(row, '准备训练工具', app.prepare_tools, width=155)
    app.model_button = app.button(row, '下载模型', app.prepare_model, width=150)
    app.use_model_button = app.button(row, '选用模型', app.use_training_model, variant='accent', width=150)
    app.wrap_label(model, f'模型目录  {app.manager.root}', size=10, color=c['muted']).pack(fill='x', pady=(5, 0))

    app.data_card = data = app.card(form, '练习数据')
    label(data, '数据文件夹', size=10, color=c['muted']).pack(anchor='w', pady=(0, 4))
    app.dataset_field = ttk.Entry(data, textvariable=app.dataset, font=ui_font(data), style='Studio.TEntry')
    app.dataset_field.pack(fill='x', pady=(0, 8))
    row = app.button_row(data)
    row.pack(fill='x')
    app.button(row, '选择文件夹', app.choose_data)
    app.button(row, '检查数据', app.check_data, variant='accent')
    app.online_data_button = app.button(row, '查找在线数据', app.toggle_online_data, variant='ghost')
    app.button(row, '数据格式', app.data_help, variant='ghost')
    app.wrap_label(data, textvariable=app.data_notice, size=11, color=c['muted']).pack(fill='x', pady=(3, 0))
    app.dataset_panel = HuggingFaceDatasetPanel(app, data, app.imported_dataset)

    app.configuration_card = config = app.card(form, '训练配置')
    app.configuration_summary = tk.StringVar(app.root)
    app.wrap_label(config, textvariable=app.configuration_summary, size=11, color=c['muted']).pack(fill='x')
    app.epochs.trace_add('write', lambda *_: _update_configuration(app))
    app.learning_rate.trace_add('write', lambda *_: _update_configuration(app))
    _update_configuration(app)
    app.parameters_button = FlatButton(config, '更多设置', app.toggle_parameters,
                                       icon='settings', variant='ghost', font_size=11, height=32, radius=7)
    app.parameters_button.pack(anchor='w', pady=(4, 0))
    _build_parameters(app, config)

    _build_overview(app, overview)
    app.studio_step_index = 0
    app.studio_steps[0].configure(variant='selected')

    logs = RoundedFrame(page, bg=c['surface'], border=c['card_border'], radius=12, padding=(18, 8))
    logs.pack(fill='x', pady=(0, 10))
    log_header = app.frame(logs.body, c['surface'])
    log_header.pack(fill='x')
    label(log_header, '运行日志', size=12, bold=True).pack(side='left')
    app.log_button = FlatButton(log_header, '展开', lambda: toggle_log(app), icon='chevron_down',
                                icon_side='right', variant='ghost', font_size=10, height=32, radius=8)
    app.log_button.pack(side='right')
    app.log_wrapper = app.frame(logs.body, c['surface'])
    app.log = app.text(app.log_wrapper, height=8)
    app.log.configure(bg=c['bg'])
    app.write(app.log, '暂无运行日志。')
    columns.bind('<Configure>', lambda event: arrange_studio(app, event))
    app.root.bind('<FocusIn>', lambda event: _keep_training_focus_visible(app, event.widget), add='+')
    arrange_studio(app)


def _update_configuration(app):
    app.configuration_summary.set(f'训练轮数  {app.epochs.get()}     学习率  {app.learning_rate.get()}')


def _build_parameters(app, config):
    c = app.colors
    app.parameters = app.frame(config, c['surface'])
    parameters = app.parameters
    row = app.button_row(parameters)
    row.pack(fill='x', pady=(7, 2))
    epochs = app.frame(row, c['surface'])
    label(epochs, '训练轮数', size=10).pack(anchor='w')
    ttk.Spinbox(epochs, from_=1, to=10, textvariable=app.epochs, width=9,
                style='Studio.TSpinbox').pack(fill='x', pady=(4, 0))
    rate = app.frame(row, c['surface'])
    label(rate, '学习率', size=10).pack(anchor='w')
    ttk.Entry(rate, textvariable=app.learning_rate, width=12,
              style='Studio.TEntry').pack(fill='x', pady=(4, 0))
    app.agent_target_switch = SwitchRow(parameters, '保存为工具使用练习成果', app.agent_target, bg=c['surface'])
    app.agent_target_switch.pack(fill='x', pady=(5, 10))
    app.wrap_label(parameters, '继续训练已有成果（选填）', size=10, color=c['muted']).pack(fill='x', pady=(0, 5))
    adapter = app.frame(parameters, c['surface'])
    adapter.pack(fill='x')
    button = app.button(adapter, '选择成果', app.choose_adapter)
    button.pack_configure(side='right', padx=(8, 0))
    ttk.Entry(adapter, textvariable=app.adapter, style='Studio.TEntry').pack(side='left', fill='x', expand=True)


def _build_overview(app, overview):
    c = app.colors
    summary = app.card(overview, '本次训练')
    app.studio_state = label(summary, '待准备', size=11, bold=True, color=c['muted'])
    app.studio_state.pack(anchor='w', pady=(0, 6))
    app.studio_phase = app.wrap_label(summary, size=10, color=c['muted'])
    app.studio_phase.pack(fill='x')
    app.frame(summary, c['line'], height=1).pack(fill='x', pady=(4, 7))
    app.studio_checks = {}
    for key, name in (('tools', '训练工具'), ('model', '训练模型'), ('data', '练习数据')):
        item = app.frame(summary, c['surface'])
        item.pack(fill='x', pady=4)
        label(item, name, size=10).pack(side='left')
        value = label(item, size=10, color=c['muted'])
        value.pack(side='right')
        app.studio_checks[key] = value
    app.start_button = app.button(summary, '开始训练', app.finetune, variant='primary')
    app.start_button.pack_configure(side='top', fill='x', pady=(14, 0), padx=0)

    results = app.card(overview, '训练成果')
    app.wrap_label(results, textvariable=app.result_notice, size=10, color=c['muted']).pack(fill='x')
    row = app.button_row(results)
    row.pack(fill='x', pady=(7, 0))
    app.button(row, '最近结果', app.latest_result, variant='ghost')
    app.button(row, '成果文件夹', app.open_outputs, variant='ghost')


def _scroll_to_step(app, index):
    card = (app.setup_card, app.data_card, app.configuration_card)[index].container
    app.studio_steps[app.studio_step_index].configure(variant='ghost')
    app.studio_step_index = index
    app.studio_steps[index].configure(variant='selected')
    canvas = app.train_scroll.canvas
    canvas.update_idletasks()
    offset = card.winfo_rooty() - app.train_scroll.body.winfo_rooty()
    canvas.yview_moveto(max(0, offset - 10) / max(1, app.train_scroll.body.winfo_reqheight()))
    control = (app.tools_button, app.dataset_field, app.parameters_button)[index]
    if control.cget('state') != 'disabled':
        control.focus_set()


def _keep_training_focus_visible(app, widget):
    scroll = app.train_scroll
    canvas = scroll.canvas
    if (not isinstance(widget, tk.Misc) or not scroll.winfo_ismapped()
            or str(widget) != str(app.root.tk.call('focus'))):
        return
    ancestor = widget
    while ancestor is not None and ancestor is not scroll.body:
        ancestor = ancestor.master
    if ancestor is None:
        return
    top = widget.winfo_rooty() - canvas.winfo_rooty()
    bottom = top + widget.winfo_height()
    available = canvas.winfo_height()
    if top < 0:
        offset = top - 8
    elif bottom > available:
        offset = bottom - available + 8
    else:
        return
    canvas.yview_moveto(max(0, canvas.canvasy(0) + offset) / max(1, scroll.body.winfo_reqheight()))


def arrange_studio(app, event=None):
    app.arrange_studio_header()
    columns = app.studio_columns
    width = event.width if event is not None else columns.winfo_width()
    overview_width = max(270, ui_font(columns, 12).measure('练习数据     已检查') + 66)
    form_width = max(595, sum(button.winfo_reqwidth() + 8 for button in
                              (app.tools_button, app.model_button, app.use_model_button)) + 48)
    if app.dataset_panel.winfo_manager():
        form_width = max(form_width, 740)
    stacked = width < form_width + overview_width + 16
    layout = stacked, overview_width
    if getattr(app, 'studio_layout', None) == layout:
        return
    app.studio_layout = layout
    columns.columnconfigure(0, weight=1)
    columns.columnconfigure(1, weight=0, minsize=0 if stacked else overview_width)
    app.studio_form.grid(row=0, column=0, sticky='new', padx=0 if stacked else (0, 16))
    app.studio_overview.grid(row=int(stacked), column=int(not stacked), sticky='new')
    app.studio_overview.configure(width=overview_width if not stacked else 1)


def refresh_studio(app, *, tools_ready=None, model_ready=None):
    if tools_ready is not None:
        app.studio_tools_ready = tools_ready
    if model_ready is not None:
        app.studio_model_ready = model_ready
    if not hasattr(app, 'studio_tools_ready'):
        app.studio_tools_ready = extension.training_python() is not None
    if not hasattr(app, 'studio_model_ready'):
        app.studio_model_ready = app.model_ready()

    c = app.colors
    tools = app.studio_tools_ready
    model = app.studio_model_ready
    selected = model and app.trainer_model.get() == 'legacy-training-model'
    checked = bool(app.checked_dataset and app.dataset.get() == app.checked_dataset)
    for key, ready, text in (('tools', tools, '已就绪' if tools else '待安装'),
                             ('model', selected, '已选择' if selected else '待选择' if model else '待下载'),
                             ('data', checked, '已检查' if checked else '待检查')):
        app.studio_checks[key].configure(text=text, fg=c['success'] if ready else c['muted'])

    phase = app.phase.get()
    if app.busy:
        if app.cancel_event.is_set():
            state, detail = '正在停止', app.status.get()
        elif app.current_job == 'training':
            state = '训练中'
            detail = '正在检查数据并准备模型。' if phase == '等待开始' else phase
        else:
            state, detail = '任务进行中', app.status.get()
        color = c['accent']
    elif phase.startswith('训练已结束'):
        state, _, detail = phase.partition(' · ')
        color = c['success'] if detail == '验收通过' else c['muted']
    elif phase in ('已停止', '本次任务未完成', '已安全停止'):
        state, detail, color = phase, '', c['muted']
    elif tools and selected:
        state = '可开始训练'
        detail = '训练前会核对练习题和检查题。' if not checked else ''
        color = c['success']
    else:
        state, detail, color = '待准备', '', c['muted']
    app.studio_state.configure(text=state, fg=color)
    app.studio_phase.configure(text=detail)
    if detail and not app.studio_phase.winfo_manager():
        app.studio_phase.pack(fill='x', after=app.studio_state)
    elif not detail:
        app.studio_phase.pack_forget()


def set_log_visible(app, visible):
    if visible:
        app.log_wrapper.pack(fill='x')
        app.log.see('end')
    else:
        app.log_wrapper.pack_forget()
    app.log_button.configure(text='收起' if visible else '展开', icon=None if visible else 'chevron_down')


def toggle_log(app):
    set_log_visible(app, not app.log_wrapper.winfo_manager())
