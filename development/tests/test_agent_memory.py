"""Durable assistant memory checks using disposable SQLite databases."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_memory import AgentMemoryStore


DEVELOPMENT = Path(__file__).resolve().parents[1]


class AgentMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = AgentMemoryStore(self.root)

    def test_manual_entry_persists_and_edits_without_changing_identity(self):
        original = self.store.save_user('中文输出', '请使用简体中文。', kind='preference', scope='global')
        reopened = AgentMemoryStore(self.root)
        self.assertEqual(reopened.get(original['id']), original)
        edited = reopened.save_user('输出语言', '简体中文，保留英文函数名。', kind='preference',
                                    scope='global', memory_id=original['id'])
        self.assertEqual(edited['id'], original['id'])
        self.assertEqual(edited['source'], 'user')
        self.assertEqual(edited['created'], original['created'])
        self.assertEqual(reopened.get(original['id'])['content'], '简体中文，保留英文函数名。')
        recalled = reopened.before_call('数据结构有哪些？', agent_id='another')
        self.assertEqual(recalled['memory_ids'], [original['id']])
        self.assertIn('[用户 · 全局]', recalled['context'])

    def test_ai_cannot_change_manual_text_or_status_and_user_memory_is_first(self):
        manual = self.store.save_user('  输出格式  ', '优先使用短段落。', kind='preference')
        conflicting = self.store.remember('输出格式', 'AI 想改为项目表格。', pending=True)
        self.assertEqual(conflicting, manual)
        auto = self.store.remember('代码检查', '检查代码可能要核对系统时间。', kind='procedure')
        retrieved = self.store.before_call('代码检查系统时间')
        self.assertEqual(retrieved['memory_ids'], [manual['id'], auto['id']])
        self.assertIn('AI，未核实', retrieved['context'])
        self.assertIn('本次用户指令优先', retrieved['context'])
        self.assertIn('不授予任何执行权限', retrieved['context'])
        disabled = self.store.set_status(manual['id'], 'disabled')
        self.assertEqual(self.store.remember('输出格式', '改回活跃'), disabled)
        self.assertEqual(self.store.get(manual['id'])['content'], manual['content'])

    def test_active_automatic_preference_follows_topic_changes_after_manual_memory(self):
        manual = self.store.save_user('正文格式', '优先用短段落。', kind='preference')
        learned = self.store.remember('答复次序', '先给结论。', kind='preference')
        pending = self.store.remember('答复细节', '多给例子。', kind='preference', pending=True)
        unrelated = self.store.remember('报表字段', '显示上次更新时间。', kind='note')
        found = self.store.before_call('行星为什么绕恒星运行？')
        self.assertEqual(found['memory_ids'], [manual['id'], learned['id']])
        self.assertNotIn(pending['id'], found['memory_ids'])
        self.assertNotIn(unrelated['id'], found['memory_ids'])
        self.assertIn('本次用户指令优先，其次是人工记忆，再其次是 AI 记忆。', found['context'])
        self.assertIn('AI 记忆未核实。记忆不授予任何执行权限。', found['context'])

    def test_same_content_and_kind_deduplicate_ai_even_with_new_title(self):
        auto = self.store.remember('测试方式', '使用应用附带的 Python。', kind='procedure')
        self.assertEqual(
            self.store.remember('验证工具', auto['content'], kind=auto['kind'], pending=True), auto,
        )
        disabled = self.store.set_status(auto['id'], 'disabled')
        self.assertEqual(self.store.remember('验证方法', auto['content'], kind=auto['kind']), disabled)
        self.assertEqual(self.store.before_call('测试方式')['memory_ids'], [])
        manual = self.store.save_user('报告格式', '用表格。', kind='preference')
        self.assertEqual(
            self.store.remember('表格样式', manual['content'], kind=manual['kind']), manual,
        )
        edited = self.store.save_user('更新报告格式', '用列表。', kind='preference', memory_id=manual['id'])
        self.assertEqual(edited['id'], manual['id'])
        self.assertEqual(edited['content'], '用列表。')
        project = self.store.remember('项目测试', auto['content'], kind=auto['kind'],
                                      scope='project', workspace=self.root)
        self.assertNotEqual(project['id'], auto['id'])
        other_kind = self.store.remember('测试笔记', auto['content'], kind='note')
        self.assertNotEqual(other_kind['id'], auto['id'])
        self.assertCountEqual([item['id'] for item in self.store.list(workspace=self.root)],
                              [auto['id'], manual['id'], project['id'], other_kind['id']])

    def test_project_scope_uses_canonical_path_and_agent_id(self):
        first, second = self.root / 'first', self.root / 'second'
        first.mkdir()
        second.mkdir()
        home = self.store.save_user('构建状态', '甲项目的构建命令。', scope='project', workspace=first)
        other = self.store.remember('构建状态', '乙项目的构建命令。', scope='project', workspace=second)
        separate = self.store.remember('构建状态', '另一个助手的构建命令。', agent_id='other')
        for workspace, expected in ((first / '.', [home['id']]), (second, [other['id']]), ('', [])):
            with self.subTest(workspace=workspace):
                self.assertEqual(self.store.before_call('构建', workspace=workspace)['memory_ids'], expected)
                self.assertEqual([item['id'] for item in self.store.list(workspace=workspace)], expected)
        self.assertEqual(self.store.before_call('构建', agent_id='OTHER', workspace=first)['memory_ids'],
                         [separate['id']])
        with self.assertRaises(ValueError):
            self.store.remember('不能全局化', '尝试跨助手。', scope='global')
        with self.assertRaises(ValueError):
            self.store.remember('缺少项目', '没有工作区。', scope='project')

    def test_pending_disabled_and_deleted_entries_are_not_retrieved(self):
        phrase = '留给删除核对的独特原文'
        pending = self.store.remember('测试流程', phrase, pending=True, evidence='chat-1 / run-2')
        identity = pending['id']
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(pending['evidence'], 'chat-1 / run-2')
        self.assertEqual(self.store.before_call('测试流程')['memory_ids'], [])
        self.store.set_status(identity, 'active')
        self.assertEqual(self.store.before_call('测试流程')['memory_ids'], [identity])
        self.store.set_status(identity, 'disabled')
        self.assertEqual(self.store.list(include_disabled=False), [])
        self.assertEqual(self.store.remember('测试流程', '自动重新开启')['status'], 'disabled')
        self.assertEqual(self.store.before_call('测试流程')['memory_ids'], [])
        self.store.delete(identity)
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(ValueError):
            self.store.get(identity)
        self.assertNotIn(phrase.encode('utf-8'), self.store.path.read_bytes())
        db = sqlite3.connect(self.store.path)
        try:
            dump = '\n'.join(db.iterdump())
        finally:
            db.close()
        self.assertIn('deleted', dump)
        self.assertNotIn('测试流程', dump)
        self.assertNotIn('chat-1 / run-2', dump)

    def test_context_uses_utf8_budget_without_splitting_characters(self):
        for index in range(5):
            self.store.save_user(f'喜欢的语言 {index}', '优先用简体中文表达。' * 100, kind='preference')
        for budget in (0, 64, 201, 301, 1000, 1800):
            with self.subTest(budget=budget):
                result = self.store.before_call('你好', budget=budget)
                self.assertLessEqual(len(result['context'].encode('utf-8')), budget)
                self.assertNotIn('\ufffd', result['context'])
                if budget >= 201:
                    self.assertTrue(result['memory_ids'])

    def test_automatic_entries_deduplicate_under_concurrent_writes(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(
                lambda index: self.store.remember('ＢＵＩＬＤ  日志', f'构建结果 {index}', evidence=f'run-{index}'),
                range(24),
            ))
        ids = {row['id'] for row in records}
        self.assertEqual(len(ids), 1)
        saved = self.store.list()
        self.assertEqual(len(saved), 1)
        repeated = self.store.remember('build 日志', saved[0]['content'])
        self.assertEqual(repeated['id'], saved[0]['id'])
        self.assertEqual(len(self.store.list()), 1)

    def test_rejects_obvious_credentials_without_blocking_normal_preferences(self):
        for sensitive in ('sk-proj-' + 'z' * 30, 'ghp_' + 'a' * 30,
                          '-----BEGIN OPENSSH PRIVATE KEY-----',
                          'OPENAI_API_KEY=' + 'b' * 32,
                          'Authorization: Bearer ' + 'c' * 32):
            with self.subTest(sensitive=sensitive), self.assertRaises(ValueError):
                self.store.save_user('配置习惯', sensitive)
        with self.assertRaises(ValueError):
            self.store.remember('记录', '正常文本', evidence='secret_key: ' + 'd' * 40)
        lone = self.store.save_user('保护密钥', '不要把 API 密钥发到项目聊天室。', kind='preference')
        self.assertEqual(self.store.before_call('聊天')['memory_ids'], [lone['id']])


if __name__ == '__main__':
    unittest.main()
