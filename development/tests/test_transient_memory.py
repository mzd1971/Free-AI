"""Regression coverage for learning and retrieving time-sensitive memories."""

import json
import tempfile
import unittest

from agent_memory import AgentMemoryStore
from memory_learning import learn_from_turn


class TransientMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = AgentMemoryStore(temporary.name)

    def test_clock_questions_never_start_learning(self):
        for message in ('今天几号？', '搜索今天日期', '现在几点了？', "What's today's date?"):
            with self.subTest(message=message):
                calls = []

                def extract(prompt):
                    calls.append(prompt)
                    return json.dumps([
                        {'title': '今日日期', 'content': '2023年11月26日', 'kind': 'fact'},
                        {'title': '用户消息', 'content': message, 'kind': 'note'},
                    ])

                self.assertEqual(learn_from_turn(message, '今天是2023年11月26日。', extract, self.store), [])
                self.assertEqual(calls, [])
        self.assertEqual(self.store.list(), [])

    def test_empty_acknowledgments_cannot_be_saved_as_user_facts(self):
        for message in ('好的，谢谢！', '继续', 'OK, thanks!'):
            with self.subTest(message=message):
                result = learn_from_turn(message, '', lambda prompt: json.dumps([
                    {'title': '用户消息', 'content': message, 'kind': 'note'},
                ]), self.store)
                self.assertEqual(result, [])
        self.assertEqual(self.store.list(), [])

    def test_mixed_turn_keeps_stable_preferences_and_drops_clock_candidates(self):
        message = '以后请用简体中文回答。今天几号？'
        for rejected in (
            {'title': '今日日期', 'content': '2023年11月26日', 'kind': 'fact'},
            {'title': '日期资料', 'content': '今天是2023年11月26日（星期日）。', 'kind': 'fact'},
            {'title': '日历偏好', 'content': '喜欢查看阴历', 'kind': 'preference', 'evidence_quote': '今天几号？'},
        ):
            with self.subTest(candidate=rejected):
                result = learn_from_turn(message, '今天是2023年11月26日。', lambda prompt: json.dumps([
                    rejected,
                    {'title': '回答语言', 'content': '使用简体中文', 'kind': 'preference'},
                ]), self.store)
                self.assertEqual([row['title'] for row in result], ['回答语言'])
                found = self.store.before_call('搜索今天日期')
                self.assertEqual(found['memory_ids'], [result[0]['id']])
                self.assertNotIn('2023年', found['context'])
                self.store.delete(result[0]['id'])

    def test_legacy_clock_and_empty_query_entries_do_not_supply_context(self):
        old_clock = self.store.remember('今日日期', '2023 年 11 月 26 日', kind='fact')
        old_message = self.store.remember('用户消息', '搜索今天日期', kind='note')
        old_named_query = self.store.remember('今天日期是什么？', '今天日期是什么？', kind='note')
        manual = self.store.save_user('当前日期', '2001年1月1日', kind='fact', pinned=True)
        self.store.set_pinned(old_clock['id'], True)
        self.store.verify(old_clock['id'])

        old_ids = {row['id'] for row in (old_clock, old_message, old_named_query)}
        for query in ('今天几号？', '搜索今天日期', '今天日期是什么？', '日期'):
            with self.subTest(query=query):
                found = self.store.before_call(query)
                self.assertTrue(old_ids.isdisjoint(found['memory_ids']))
                if query != '日期':
                    self.assertNotIn(manual['id'], found['memory_ids'])

        self.assertCountEqual([row['id'] for row in self.store.list()],
                              old_ids | {manual['id']})
        self.assertEqual(self.store.before_call('有哪些记忆')['memory_ids'], [manual['id']])

    def test_explicit_birthday_historical_event_and_project_rule_are_learned(self):
        for title, message, kind in (
            ('生日', '记住，我的生日是11月26日。', 'fact'),
            ('上线日期', '我们的项目在2023年11月26日上线。', 'fact'),
            ('成立日期', '公司成立于2020年2月29日。', 'fact'),
            ('当前日期', '这个项目以后统一用 ISO 8601 格式显示当前日期。', 'procedure'),
        ):
            with self.subTest(title=title):
                result = learn_from_turn(message, '', lambda prompt: json.dumps([
                    {'title': title, 'content': message, 'kind': kind},
                ]), self.store)
                self.assertEqual([row['title'] for row in result], [title])
                self.assertIn(result[0]['id'], self.store.before_call(title)['memory_ids'])


if __name__ == '__main__':
    unittest.main()
