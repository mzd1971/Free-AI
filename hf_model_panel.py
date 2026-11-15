"""Browse and download public Hugging Face GGUF models in the native client."""
import logging
import tkinter as tk
from tkinter import ttk
import webbrowser

import hf_models
from capability_manager import PackageError
from desktop_app import size_text
from ui_kit import COLORS as C, FlatButton, RoundedFrame, label


class HuggingFacePanel(tk.Frame):
    def __init__(self, app, parent):
        super().__init__(parent, bg=C['bg'])
        self.app = app
        self.query = tk.StringVar(self)
        self.search_message = tk.StringVar(self)
        self.files_message = tk.StringVar(self)
        self.file_detail = tk.StringVar(self)
        self.download_message = tk.StringVar(self)
        self._query_version = 0
        self._pending = False
        self._repo_id = None
        self._repositories = {}
        self._files = {}
        self.query.trace_add('write', self._query_changed)

        card = RoundedFrame(self, padding=(18, 14), radius=16)
        card.pack(fill='x', pady=5)
        body = card.body
        self._wrap(body, text='在 Hugging Face 搜索公开模型，查看 GGUF 文件大小。需要登录或授权的文件无法在这里下载。').pack(fill='x')

        search = tk.Frame(body, bg=C['surface'])
        search.pack(fill='x', pady=(10, 4))
        label(search, '模型名称', size=11).pack(side='left', padx=(0, 8))
        self.search_button = FlatButton(search, '搜索', self.search, variant='primary', width=70, height=34, font_size=11)
        self.search_button.pack(side='right', padx=(8, 0))
        self.entry = ttk.Entry(search, textvariable=self.query, width=12)
        self.entry.pack(side='left', fill='x', expand=True)
        self.entry.bind('<Return>', self.search)
        self._wrap(body, textvariable=self.search_message).pack(fill='x', pady=(0, 6))

        self.repository_area = tk.Frame(body, bg=C['surface'])
        self.repository_tree = self._table(self.repository_area, height=4, columns=(
            ('repository', '模型仓库', 240, True),
        ))
        self.repository_tree.bind('<<TreeviewSelect>>', self._select_repository)

        self.files_area = tk.Frame(body, bg=C['surface'])
        file_header = tk.Frame(self.files_area, bg=C['surface'])
        file_header.pack(fill='x', pady=(0, 4))
        label(file_header, 'GGUF 文件', size=11, bold=True).pack(side='left')
        self.open_button = FlatButton(file_header, '打开仓库', self._open_repository,
                                      variant='ghost', width=88, height=30, font_size=10)
        self.open_button.pack(side='right')
        self.reload_button = FlatButton(file_header, '重新读取', self._load_files,
                                        variant='ghost', width=88, height=30, font_size=10)
        self.reload_button.pack(side='right')
        self.file_tree = self._table(self.files_area, height=5, columns=(
            ('file', '文件', 160, True),
            ('size', '文件大小', 112, False),
            ('access', '状态', 100, False),
        ))
        self.file_tree.bind('<<TreeviewSelect>>', self._select_file)
        self._wrap(self.files_area, textvariable=self.files_message).pack(fill='x', pady=(5, 0))
        self._wrap(self.files_area, textvariable=self.file_detail).pack(fill='x')
        self._wrap(self.files_area, textvariable=self.download_message).pack(fill='x')
        self.download_button = FlatButton(self.files_area, '下载选中文件', self._download,
                                          variant='primary', width=132, height=34, font_size=11)
        self.download_button.pack(anchor='e', pady=(5, 0))
        self.sync_busy()

    @staticmethod
    def _wrap(parent, **kwargs):
        widget = label(parent, size=10, color=C['muted'], anchor='w', justify='left', **kwargs)
        widget.bind('<Configure>', lambda event: widget.configure(wraplength=max(1, event.width - 6)))
        return widget

    @staticmethod
    def _table(parent, height, columns):
        frame = tk.Frame(parent, bg=C['surface'])
        frame.pack(fill='x')
        tree = ttk.Treeview(frame, columns=[column[0] for column in columns],
                            show='headings', selectmode='browse', height=height)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        tree.pack(side='left', fill='x', expand=True)
        for key, title, width, stretch in columns:
            tree.heading(key, text=title, anchor='w')
            tree.column(key, width=width, minwidth=48, stretch=stretch, anchor='w')
        return tree

    def _query_changed(self, *_):
        self._query_version += 1
        self.sync_busy()

    def sync_busy(self):
        working = self.app.busy or self._pending
        self.search_button.configure(state='normal' if self.query.get().strip() and not working else 'disabled')
        self.reload_button.configure(state='normal' if self._repo_id and not working else 'disabled')
        file = self._selected_file()
        downloadable = file and file['available'] and not working and not self.app.downloading
        self.download_button.configure(state='normal' if downloadable else 'disabled')

    def _submit(self, title, work, done, fallback):
        self._pending = True
        self.sync_busy()

        def execute():
            try:
                return work(), ''
            except PackageError as exc:
                return None, str(exc) or fallback
            except Exception:
                logging.exception('Hugging Face model task failed: %s', title)
                return None, fallback

        def finish(result):
            self._pending = False
            done(*result)
            self.sync_busy()

        self.app.submit(title, execute, finish)

    def search(self, event=None):
        query = self.query.get().strip()
        if self.app.busy or self._pending or not query:
            return 'break'
        version = self._query_version
        self._repo_id = None
        self._repositories.clear()
        self.repository_tree.delete(*self.repository_tree.get_children())
        self.repository_area.pack_forget()
        self.files_area.pack_forget()
        self._clear_files()
        self.search_message.set('正在搜索模型…')

        def show(repositories, error):
            if version != self._query_version:
                self.search_message.set('关键词已改动，点击“搜索”查看新结果。')
                return
            if error:
                self.search_message.set(error)
                return
            if not repositories:
                self.search_message.set('没有找到匹配的 GGUF 模型，试试其他名称。')
                return
            for repository in repositories:
                repo_id = repository['repo_id']
                self._repositories[repo_id] = repository
                self.repository_tree.insert('', 'end', iid=repo_id, values=(repo_id,))
            self.repository_tree.configure(height=min(4, len(repositories)))
            self.repository_area.pack(fill='x')
            self.search_message.set(f'找到 {len(repositories)} 个仓库。选择仓库可查看文件。')

        self._submit('正在搜索 Hugging Face 模型', lambda: hf_models.search_models(query), show,
                     '暂时无法搜索 Hugging Face，请检查网络连接后重试。')
        return 'break'

    def _select_repository(self, event):
        selection = self.repository_tree.selection()
        if not selection or selection[0] == self._repo_id:
            return
        if self.app.busy or self._pending:
            if self._repo_id:
                self.repository_tree.selection_set(self._repo_id)
            else:
                self.repository_tree.selection_remove(*selection)
            return
        self._repo_id = selection[0]
        self.files_area.pack(fill='x', pady=(8, 0))
        self._load_files()

    def _clear_files(self):
        self._files.clear()
        self.file_tree.delete(*self.file_tree.get_children())
        self.files_message.set('')
        self.file_detail.set('')
        self.download_message.set('')

    def _load_files(self):
        repo_id = self._repo_id
        if not repo_id or self.app.busy or self._pending:
            return
        self._clear_files()
        self.files_message.set('正在读取文件…')

        def show(files, error):
            if repo_id != self._repo_id:
                return
            if error:
                self.files_message.set(error)
                return
            if not files:
                self.files_message.set('这个仓库没有列出 GGUF 文件。')
                return
            for index, file in enumerate(files):
                row = str(index)
                self._files[row] = file
                size = size_text(file['size']) if file['size'] is not None else '未知'
                self.file_tree.insert('', 'end', iid=row,
                    values=(file['filename'], size, '可下载' if file['available'] else '不可下载'))
            self.file_tree.configure(height=min(5, len(files)))
            self.files_message.set('选择要下载的文件。文件大小不含缺少的运行组件。')

        self._submit('正在读取 Hugging Face 文件', lambda: hf_models.model_files(repo_id), show,
                     '暂时无法读取这个仓库的文件，请检查网络连接后重试。')

    def _open_repository(self):
        repository = self._repositories.get(self._repo_id)
        if repository:
            webbrowser.open(repository['url'])

    def _selected_file(self):
        selection = self.file_tree.selection()
        return self._files.get(selection[0]) if selection else None

    def _select_file(self, event):
        file = self._selected_file()
        self.download_message.set('')
        if file:
            detail = file['filename'] + '\n许可：' + (file['license'] or '未标注')
            if file['reason']:
                detail += '\n' + file['reason']
            self.file_detail.set(detail)
        else:
            self.file_detail.set('')
        self.sync_busy()

    def _download(self):
        file = self._selected_file()
        if (not file or not file['available'] or self.app.busy or self._pending
                or self.app.downloading):
            return
        self.download_message.set('正在添加模型…')

        def add():
            identity = hf_models.add_model(self.app.manager, file)
            return identity, self.app.manager.ready(identity)

        def finish(result, error):
            if error:
                self.download_message.set(error)
                return
            identity, ready = result
            if ready:
                self.download_message.set('文件已下载，可在“聊天模型”中点击“使用”。')
            elif self.app.downloading:
                self.download_message.set('模型已添加。当前下载结束后，可在“聊天模型”中下载它。')
            else:
                self.app.start_download(identity)
                self.download_message.set('已提交下载。下载完成后，可在“聊天模型”中点击“使用”。')

        self._submit('正在添加 Hugging Face 模型', add, finish, '暂时无法添加模型，请重试。')
