"""Validate the read-only retrieval preview against live prompt assembly."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_memory import AgentMemoryStore, HEADER


class MemoryRetrievalPreviewTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='memory-preview-', dir=Path(__file__).resolve().parents[1])
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.store = AgentMemoryStore(self.root, clock=lambda: self.now)

    def database_snapshot(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            return tuple(db.iterdump())

    def assert_preview_matches_live(self, query, preview, **kwargs):
        actual = self.store.before_call(query, **kwargs)
        self.assertEqual(actual, {name: preview[name] for name in ('context', 'memory_ids')})
        return actual

    def test_scope_is_applied_before_exposing_items_even_with_an_allowlist(self):
        first = self.root / 'first'
        second = self.root / 'second'
        global_note = self.store.save_user('build global', '共享构建说明', scope='global')
        assistant = self.store.remember('build personal', '此助手的构建说明')
        project = self.store.remember('build project', '甲项目构建说明', scope='project', workspace=first)
        other_project = self.store.remember('build second', '乙项目独立内容', scope='project', workspace=second)
        other_assistant = self.store.remember('build separate', '其他助手独立内容', agent_id='other')
        other_agent_project = self.store.remember(
            'build alternate', '同项目其他助手独立内容', scope='project', agent_id='other', workspace=first,
        )
        foreign = (other_project, other_assistant, other_agent_project)
        foreign_ids = {row['id'] for row in foreign}
        cases = (
            (None, {global_note['id'], assistant['id'], project['id']}),
            (foreign_ids | {project['id']}, {project['id']}),
            (foreign_ids, set()),
        )
        for allowed, expected in cases:
            with self.subTest(allowed=allowed):
                options = {'workspace': first / '.', 'allowed_ids': allowed}
                preview = self.store.preview_call('build', **options)
                self.assertCountEqual([row['id'] for row in preview['items']], expected)
                self.assertCountEqual(preview['memory_ids'], expected)
                diagnostic = json.dumps(preview, ensure_ascii=False)
                for row in foreign:
                    for key in ('id', 'title', 'content'):
                        self.assertNotIn(row[key], diagnostic)
                self.assert_preview_matches_live('build', preview, **options)

    def test_preview_predicts_expiry_without_writing_or_repairing_legacy_state(self):
        archived = self.store.remember('build archived', '已归档的构建说明', importance=1)
        self.now += timedelta(days=900)
        self.assertEqual(self.store.maintain(), {'archived': 1})
        expired = self.store.remember('build expired', '超过保留期的构建说明')
        legacy = self.store.remember('build legacy', '旧记录的构建说明')
        self.store.mark_used([legacy['id']])
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('UPDATE memories SET retention_state=? WHERE id=?', ('', legacy['id']))
        self.now += timedelta(days=100)
        before = self.database_snapshot()

        with patch.object(self.store, 'maintain', side_effect=AssertionError('preview called maintain')):
            preview = self.store.preview_call('build')
            self.assertEqual(self.store.preview_call('build'), preview)

        self.assertEqual(self.database_snapshot(), before)
        self.assertFalse(self.store.get(expired['id'])['archived'])
        self.assertEqual(self.store.get(legacy['id'])['access_count'], 1)
        rows = {row['id']: row for row in preview['items']}
        self.assertEqual(rows[archived['id']]['reason_code'], 'archived')
        self.assertEqual(rows[expired['id']]['reason_code'], 'expired')
        self.assertEqual(rows[legacy['id']]['reason_code'], 'included')
        self.assertEqual(preview['memory_ids'], [legacy['id']])
        self.assert_preview_matches_live('build', preview)
        self.assertTrue(self.store.get(expired['id'])['archived'])

    def test_priority_never_overrides_inactive_or_transient_exclusions(self):
        pending = self.store.remember('build pending', '等待审核的构建流程', pending=True, importance=1)
        disabled = self.store.remember('build disabled', '已停用的构建流程', importance=1)
        clock = self.store.remember('系统日期', '2026年5月1日', kind='fact', importance=1)
        acknowledgment = self.store.remember('本轮用户输入', '好的', kind='preference', importance=1)
        self.store.set_status(disabled['id'], 'disabled')
        for row in (pending, disabled, clock, acknowledgment):
            self.store.set_pinned(row['id'], True)
        kept = self.store.remember('build selected', '仍在使用的构建流程')

        expected = {pending['id']: 'pending', disabled['id']: 'disabled',
                    clock['id']: 'transient', acknowledgment['id']: 'transient'}
        for query in ('build', '列出长期记忆'):
            with self.subTest(query=query):
                preview = self.store.preview_call(query)
                rows = {row['id']: row for row in preview['items']}
                for identity, reason_code in expected.items():
                    row = rows[identity]
                    self.assertEqual(row['reason_code'], reason_code)
                    self.assertIs(row['selected'], False)
                    self.assertEqual(row['fragment'], '')
                self.assertEqual(preview['memory_ids'], [kept['id']])
                self.assert_preview_matches_live(query, preview)

    def test_diagnostics_capture_ranking_budget_and_preview_only_controls(self):
        strongest = self.store.remember('构建 build', 'build ' + '多字节🧪构建过程说明。' * 100)
        title_match = self.store.remember('build checklist', '逐项校对构建结果')
        body_match = self.store.remember('检查说明', 'build 检查结果')
        unrelated = self.store.remember('旋律', '音高与节奏')
        lexical_ids = [strongest['id'], title_match['id'], body_match['id']]

        normal = self.store.preview_call('build')
        self.assertEqual(normal['memory_ids'], lexical_ids)
        self.assertEqual(normal['minimum_match'], 1)
        self.assertIsNone(normal['max_items'])
        rows = {row['id']: row for row in normal['items']}
        eligible = [rows[identity] for identity in lexical_ids]
        self.assertTrue(all(row['selected'] and row['reason_code'] == 'included' for row in eligible))
        self.assertEqual([row['match_score'] for row in eligible], [4, 3, 1])
        self.assertEqual([row['matched_terms'] for row in eligible], [['build']] * 3)
        self.assertLess(eligible[0]['rank'], eligible[1]['rank'])
        self.assertLess(eligible[1]['rank'], eligible[2]['rank'])
        self.assertEqual(rows[unrelated['id']]['reason_code'], 'no_match')
        self.assertTrue(all(row['reason'] and 0 <= row['retention_score'] <= 1 for row in normal['items']))
        self.assertEqual(normal['context'], HEADER + ''.join(row['fragment'] for row in eligible))
        self.assert_preview_matches_live('build', normal)

        budget = len(HEADER.encode('utf-8')) + 101
        narrow = self.store.preview_call('build', budget=budget)
        rows = {row['id']: row for row in narrow['items']}
        truncated = rows[strongest['id']]['fragment']
        self.assertEqual(narrow['memory_ids'], [strongest['id']])
        self.assertEqual(narrow['context'], HEADER + truncated)
        self.assertIn('…', truncated)
        self.assertNotIn('\ufffd', truncated)
        self.assertEqual(narrow['budget'], budget)
        self.assertEqual(narrow['used_bytes'], len(narrow['context'].encode('utf-8')))
        self.assertLessEqual(narrow['used_bytes'], budget)
        for identity in lexical_ids[1:]:
            self.assertEqual(rows[identity]['reason_code'], 'budget')
            self.assertEqual(rows[identity]['fragment'], '')
        self.assert_preview_matches_live('build', narrow, budget=budget)

        limited = self.store.preview_call('build', max_items=1, minimum_match=2)
        rows = {row['id']: row for row in limited['items']}
        self.assertEqual(limited['memory_ids'], [strongest['id']])
        self.assertEqual((limited['max_items'], limited['minimum_match']), (1, 2))
        self.assertEqual(rows[title_match['id']]['reason_code'], 'item_limit')
        self.assertEqual(rows[body_match['id']]['reason_code'], 'match_threshold')
        self.assertEqual(rows[strongest['id']]['fragment'], eligible[0]['fragment'])
        self.assert_preview_matches_live('build', normal)

    def test_pin_preference_and_overview_keep_keyword_exemptions(self):
        ordinary = self.store.remember('构建日志', '记录任务耗时')
        preference = self.store.remember('答复语气', '采用简洁表述', kind='preference')
        pinned = self.store.save_user('书写规则', '使用正式书面语', pinned=True)

        high_threshold = self.store.preview_call('构建日志', minimum_match=100)
        self.assertEqual(high_threshold['memory_ids'], [pinned['id'], preference['id']])
        overview = self.store.preview_call('列出长期记忆', minimum_match=100)
        self.assertCountEqual(overview['memory_ids'], [ordinary['id'], preference['id'], pinned['id']])

    def test_validation_rejects_invalid_limits_without_changing_the_database(self):
        invalid = {
            'max_items': (True, False, 0, -1, 101, 10**50, 1.0, float('inf'), '1'),
            'minimum_match': (True, False, None, 0, -1, 101, 10**50, 1.0, float('nan'), '1'),
            'budget': (True, False, -1, 1.0, float('inf'), '20'),
            'allowed_ids': ('id', 1),
        }
        before = self.database_snapshot()
        for name, values in invalid.items():
            for value in values:
                with self.subTest(argument=name, value=value), self.assertRaises(ValueError):
                    self.store.preview_call('build', **{name: value})
        with self.assertRaises(ValueError):
            self.store.preview_call(None)
        self.assertEqual(self.database_snapshot(), before)
        preview = self.store.preview_call('build', budget=0, max_items=100, minimum_match=100)
        self.assertEqual((preview['context'], preview['memory_ids'], preview['used_bytes']), ('', [], 0))


if __name__ == '__main__':
    unittest.main()
