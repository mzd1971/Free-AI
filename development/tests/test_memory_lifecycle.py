"""Exercise persistent memory aging with a clock, without waiting or loading a model."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_memory import AgentMemoryStore
from memory_learning import learn_from_turn


class MemoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.store = AgentMemoryStore(self.temp.name, clock=lambda: self.now)

    def advance(self, days):
        self.now += timedelta(days=days)

    def test_auto_extraction_is_persistent_and_old_memory_is_soft_archived(self):
        import json
        message = '我的项目使用 pytest 检查代码。'
        saved = learn_from_turn(message, '已记录', lambda _: json.dumps([
            {'title': '项目测试', 'content': message, 'kind': 'fact', 'evidence_quote': message}
        ]), self.store)
        identity = saved[0]['id']
        self.assertIn(identity, self.store.before_call('项目测试')['memory_ids'])
        self.advance(70)
        self.assertNotIn(identity, self.store.before_call('项目测试')['memory_ids'])
        self.assertTrue(self.store.get(identity)['archived'])
        reopened = AgentMemoryStore(self.temp.name, clock=lambda: self.now)
        self.assertEqual(reopened.get(identity)['content'], message)
        self.assertEqual(reopened.maintain(), {'archived': 0})
        self.assertEqual(reopened.list(include_archived=False), [])

    def test_importance_and_explicit_user_protection(self):
        ordinary = self.store.remember('规则普通', '规则甲')
        important = self.store.remember('规则重要', '规则乙', importance=.8)
        manual = self.store.save_user('手工规则', '规则丙')
        verified = self.store.remember('核实规则', '规则丁')
        pinned = self.store.remember('置顶规则', '规则戊')
        self.store.verify(verified['id'])
        self.store.set_pinned(pinned['id'], True)
        self.advance(70)
        self.assertEqual(self.store.maintain(), {'archived': 1})
        self.assertTrue(self.store.get(ordinary['id'])['archived'])
        self.assertFalse(self.store.get(important['id'])['archived'])
        self.advance(430)
        self.assertEqual(self.store.maintain(), {'archived': 1})
        for item in (manual, verified, pinned):
            self.assertFalse(self.store.get(item['id'])['archived'])
        restored = self.store.restore(important['id'])
        self.assertFalse(restored['archived'])
        self.assertFalse(restored['verified'])
        self.assertIn(important['id'], self.store.before_call('规则重要')['memory_ids'])

    def test_using_memory_does_not_turn_a_claim_into_evidence(self):
        row = self.store.remember('待核实规则', '规则未经核实')
        for _ in range(5):
            self.store.mark_used(self.store.before_call('待核实规则')['memory_ids'])
        used = self.store.get(row['id'])
        self.assertEqual(used['access_count'], 5)
        self.assertEqual(used['retention_state'], row['retention_state'])
        self.assertFalse(used['verified'])
        self.advance(70)
        self.store.maintain()
        self.store.mark_used([row['id']])
        self.assertEqual(self.store.get(row['id'])['access_count'], 5)

    def test_user_change_during_maintenance_is_not_overwritten(self):
        row = self.store.remember('规则', '待核实')
        self.advance(70)
        old_list = self.store.list()
        self.store.verify(row['id'])
        with patch.object(self.store, 'list', return_value=old_list):
            self.assertEqual(self.store.maintain(), {'archived': 0})
        self.assertFalse(self.store.get(row['id'])['archived'])

    def test_ai_cannot_replace_archived_text_or_silently_restore_it(self):
        original = self.store.remember('构建命令', '运行旧命令', evidence='第一次用户原话')
        self.advance(70)
        self.store.maintain()
        archived = self.store.get(original['id'])
        returned, changed = self.store.remember('构建命令', '运行新命令', evidence='新的提取',
                                                return_changed=True)
        self.assertFalse(changed)
        self.assertEqual(returned, archived)
        self.assertTrue(returned['archived'])
        restored = self.store.restore(original['id'])
        self.assertEqual(restored['content'], original['content'])
        self.assertEqual(restored['evidence'], original['evidence'])

    def test_internal_allowlist_cannot_bypass_scope_or_restore_archived_text(self):
        own = self.store.remember('训练结果', '本次验收需核实')
        other = self.store.remember('训练结果', '另一个助手的数据', agent_id='other')
        found = self.store.before_call('训练结果', allowed_ids={own['id'], other['id']})
        self.assertEqual(found['memory_ids'], [own['id']])
        self.advance(70)
        self.assertEqual(self.store.before_call('训练结果', allowed_ids={own['id']})['memory_ids'], [])

    def test_importance_validation_and_user_edits_preserve_priority(self):
        row = self.store.save_user('输出格式', '中文', importance=.8)
        edited = self.store.save_user('输出格式', '中文短句', memory_id=row['id'])
        self.assertEqual(edited['importance'], .8)
        self.assertEqual(self.store.remember('输出格式', '英文', importance=1), edited)
        for value in (True, -1, 2, float('nan'), float('inf'), '重要'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.set_importance(row['id'], value)


if __name__ == '__main__':
    unittest.main()
