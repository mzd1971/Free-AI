"""Native review interface for evidence-backed, domain-independent knowledge."""
import tkinter as tk
from tkinter import ttk


STATUS = {'candidate': '待确认建议', 'active': '正在使用', 'suspended': '已停用', 'superseded': '已替换'}
VERIFY = {'unverified': '尚未核验', 'execution_only': '只确认工具成功运行', 'objective': '已用客观结果核验', 'user_reported': '你已反馈'}
OUTCOME = {'pending': '待确认', 'success': '成功', 'failure': '失败'}
DOMAIN = {
    'general': '常见问题',
    'arithmetic': '算术计算',
    'programming': '编程',
    'writing': '写作',
    'environment': '电脑环境',
    'training': '教 AI 学习',
}
DOMAIN_VALUE = {label: value for value, label in DOMAIN.items()}
AUTHOR = {'user': '你填写的', 'model_proposal': 'AI 总结的', 'metrics_diagnostic': '根据学习数据总结的'}


def domain_label(value):
    """Translate built-in source values while leaving user-defined problem types untouched."""
    return DOMAIN.get(value, value)


def domain_value(value):
    return DOMAIN_VALUE.get(value, value)


class EpistemologyPanel:
    def __init__(self, app, page):
        self.app, self.store = app, app.knowledge
        self.episodes, self.rules = {}, {}
        self.selected_episode = None
        ttk.Label(page, text='实际使用 → 你的反馈 → 可复用做法 → 下次自动查找',
                  font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w', pady=(0, 5))
        ttk.Label(page, text='覆盖编程、写作、电脑环境、教 AI 学习等问题；新建议经你确认后才会使用，也可以随时修正。',
                  foreground='#63728b').pack(anchor='w')
        self.notebook = ttk.Notebook(page)
        self.notebook.pack(fill='both', expand=True, pady=8)
        self.practice_page = ttk.Frame(self.notebook, padding=6)
        self.rules_page = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.practice_page, text='使用记录与反馈')
        self.notebook.add(self.rules_page, text='可用做法与待确认建议')
        # The host hides its navigation tabs; restore tabs for this local notebook.
        style = ttk.Style(page)
        style.layout('Knowledge.TNotebook.Tab', [('Notebook.tab', {'sticky': 'nswe', 'children': [
            ('Notebook.padding', {'side': 'top', 'sticky': 'nswe', 'children': [
                ('Notebook.label', {'side': 'top', 'sticky': ''})]})]})])
        style.configure('Knowledge.TNotebook.Tab', padding=(16, 8))
        style.configure('Knowledge.Treeview', rowheight=29, font=('Microsoft YaHei UI', 11))
        self.notebook.configure(style='Knowledge.TNotebook')
        self._practice()
        self._rules()
        self.notice = tk.StringVar(value='先在“中文聊天”或“自动办事”中完成一次任务，再选择记录进行反馈。')
        ttk.Label(page, textvariable=self.notice, foreground='#63728b', wraplength=950).pack(side='bottom', fill='x', pady=(4, 0), before=self.notebook)
        app.root.after_idle(self.refresh)

    def _text(self, parent, height, editable=False):
        text = self.app.text(parent, height=height, editable=editable)
        text.pack_configure(expand=False, pady=3)
        text.configure(width=60, padx=9, pady=6)
        return text

    def _practice(self):
        left = ttk.Frame(self.practice_page, width=260)
        left.pack(side='left', fill='y', padx=(0, 14))
        left.pack_propagate(False)
        ttk.Label(left, text='实际使用记录').pack(anchor='w')
        self.episode_tree = ttk.Treeview(left, columns=('task',), show='headings', height=10,
                                       selectmode='browse', style='Knowledge.Treeview')
        self.episode_tree.heading('task', text='选择一条使用记录')
        self.episode_tree.column('task', width=240, stretch=True)
        self.episode_tree.pack(fill='both', expand=True, pady=6)
        self.episode_tree.bind('<<TreeviewSelect>>', self._episode_selected)
        row = ttk.Frame(left)
        row.pack(fill='x', pady=4)
        self.app.button(row, '刷新记录', self.refresh)
        self.app.button(row, '查看可用做法', lambda: self.notebook.select(self.rules_page))
        row = ttk.Frame(left)
        row.pack(fill='x', pady=4)
        self.app.button(row, '撤回所选记录', self._retire_episode)
        right = ttk.Frame(self.practice_page)
        right.pack(side='left', fill='both', expand=True)
        self.episode_hint = tk.StringVar(value='尚未选择使用记录')
        ttk.Label(right, textvariable=self.episode_hint, foreground='#63728b').pack(anchor='w')
        self.original = self._text(right, 3)
        row = ttk.Frame(right)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text='反馈').pack(side='left')
        self.outcome = tk.StringVar(value='纠正错误')
        ttk.Combobox(row, textvariable=self.outcome, values=('纠正错误', '确认结果正确'),
                     state='readonly', width=13).pack(side='left', padx=(6, 16))
        ttk.Label(row, text='问题类型').pack(side='left')
        self.domain = tk.StringVar(value=DOMAIN['general'])
        ttk.Entry(row, textvariable=self.domain, width=18).pack(side='left', padx=6)
        ttk.Label(right, text='正确答案 / 正确做法（纠错必填；确认成功时可留空）').pack(anchor='w')
        self.correction = self._text(right, 2, True)
        ttk.Label(right, text='适用条件与关键词（例如：Python 列表 遍历时删除元素）').pack(anchor='w')
        self.scope = tk.StringVar()
        ttk.Entry(right, textvariable=self.scope).pack(fill='x', pady=3)
        ttk.Label(right, text='可复用做法：什么情况下、怎么做、如何检查').pack(anchor='w')
        self.lesson = self._text(right, 2, True)
        row = ttk.Frame(right)
        row.pack(fill='x', pady=(5, 0))
        self.app.button(row, '只保存反馈', lambda: self._save(False))
        self.app.button(row, '反馈并存为待确认建议', lambda: self._save(True))
        row = ttk.Frame(right)
        row.pack(fill='x', pady=5)
        self.app.button(row, '让 AI 总结成建议', self._propose_model)
        ttk.Label(row, text='先保存反馈；AI 总结后仍需你确认。', foreground='#63728b').pack(side='left')

    def _rules(self):
        self.rule_tree = ttk.Treeview(self.rules_page, columns=('status', 'domain', 'scope'),
                                      show='headings', height=4, selectmode='browse', style='Knowledge.Treeview')
        for name, title, width in [('status', '使用状态', 100), ('domain', '问题类型', 130), ('scope', '适用条件', 580)]:
            self.rule_tree.heading(name, text=title)
            self.rule_tree.column(name, width=width, stretch=name == 'scope')
        self.rule_tree.pack(fill='x', pady=(0, 6))
        self.rule_tree.bind('<<TreeviewSelect>>', self._rule_selected)
        self.rule_detail = self._text(self.rules_page, 3)
        row = ttk.Frame(self.rules_page)
        row.pack(fill='x', pady=4)
        self.app.button(row, '确认并使用所选建议', self._activate)
        self.app.button(row, '停用所选做法', self._disable)
        ttk.Label(self.rules_page, text='修正做法：选择当前正在使用的做法，再用“使用记录与反馈”中选定的记录作为依据。').pack(anchor='w', pady=(6, 3))
        row = ttk.Frame(self.rules_page)
        row.pack(fill='x')
        ttk.Label(row, text='问题类型').pack(side='left')
        self.replace_domain = tk.StringVar()
        ttk.Entry(row, textvariable=self.replace_domain, width=16).pack(side='left', padx=(6, 12))
        ttk.Label(row, text='新适用条件').pack(side='left')
        self.replace_scope = tk.StringVar()
        ttk.Entry(row, textvariable=self.replace_scope).pack(side='left', fill='x', expand=True, padx=6)
        self.replace_lesson = self._text(self.rules_page, 2, True)
        row = ttk.Frame(self.rules_page)
        row.pack(fill='x', pady=4)
        self.app.button(row, '用新依据替换所选做法', self._replace)
        self.app.button(row, '返回使用记录与反馈', lambda: self.notebook.select(self.practice_page))
        row = ttk.Frame(self.rules_page)
        row.pack(fill='x', pady=5)
        self.export_details = None
        self.app.button(row, '导出待确认的教学内容', self._export)
        self.app.button(row, '查看导出详情', lambda: self.app.show_details('导出详细记录', self.export_details))
        ttk.Label(row, text='只导出待确认内容；仍需审核、去重和分组，不会自动开始教 AI 学习。',
                  foreground='#63728b', wraplength=300).pack(side='left')

    @staticmethod
    def _selection(tree):
        selected = tree.selection()
        return selected[0] if selected else None

    @staticmethod
    def _require(identity, what):
        if not identity:
            raise ValueError('请先选择' + what + '。')
        return identity

    def refresh(self, episode_id=None):
        target = episode_id or self._selection(self.episode_tree) or self.selected_episode
        rule_id = self._selection(self.rule_tree)
        def work():
            episodes = self.store.recent(120)
            if target and not any(r['id'] == target for r in episodes):
                episodes.insert(0, self.store.episode(target))
            return episodes, self.store.rules()
        def done(result):
            self.episodes = {r['id']: r for r in result[0]}
            self.rules = {r['id']: r for r in result[1]}
            self.episode_tree.delete(*self.episode_tree.get_children())
            self.rule_tree.delete(*self.rule_tree.get_children())
            for identity, row in self.episodes.items():
                self.episode_tree.insert('', 'end', iid=identity, values=(row['task'].replace('\n', ' ')[:60],))
            for identity, row in self.rules.items():
                self.rule_tree.insert('', 'end', iid=identity, values=(STATUS.get(row['status'], row['status']), domain_label(row['domain']), row['scope']))
            choice = target if target in self.episodes else next(iter(self.episodes), None)
            if choice:
                self.episode_tree.selection_set(choice)
                self.episode_tree.see(choice)
                self._episode_selected()
            if rule_id in self.rules:
                self.rule_tree.selection_set(rule_id)
                self._rule_selected()
        self.app.submit('读取使用记录与做法', work, done)

    def _episode_selected(self, event=None):
        identity = self._selection(self.episode_tree)
        if identity not in self.episodes:
            return
        row = self.episodes[identity]
        changed = identity != self.selected_episode
        self.selected_episode = identity
        state = {'active': '可用记录', 'withdrawn': '已撤回', 'superseded': '已被新反馈替换'}.get(row.get('memory_state'), '')
        self.episode_hint.set(f"{domain_label(row['domain'])} · {OUTCOME.get(row['outcome'], row['outcome'])} · {VERIFY.get(row['verification'], row['verification'])} · {state}")
        self.app.write(self.original, f"任务：{row['task']}\n\n当时的回答：{row['answer']}\n\n核验依据：{row['evidence']}\n记录编号：{identity}")
        if changed:
            self.domain.set(domain_label(row['domain']))
            self.outcome.set('确认结果正确' if row['outcome'] == 'success' else '纠正错误')
            self.correction.delete('1.0', 'end')
            self.correction.insert('1.0', row['correction'])
            self.scope.set('')
            self.lesson.delete('1.0', 'end')

    def _rule_selected(self, event=None):
        identity = self._selection(self.rule_tree)
        if identity not in self.rules:
            return
        row = self.rules[identity]
        self.app.write(self.rule_detail, f"{STATUS.get(row['status'], row['status'])} · {domain_label(row['domain'])} · 来源：{AUTHOR.get(row['author'], row['author'])}\n"
                       f"适用条件：{row['scope']}\n可用做法：{row['lesson']}\n依据记录：{len(row['evidence_ids'])} 条\n做法编号：{identity}")
        self.replace_domain.set(domain_label(row['domain']))
        self.replace_scope.set(row['scope'])
        self.replace_lesson.delete('1.0', 'end')
        self.replace_lesson.insert('1.0', row['lesson'])

    def _finish(self, message, episode_id=None):
        self.notice.set(message)
        self.refresh(episode_id)

    def _save(self, candidate):
        identity = self.selected_episode
        correction = self.correction.get('1.0', 'end').strip()
        scope, lesson = self.scope.get().strip(), self.lesson.get('1.0', 'end').strip()
        domain = domain_value(self.domain.get().strip())
        outcome = 'success' if self.outcome.get() == '确认结果正确' else 'failure'
        def work():
            self._require(identity, '使用记录')
            if candidate and (not scope or not lesson or len(scope) > 250 or len(lesson) > 1000):
                raise ValueError('请填写适用条件（最多250字）和可复用做法（最多1000字）。')
            self.store.feedback(identity, correction=correction, outcome=outcome)
            if candidate:
                return self.store.propose([identity], scope, lesson, domain=domain or None)
            return None
        self.app.submit('保存反馈与依据', work, lambda result: self._finish(
            '反馈已保存；新建议还没有启用，请到“可用做法与待确认建议”确认。' if result else '反馈已保存，以后可用于查找和总结做法。', identity))

    def _propose_model(self):
        identity = self.selected_episode
        def work():
            self._require(identity, '使用记录')
            from epistemology import propose_with_model
            from qwen_chat import chat
            return propose_with_model(self.store, [identity], lambda prompt: chat(prompt, use_memory=False))
        self.app.submit('AI 正在总结建议', work, lambda result: self._finish(
            'AI 已给出一条待确认建议，请核对适用范围、做法和检查方法；它没有自动启用。', identity))

    def _activate(self):
        identity = self._selection(self.rule_tree)
        def work():
            self._require(identity, '待确认建议')
            self.store.activate(identity)
        self.app.submit('确认并使用建议', work, lambda _: self._finish('这条做法已开始使用；遇到相似任务时会按适用条件查找。'))

    def _disable(self):
        identity = self._selection(self.rule_tree)
        def work():
            self._require(identity, '做法')
            self.store.disable(identity)
        self.app.submit('停用做法', work, lambda _: self._finish('所选做法已停用，遇到相似任务时不会再使用。'))

    def _retire_episode(self):
        identity = self.selected_episode
        def work():
            self._require(identity, '使用记录')
            self.store.retire_episode(identity)
        self.app.submit('撤回记录', work, lambda _: self._finish('记录已撤回，相关做法也已停用；原始记录仍保留，但以后不会使用或导出。'))

    def _replace(self):
        old_id, episode = self._selection(self.rule_tree), self.selected_episode
        scope, lesson = self.replace_scope.get().strip(), self.replace_lesson.get('1.0', 'end').strip()
        domain = domain_value(self.replace_domain.get().strip())
        def work():
            self._require(old_id, '要替换的可用做法')
            self._require(episode, '作为新依据的使用记录')
            if self.store.rule(old_id)['status'] != 'active':
                raise ValueError('只能替换当前正在使用的做法；待确认建议请先确认。')
            new_id = self.store.propose([episode], scope, lesson, domain=domain or None)
            self.store.activate(new_id, replaces=old_id)
            return new_id
        self.app.submit('用新依据替换做法', work, lambda _: self._finish('新做法已开始使用；旧版本会保留，方便以后查明修改过程。'))

    def _export(self):
        def done(result):
            self.export_details = result
            self.notice.set(f"已导出 {result['rows']} 条待确认内容，保存到：{result['path']}。没有开始教 AI 学习。")
            self.app.write(self.rule_detail, f"导出完成\n\n数量：{result['rows']} 条\n保存位置：{result['path']}\n\n完整技术数据可点“查看导出详情”。")
        self.app.submit('导出待确认的教学内容', self.store.export_candidates, done)
