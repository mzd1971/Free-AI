"""Persistent source metadata and completion blockers for project web tools."""

import json
from pathlib import Path
import tempfile
import unittest

from agent_task_state import OBSERVATION_LIMIT, STATE_FILE, STATE_LIMIT, WEB_FAILURE_LIMIT, TaskJournal, load_task


class WebTaskStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.workspace = root / 'project'
        self.workspace.mkdir()
        self.runs = root / 'runs'
        self.run = self.runs / 'first'
        self.run.mkdir(parents=True)
        self.journal = TaskJournal(self.run, self.workspace, '核实资料来源')

    def resume(self, name):
        self.journal.save(status='unfinished')
        parent = load_task(self.runs, self.run, self.workspace)
        self.run = self.runs / name
        self.run.mkdir()
        self.journal = TaskJournal(self.run, self.workspace, '继续', parent_run=parent.export()['run_id'])
        self.journal.inherit(parent)

    def test_failures_survive_observation_eviction_and_resumes_until_matched(self):
        first = {'query': '版本变更笔记', 'limit': 1}
        second = {'query': 'API 改动公告'}
        address = {'url': 'https://example.org/changelog', 'max_chars': 200}
        failure = {'ok': False, 'message': '服务超时'}
        self.journal.observe('web_search', first, failure)
        self.journal.observe('web_search', second, failure)
        self.journal.observe('web_fetch', address, failure)
        for index in range(OBSERVATION_LIMIT + 1):
            self.journal.observe('read_file', {'path': f'notes-{index}'}, {'ok': True})

        for name in ('second', 'third'):
            self.resume(name)
            self.assertTrue(all(row['tool'] == 'read_file' for row in self.journal.export()['observations']))
            blocker, = self.journal.completion_blockers()
            for target in (first['query'], second['query'], address['url']):
                self.assertIn(target, blocker)
                self.assertIn(target, self.journal.handoff())

        self.journal.observe('web_search', {'query': '其他资料'}, {'ok': True, 'results': []})
        self.journal.observe('web_search', {'query': address['url']}, {'ok': True, 'results': []})
        self.journal.observe('run_command', {'argv': ['unit'], 'purpose': 'check'}, {
            'status': 'exited', 'returncode': 0,
        })
        self.assertEqual(len(self.journal.export()['failed_web']), 3)
        self.journal.observe('web_search', {'query': '  版本变更笔记  ', 'limit': 5}, {'ok': True, 'results': []})
        self.journal.observe('web_fetch', {**address, 'max_chars': 1500}, {'ok': True, **address})
        self.assertEqual([row['subject'] for row in self.journal.export()['failed_web']], [second['query']])
        self.journal.observe('web_search', second, failure)
        self.assertTrue(self.journal.completion_blockers())
        self.journal.observe('web_search', second, {'ok': True, 'results': []})
        self.assertEqual(self.journal.completion_blockers(), [])
        self.journal.save(status='completed')
        self.assertEqual(load_task(self.runs, self.run, self.workspace).export(), self.journal.export())

    def test_successful_tool_call_clears_only_its_own_missing_subject_failures(self):
        failure = {'ok': False, 'message': '工具参数无效'}
        real_query = {'query': '有效查询'}
        for tool, malformed in (('web_search', {'query': 3}), ('web_fetch', {'url': []})):
            self.journal.observe(tool, {}, failure)
            self.journal.observe(tool, malformed, failure)
        self.journal.observe('web_search', real_query, failure)
        self.resume('second')
        self.journal.observe('web_search', {'query': '修正搜索'}, {'ok': True, 'results': []})
        remaining = self.journal.export()['failed_web']
        self.assertEqual([(row['tool'], row['subject']) for row in remaining], [
            ('web_fetch', ''), ('web_search', real_query['query']),
        ])
        url = 'https://example.org/notes'
        self.journal.observe('web_fetch', {'url': url}, {'ok': True, 'url': url})
        self.assertEqual([row['subject'] for row in self.journal.export()['failed_web']], [real_query['query']])
        self.assertTrue(self.journal.completion_blockers())
        self.journal.observe('web_search', real_query, {'ok': True, 'results': []})
        self.assertEqual(self.journal.completion_blockers(), [])

    def test_success_summaries_survive_resume_and_exclude_bodies_and_credentials(self):
        self.journal.observe('web_search', {'query': '版本迁移'}, {'ok': True, 'results': [
            {'title': '迁移指南', 'url': 'https://example.org/guide?page=2', 'snippet': '搜索正文不落盘'},
            {'title': '参考手册', 'url': 'https://example.org/reference?X-Amz-Signature=opaque-secret'},
        ]})
        target = 'https://user-secret@example.org/request?%74oken=opaque-token'
        self.journal.observe('web_fetch', {'url': target}, {'ok': True, 'title': '版本公告',
            'url': 'https://example.org/changelog', 'text': '网页正文不落盘'})
        for index in range(5):
            self.journal.observe('read_file', {'path': f'notes-{index}'}, {'ok': True})
        self.journal.save()
        data = (self.run / STATE_FILE).read_text(encoding='utf-8')
        for secret in ('opaque-secret', 'opaque-token', 'user-secret', '网页正文不落盘', '搜索正文不落盘'):
            self.assertNotIn(secret, data)
        prior = load_task(self.runs, self.run, self.workspace)
        for expected in ('版本迁移', '迁移指南', '参考手册', '版本公告',
                         'https://example.org/guide?page=2', 'https://example.org/changelog'):
            self.assertIn(expected, prior.handoff())

    def test_matching_web_retry_resolves_an_interrupted_call(self):
        target = {'url': 'https://example.org/changelog', 'max_chars': 200}
        self.journal.begin('web_fetch', target)
        prior = load_task(self.runs, self.run, self.workspace)
        self.assertIn(target['url'], prior.handoff())
        path = self.runs / 'second'
        path.mkdir()
        next_run = TaskJournal(path, self.workspace, '继续', parent_run=self.run.name)
        next_run.inherit(prior)
        next_run.observe('web_fetch', {**target, 'max_chars': 500}, {'ok': True, **target})
        self.assertEqual(next_run.completion_blockers(), [])

    def test_bounded_failure_index_stays_blocked_after_overflow(self):
        for index in range(WEB_FAILURE_LIMIT + 1):
            self.journal.observe('web_search', {'query': f'资料-{index}'}, {'ok': False})
        self.assertEqual(len(self.journal.export()['failed_web']), WEB_FAILURE_LIMIT)
        self.journal.save()
        raw = (self.run / STATE_FILE).read_bytes()
        self.assertLessEqual(len(raw), STATE_LIMIT)
        self.assertEqual(json.loads(raw)['version'], 3)
        self.resume('second')
        self.journal.confirm_recovery()
        for index in range(1, WEB_FAILURE_LIMIT + 1):
            self.journal.observe('web_search', {'query': f'资料-{index}'}, {'ok': True, 'results': []})
        self.assertEqual(self.journal.export()['failed_web'], [])
        self.assertIn('部分失败已无法逐项核实', ' '.join(self.journal.completion_blockers()))


if __name__ == '__main__':
    unittest.main()
