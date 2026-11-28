"""Browse public Hugging Face datasets inside the training wizard."""
import json
import logging
import tkinter as tk
from tkinter import ttk
from urllib.parse import quote
import webbrowser

import hf_datasets
from hf_dataset_catalog import RECOMMENDED_DATASETS, recommend_datasets
from ui_kit import COLORS as C, FlatButton, background, label, ui_font


DATASET_HOME = 'https://huggingface.co/datasets'


class HuggingFaceDatasetPanel(tk.Frame):
    def __init__(self, app, parent, on_import):
        super().__init__(parent, bg=background(parent))
        self.app = app
        self.on_import = on_import
        self.query = tk.StringVar(self)
        self.search_message = tk.StringVar(self)
        self.source_description = tk.StringVar(self)
        self.status = tk.StringVar(self)
        self.config_name = tk.StringVar(self)
        self.split_name = tk.StringVar(self)
        self.import_count = tk.StringVar(self, value='100')
        self.fields = {key: tk.StringVar(self) for key in ('prompt', 'response', 'input', 'messages')}
        self._repositories = {}
        self._recommendations = {}
        self._recommendation_records = {item['id']: item for item in recommend_datasets()}
        for item in RECOMMENDED_DATASETS:
            title, repository, description = item
            for name in (repository, title, f'{title} · {repository}'):
                self._recommendations[name.casefold()] = item
        self._source = None
        self._splits = {}
        self._preview_source = None
        self._columns = ()
        self._pending = False
        self._query_version = 0

        header = tk.Frame(self, bg=background(self))
        header.pack(fill='x')
        heading = label(header, 'Hugging Face 训练数据', size=12, bold=True, anchor='w')
        website = tk.Frame(header, bg=background(self))
        self.website_button = FlatButton(website, '数据集官网', lambda: webbrowser.open(DATASET_HOME),
                                         variant='ghost', font_size=10, height=30)
        self.website_button.pack(side='right')
        self._responsive_row(header, heading, website)
        self._wrap(self, text='可搜“中文”“代码”“数学”，也支持部分名称和近似拼写。选择数据集后自动预览。').pack(fill='x')
        search = tk.Frame(self, bg=background(self))
        search.pack(fill='x', pady=(6, 4))
        label(search, '数据集', size=11).pack(side='left', padx=(0, 8))
        self.search_button = FlatButton(search, '搜索', self.search, variant='primary', height=32, font_size=11)
        self.search_button.pack(side='right', padx=(8, 0))
        self.query_entry = ttk.Combobox(search, textvariable=self.query, width=12,
                                        values=tuple(f'{title} · {repository}' for title, repository, _ in RECOMMENDED_DATASETS))
        self.query_entry.pack(side='left', fill='x', expand=True)
        self.query_entry.bind('<Return>', self.search)
        self.query_entry.bind('<<ComboboxSelected>>', self._choose_recommendation)
        self._wrap(self, textvariable=self.search_message).pack(fill='x')

        self.result_area = tk.Frame(self, bg=background(self))
        self.details = tk.Frame(self, bg=background(self))
        self._build_results()
        self._build_source()
        self._build_preview()
        self.footer = tk.Frame(self, bg=background(self))
        self.footer.pack(fill='x', pady=(2, 0))
        self.stop_button = FlatButton(self.footer, '停止', self._stop, height=30, font_size=11)
        self.status_label = self._wrap(self.footer, textvariable=self.status)
        self.status_label.pack(side='left', fill='x', expand=True)

        self.query.trace_add('write', self._query_changed)
        self.config_name.trace_add('write', self._config_changed)
        self.split_name.trace_add('write', self._split_changed)
        self.import_count.trace_add('write', self._mapping_changed)
        for variable in self.fields.values():
            variable.trace_add('write', self._mapping_changed)
        self._show_repositories(list(self._recommendation_records.values()))
        self.search_message.set('下面是推荐数据集。可直接选择，也可搜索更多。')
        self.sync_busy()

    @staticmethod
    def _wrap(parent, **kwargs):
        widget = label(parent, size=10, color=C['muted'], anchor='w', justify='left', **kwargs)
        widget.bind('<Configure>', lambda event: widget.configure(wraplength=max(1, event.width - 4)))
        return widget

    @staticmethod
    def _responsive_row(frame, *widgets):
        layout = None

        def reflow(event=None):
            nonlocal layout
            horizontal = frame.winfo_width() >= sum(widget.winfo_reqwidth() + 6 for widget in widgets)
            if horizontal == layout:
                return
            layout = horizontal
            for index, widget in enumerate(widgets):
                frame.columnconfigure(index, weight=int(horizontal or index == 0))
                widget.grid(row=0 if horizontal else index, column=index if horizontal else 0,
                            sticky='ew', padx=(0, 6) if horizontal and index < len(widgets) - 1 else 0,
                            pady=(0, 4) if not horizontal else 0)

        frame.bind('<Configure>', reflow)
        reflow()

    @staticmethod
    def _choice(parent, title, variable):
        frame = tk.Frame(parent, bg=background(parent))
        label(frame, title, size=10).pack(side='left', padx=(0, 6))
        box = ttk.Combobox(frame, textvariable=variable, width=9, state='readonly')
        box.pack(side='left', fill='x', expand=True)
        return frame, box

    def _build_results(self):
        tree = ttk.Treeview(self.result_area, columns=('repo', 'license'), show='headings',
                            height=4, selectmode='browse')
        self.repository_tree = tree
        scroll = ttk.Scrollbar(self.result_area, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        tree.pack(side='left', fill='x', expand=True)
        tree.heading('repo', text='数据集 / 用途', anchor='w')
        tree.heading('license', text='许可证', anchor='w')
        tree.column('repo', width=255, minwidth=120, anchor='w')
        tree.column('license', width=120, minwidth=70, anchor='w', stretch=False)
        tree.bind('<<TreeviewSelect>>', self._select_repository)

    def _build_source(self):
        header = tk.Frame(self.details, bg=background(self))
        header.pack(fill='x', pady=(0, 3))
        self.open_button = FlatButton(header, '打开仓库', self._open_repository,
                                      variant='ghost', height=30, font_size=10)
        self.open_button.pack(side='right')
        self._wrap(header, textvariable=self.source_description).pack(side='left', fill='x', expand=True)
        self.choices = tk.Frame(self.details, bg=background(self))
        config, self.config_box = self._choice(self.choices, '子集', self.config_name)
        split, self.split_box = self._choice(self.choices, '划分', self.split_name)
        action = tk.Frame(self.choices, bg=background(self))
        self.preview_button = FlatButton(action, '预览样本', self._load_preview, height=30, font_size=10)
        self.preview_button.pack(anchor='e')
        self._responsive_row(self.choices, config, split, action)
        self.reload_button = FlatButton(self.details, '重试读取子集', self._load_splits, height=30, font_size=10)

    def _build_preview(self):
        self.preview_area = tk.Frame(self.details, bg=background(self))
        self.preview_description = tk.StringVar(self)
        self._wrap(self.preview_area, textvariable=self.preview_description).pack(fill='x', pady=(6, 3))
        sample = tk.Frame(self.preview_area, bg=background(self))
        sample.pack(fill='x')
        self.preview_text = tk.Text(sample, height=7, width=1, wrap='word', state='disabled', relief='flat',
                                    padx=8, pady=4, font=ui_font(self, 10), bg=C['input'], fg=C['text'],
                                    selectbackground=C['selection_bg'], selectforeground=C['selection_fg'],
                                    highlightthickness=1, highlightbackground=C['line'], highlightcolor=C['focus'])
        scrollbar = ttk.Scrollbar(sample, orient='vertical', command=self.preview_text.yview)
        self.preview_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.preview_text.pack(side='left', fill='x', expand=True)

        self.mapping_area = tk.Frame(self.preview_area, bg=background(self))
        self.field_boxes = {}
        for fields in ((('prompt', '问题 / 指令'), ('response', '回答')),
                       (('input', '上下文（可选）'), ('messages', '对话消息（可选）'))):
            row = tk.Frame(self.mapping_area, bg=background(self))
            row.pack(fill='x', pady=2)
            pairs = []
            for key, title in fields:
                frame, self.field_boxes[key] = self._choice(row, title, self.fields[key])
                pairs.append(frame)
            self._responsive_row(row, *pairs)
        self._wrap(self.mapping_area, text='选择对话消息列时，使用该列中的对话作为训练样本。').pack(
            fill='x', pady=(2, 4))
        controls = tk.Frame(self.mapping_area, bg=background(self))
        controls.pack(fill='x')
        count = tk.Frame(controls, bg=background(self))
        label(count, '最多读取条数', size=10).pack(side='left', padx=(0, 6))
        self.count_entry = ttk.Entry(count, textvariable=self.import_count, width=5)
        self.count_entry.pack(side='left')
        label(count, f'{hf_datasets.MIN_IMPORT_ROWS}–{hf_datasets.MAX_IMPORT_ROWS} 源行', size=10,
              color=C['muted']).pack(side='left', padx=(6, 0))
        action = tk.Frame(controls, bg=background(self))
        self.import_button = FlatButton(action, '下载训练数据', self._import_dataset,
                                        variant='primary', height=32, font_size=11)
        self.import_button.pack(anchor='e')
        self._responsive_row(controls, count, action)

    def _query_changed(self, *_):
        self._query_version += 1
        suggestions = recommend_datasets(self.query.get(), limit=12)
        self.query_entry.configure(values=tuple(f"{item['title']} · {item['id']}" for item in suggestions))
        if self._repositories and not self._pending:
            self.search_message.set('下拉框显示相近推荐；点击“搜索”可联网查找更多。')
        self.sync_busy()

    def _mapping_changed(self, *_):
        self.sync_busy()

    def _working(self):
        return self.app.busy or self._pending

    def _selection(self):
        config = self.config_name.get()
        split = self.split_name.get()
        if self._source and split in self._splits.get(config, ()):
            return self._source['id'], config, split
        return None

    def _count(self):
        try:
            count = int(self.import_count.get())
        except ValueError:
            return None
        if hf_datasets.MIN_IMPORT_ROWS <= count <= hf_datasets.MAX_IMPORT_ROWS:
            return count
        return None

    def _mapping(self):
        messages = self.fields['messages'].get()
        if messages:
            return {'messages': messages} if messages in self._columns else None
        fields = {key: self.fields[key].get() for key in ('prompt', 'response', 'input')}
        active = [value for value in fields.values() if value]
        if (fields['prompt'] and fields['response'] and len(set(active)) == len(active)
                and all(value in self._columns for value in active)):
            return fields
        return None

    def sync_busy(self):
        working = self._working()
        selection = self._selection()
        self.search_button.configure(state='normal' if self.query.get().strip() and not working else 'disabled')
        self.reload_button.configure(state='normal' if self._source and not working else 'disabled')
        self.preview_button.configure(state='normal' if selection and not working else 'disabled')
        self.open_button.configure(state='normal' if self._source else 'disabled')
        self.config_box.configure(state='readonly' if self._splits and not working else 'disabled')
        self.split_box.configure(state='readonly' if self._splits and not working else 'disabled')
        for key, box in self.field_boxes.items():
            enabled = self._columns and not working and (key == 'messages' or not self.fields['messages'].get())
            box.configure(state='readonly' if enabled else 'disabled')
        self.count_entry.configure(state='normal' if not working else 'disabled')
        import_ready = (not working and selection and selection == self._preview_source
                        and hf_datasets.is_training_split(selection[2]) and self._mapping() and self._count())
        self.import_button.configure(state='normal' if import_ready else 'disabled')
        if self._pending:
            self.stop_button.pack(side='right', padx=(6, 0), before=self.status_label)
            self.stop_button.configure(state='disabled' if self.app.cancel_event.is_set() else 'normal')
        else:
            self.stop_button.pack_forget()

    def _submit(self, title, work, done, fallback):
        self._pending = True

        def execute():
            try:
                return work(), ''
            except hf_datasets.DatasetCancelled:
                return None, '已停止 Hugging Face 数据集请求。'
            except hf_datasets.DatasetError as exc:
                return None, str(exc) or fallback
            except Exception:
                logging.exception('Hugging Face dataset task failed')
                return None, fallback

        def finish(outcome):
            if not self.winfo_exists():
                return
            self._pending = False
            try:
                done(*outcome)
            finally:
                self.sync_busy()

        self.app.submit(title, execute, finish, kind='hf-dataset', cancellable=True)
        self.sync_busy()

    def _stop(self):
        if self._pending:
            self.app.stop_training()
            self.status.set('正在停止 Hugging Face 数据集请求…')
            self.sync_busy()

    def _set_status(self, message):
        self.status.set(message)
        self.app.status.set(message)

    def _choose_recommendation(self, event=None):
        recommendation = self._recommendations.get(self.query.get().strip().casefold())
        if not recommendation or self._working():
            return
        title, repo_id, description = recommendation
        self.query.set(repo_id)
        self.search_message.set(f'{title} · {description}')
        if repo_id in self._repositories:
            self.repository_tree.selection_set(repo_id)
        else:
            self.repository_tree.selection_remove(*self.repository_tree.selection())
        self._activate_repository(self._repositories.get(repo_id, self._recommendation_records[repo_id]))

    def search(self, event=None):
        query = self.query.get().strip()
        if self._working() or not query:
            return 'break'
        if query.casefold() in self._recommendations:
            self._choose_recommendation()
            return 'break'
        version = self._query_version
        self.search_message.set('正在查找名称或用途相近的数据集…')
        self.status.set('')

        def show(repositories, error):
            if version != self._query_version:
                message = '关键词已改动，点击“搜索”查看匹配结果。'
            elif error:
                message = error
            else:
                self._show_repositories(repositories)
                message = (f'找到 {len(repositories)} 个相关数据集，按匹配程度排列。选择一项查看样本。'
                           if repositories else '没有找到匹配的公开数据集。')
            self.search_message.set(message)
            self.app.status.set(message)
            self.status.set('')

        self._submit('正在搜索 Hugging Face 数据集',
                     lambda: hf_datasets.search_datasets(query, cancel=self.app.cancel_event),
                     show, '无法搜索 Hugging Face 数据集，请检查网络连接后重试。')
        return 'break'

    def _show_repositories(self, repositories):
        self._repositories = {repo['id']: repo for repo in repositories}
        tree = self.repository_tree
        tree.delete(*tree.get_children())
        for repository in repositories:
            repo_id = repository['id']
            title = repository.get('title')
            caption = f'{title} · {repo_id}' if title else repo_id
            tree.insert('', 'end', iid=repo_id, values=(caption, repository['license'] or '未标注'))
        if repositories:
            tree.configure(height=min(6, len(repositories)))
            anchor = self.details if self.details.winfo_manager() else self.footer
            self.result_area.pack(fill='x', pady=(4, 0), before=anchor)
        else:
            self.result_area.pack_forget()
        if self._source and self._source['id'] in self._repositories:
            source_id = self._source['id']
            self._show_source(self._repositories[source_id])
            tree.selection_set(source_id)

    def _show_source(self, repository):
        self._source = repository
        details = [repository['id']]
        if repository.get('title'):
            details.insert(0, repository['title'])
        if repository.get('description'):
            details.append(repository['description'])
        details.append(f"许可证 · {repository['license'] or '未标注'}")
        self.source_description.set('\n'.join(details))

    def _select_repository(self, event):
        selection = self.repository_tree.selection()
        source_id = self._source['id'] if self._source else None
        if not selection or selection[0] == source_id:
            return
        if self._working():
            if source_id in self._repositories:
                self.repository_tree.selection_set(source_id)
            else:
                self.repository_tree.selection_remove(*selection)
            return
        self.query.set(selection[0])
        self.search_message.set('')
        self._activate_repository(self._repositories[selection[0]])

    def _activate_repository(self, repository):
        if self._source and self._source['id'] == repository['id']:
            if not self._splits:
                self._load_splits()
            elif not self._preview_source:
                self._load_preview()
            return
        self._show_source(repository)
        self.details.pack(fill='x', pady=(8, 0), before=self.footer)
        self._load_splits()

    def _load_splits(self):
        if self._working() or not self._source:
            return
        source_id = self._source['id']
        self._splits = {}
        self.config_box.configure(values=())
        self.config_name.set('')
        self.reload_button.pack_forget()
        self.choices.pack_forget()
        self.status.set('正在读取子集和划分…')

        def show(splits, error):
            if not self._source or self._source['id'] != source_id:
                return
            if error or not splits:
                self.reload_button.pack(anchor='w')
                self._set_status(error or '这个数据集没有可在线预览的划分。')
                return
            for split in splits:
                self._splits.setdefault(split['config'], []).append(split['split'])
            self.config_box.configure(values=tuple(self._splits))
            training = [config for config, names in self._splits.items()
                        if any(hf_datasets.is_training_split(name) for name in names)]
            candidates = training or list(self._splits)
            preferred = 'default' if 'default' in candidates else candidates[0]
            self.config_name.set(preferred)
            self.choices.pack(fill='x')
            self.app.status.set('已读取数据集子集和划分。')
            if not self.app.cancel_event.is_set():
                self._load_preview()

        self._submit('正在读取 Hugging Face 数据集划分',
                     lambda: hf_datasets.dataset_splits(source_id, cancel=self.app.cancel_event),
                     show, '无法读取子集和划分，请重试。')

    def _config_changed(self, *_):
        names = self._splits.get(self.config_name.get(), ())
        self.split_box.configure(values=tuple(names))
        self.split_name.set(next((name for name in names if hf_datasets.is_training_split(name)), names[0] if names else ''))

    def _split_changed(self, *_):
        self._preview_source = None
        self._columns = ()
        self.preview_area.pack_forget()
        for variable in self.fields.values():
            if variable.get():
                variable.set('')
        selection = self._selection()
        if selection:
            self.status.set('选择“预览样本”查看数据。' if hf_datasets.is_training_split(selection[2])
                            else '此划分可预览。导入请选择训练部分（train）。')
        else:
            self.status.set('')
        self.sync_busy()

    def _open_repository(self):
        if self._source:
            webbrowser.open(f"{DATASET_HOME}/{quote(self._source['id'], safe='/')}")

    def _load_preview(self):
        source = self._selection()
        if self._working() or not source:
            return
        self.status.set('正在读取样本预览…')

        def show(preview, error):
            if self._selection() != source:
                return
            if error:
                self._set_status(error)
                return
            rows = preview['rows']
            self._columns = tuple(preview['columns'])
            self._preview_source = source if rows else None
            self._show_preview(preview, source)
            if rows and self._columns and hf_datasets.is_training_split(source[2]):
                self._choose_fields(rows)
                self.mapping_area.pack(fill='x', pady=(4, 0))
                self._set_status('确认字段对应关系后即可导入。')
            else:
                self.mapping_area.pack_forget()
                self._set_status('导入请选择训练部分（train）。' if rows else '此划分没有返回可预览的样本。')

        self._submit('正在预览 Hugging Face 数据集',
                     lambda: hf_datasets.preview_rows(*source, cancel=self.app.cancel_event),
                     show, '无法预览样本，请重试。')

    def _show_preview(self, preview, source):
        rows = preview['rows'][:3]
        cells = preview.get('truncated_cells', ())
        detail = []
        for index, row in enumerate(rows):
            detail.append(f'第 {index + 1} 行')
            truncated = cells[index] if index < len(cells) else ()
            for column in self._columns:
                value = row.get(column)
                display = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                if len(display) > 320 or column in truncated:
                    display = display[:320] + '…'
                detail.append(f'{column}  ·  {display}')
            detail.append('')
        self.preview_text.configure(state='normal')
        self.preview_text.delete('1.0', 'end')
        self.preview_text.insert('1.0', '\n'.join(detail))
        self.preview_text.configure(state='disabled')
        shown = f'子集 {source[1]} · 划分 {source[2]}\n预览 {len(rows)} 行'
        if preview['total'] is not None:
            shown += f" · {'目前可预览' if preview.get('partial') else '共'} {preview['total']:,} 行"
        self.preview_description.set(shown)
        self.preview_area.pack(fill='x')

    def _choose_fields(self, rows):
        names = {column.casefold(): column for column in self._columns}
        suggestions = {
            'prompt': ('prompt', 'question', 'instruction', 'query', 'user', 'input', 'text'),
            'response': ('response', 'answer', 'output', 'completion', 'assistant', 'target'),
            'input': ('input', 'context', 'passage'),
        }
        used = set()
        previous = self._mapping()
        for key, variable in self.fields.items():
            self.field_boxes[key].configure(values=('', *self._columns))
            value = variable.get()
            if not previous or (value and value not in self._columns):
                value = next((names[name] for name in suggestions.get(key, ())
                              if name in names and names[name] not in used), '')
                if key == 'messages' and not (self.fields['prompt'].get() and self.fields['response'].get()):
                    value = names.get('messages', '')
                    alias = names.get('conversations')
                    if not value and alias and hf_datasets.has_chat_samples(rows, alias):
                        value = alias
            variable.set(value)
            used.add(value)

    def _import_dataset(self):
        source = self._selection()
        count = self._count()
        mapping = self._mapping()
        if (self._working() or not source or source != self._preview_source or not count or not mapping
                or not hf_datasets.is_training_split(source[2])):
            return
        self.status.set('正在读取源数据…')

        def update(progress):
            if self._pending and not self.app.cancel_event.is_set():
                fetched, limit = progress
                self.status.set(f'已读取 {fetched:,} / {limit:,} 源行，正在生成训练数据…')

        def progress(fetched, limit):
            self.app.events.put(('update', update, (fetched, limit)))

        def finish(result, error):
            if error:
                self._set_status(error)
                return
            counts = result['counts']
            skipped = sum(result['skipped'].values())
            messages = [
                f"已读取 {result['fetched']:,} 源行，跳过 {skipped:,} 行。",
                f"训练 {counts['train']:,} · 验证 {counts['validation']:,} · 测试 {counts['test']:,}"
                f" · 本地基础检查 {counts['general_test']:,}",
                str(result['path']),
            ]
            self._set_status('\n'.join(messages))
            self.on_import(result)

        self._submit('正在导入 Hugging Face 数据集',
                     lambda: hf_datasets.import_dataset(*source, count, mapping,
                                                        cancel=self.app.cancel_event, progress=progress),
                     finish, '无法导入训练数据，请重试。')
