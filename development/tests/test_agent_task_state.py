"""Checkpoint persistence and resume validation for project tasks."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_task_state import CHECK_LIMIT, STATE_FILE, STATE_LIMIT, TaskJournal, load_task


class TaskJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.runs = self.root / 'runs'
        self.run = self.runs / 'run-one'
        self.run.mkdir(parents=True)
        self.path = self.run / STATE_FILE
        self.journal = TaskJournal(self.run, self.workspace, '修复项目脚本')

    def check(self, command, code=0, status='exited', **result):
        self.journal.observe('run_command', {'argv': [command], 'purpose': 'check'}, {
            'status': status, 'returncode': code, 'ok': status == 'exited' and code == 0, **result,
        })

    def status(self):
        return self.journal.export()['verification']['status']

    def test_roundtrip_and_new_run_parent(self):
        self.journal.plan(['读取源码', {'text': '验证脚本', 'status': 'in_progress'}])
        self.journal.observe('replace_text', {'path': 'script.py', 'old': '原文', 'new': '替换内容'}, {
            'ok': True, 'path': 'script.py', 'changed': True, 'sha256': 'a' * 64,
        })
        self.check('test-command', stdout='三项成功')
        self.journal.save(status='completed')
        loaded = load_task(self.runs, self.run, self.workspace)
        self.assertEqual(loaded.export(), self.journal.export())
        self.assertIn('三项成功', loaded.handoff())
        self.assertIn('script.py', loaded.handoff())
        self.assertNotIn('替换内容', self.path.read_text(encoding='utf-8'))
        self.assertEqual(load_task(self.runs, self.run.name, self.workspace).export(), loaded.export())
        with self.assertRaises(ValueError):
            loaded.save()
        child = self.runs / 'run-two'
        child.mkdir()
        child_journal = TaskJournal(child, self.workspace, '继续任务', self.run)
        child_journal.inherit(loaded)
        child_journal.save(status='unfinished')
        child_state = load_task(self.runs, child, self.workspace).export()
        self.assertEqual(child_state['parent_run'], self.run.name)
        self.assertEqual(child_state['status'], 'unfinished')
        self.assertEqual(child_state['task'], loaded.export()['task'])
        self.assertEqual(child_state['plan'], loaded.export()['plan'])
        self.assertEqual(child_state['edits'], loaded.export()['edits'])
        self.assertEqual(child_state['observations'], loaded.export()['observations'])
        self.assertEqual(child_state['verification']['status'], 'stale')
        child_journal.observe('run_command', {
            'argv': ['test-command'], 'cwd': str(self.workspace), 'purpose': 'check',
        }, {'status': 'exited', 'returncode': 0})
        self.assertEqual(child_journal.export()['verification']['status'], 'passed')

    def test_checks_follow_observed_exit_codes_and_edits(self):
        self.journal.observe('run_command', {'argv': ['other']}, {'status': 'exited', 'returncode': 0})
        self.assertEqual(self.status(), 'not_run')
        self.check('ignored', code=None, status='denied')
        self.assertEqual(self.status(), 'blocked')
        self.check('ignored')
        self.check('unit', code=1, stderr='断言失败')
        self.assertEqual(self.status(), 'failed')
        self.assertIn('断言失败', self.journal.handoff())
        self.check('integration')
        self.assertEqual(self.status(), 'failed')
        self.check('unit')
        self.assertEqual(self.status(), 'passed')
        self.journal.observe('write_file', {'path': 'same.py'}, {'ok': True, 'changed': False})
        self.assertEqual(self.status(), 'passed')
        self.journal.observe('write_file', {'path': 'same.py'}, {'ok': True, 'changed': True})
        self.assertEqual(self.status(), 'stale')
        self.check('unit')
        self.assertEqual(self.status(), 'stale')
        self.check('integration')
        self.check('ignored')
        self.assertEqual(self.status(), 'passed')
        self.journal.observe('run_command', {'argv': ['other']}, {'status': 'exited', 'returncode': 1})
        self.assertEqual(self.status(), 'stale')
        self.check('integration', code=1, status='timeout')
        self.assertEqual(self.status(), 'failed')

    def test_bounded_checks_do_not_hide_earlier_failure(self):
        self.check('unit', code=1)
        for index in range(CHECK_LIMIT + 2):
            self.check(f'check-{index}')
        self.assertEqual(self.status(), 'failed')
        self.assertEqual(len(self.journal.export()['verification']['checks']), CHECK_LIMIT)
        self.journal.observe('write_file', {'path': 'fixed.py'}, {'ok': True, 'changed': True})
        self.check('unrelated')
        self.assertEqual(self.status(), 'stale')
        self.check('unit')
        self.assertEqual(self.status(), 'stale')
        for index in range(CHECK_LIMIT + 2):
            self.check(f'check-{index}')
        self.assertEqual(self.status(), 'passed')

    def test_initial_and_interrupted_checkpoints(self):
        self.journal.save()
        self.assertEqual(load_task(self.runs, self.run, self.workspace).export()['verification']['status'],
                         'not_run')
        self.check('unit')
        self.journal.save(phase='executing')
        interrupted = load_task(self.runs, self.run, self.workspace)
        self.assertEqual(interrupted.export()['phase'], 'executing')
        self.assertTrue(interrupted.export()['uncertain'])
        self.assertEqual(interrupted.export()['verification']['status'], 'stale')
        self.assertIn('是否实际执行及其副作用尚不确定', interrupted.handoff())

    def test_uncertainty_survives_resume_chain_until_matching_check(self):
        self.journal.begin('run_command', {'argv': ['known'], 'purpose': 'check'})
        previous = load_task(self.runs, self.run, self.workspace)
        self.assertEqual(previous.export()['verification']['status'], 'blocked')
        for name in ('run-two', 'run-three'):
            path = self.runs / name
            path.mkdir()
            self.journal = TaskJournal(path, self.workspace, '继续任务', previous.export()['run_id'])
            self.journal.inherit(previous)
            self.journal.save(status='unfinished')
            previous = load_task(self.runs, path, self.workspace)
            self.assertTrue(previous.export()['uncertain'])
            self.assertEqual(previous.export()['phase'], 'ready')
            self.assertIn('是否实际执行及其副作用尚不确定', previous.handoff())
        self.journal.observe('run_command', {'argv': ['ordinary']}, {'status': 'exited', 'returncode': 0})
        self.assertTrue(self.journal.export()['uncertain'])
        self.check('known', code=1)
        self.assertTrue(self.journal.export()['uncertain'])
        self.check('other')
        self.assertTrue(self.journal.export()['uncertain'])
        self.assertEqual(self.status(), 'failed')
        self.check('known')
        self.assertFalse(self.journal.export()['uncertain'])
        self.assertEqual(self.status(), 'passed')
        self.journal.save(status='unfinished')
        self.assertFalse(load_task(self.runs, path, self.workspace).export()['uncertain'])

    def test_legacy_resume_chain_recovers_metadata_without_changing_old_files(self):
        def old_file(journal):
            journal.save(status='unfinished')
            path = journal._run_dir / STATE_FILE
            data = json.loads(path.read_text(encoding='utf-8'))
            data.update(version=1)
            del data['inflight'], data['unresolved'], data['failed_writes']
            for check in data.get('checks', []):
                check.pop('argv_id', None)
            path.write_text(json.dumps(data), encoding='utf-8')
            return path, path.read_bytes()

        self.journal.observe('write_file', {'path': 'early.py'},
                             {'ok': True, 'changed': True, 'sha256': 'a' * 64})
        snapshots = [old_file(self.journal)]
        prior = self.run.name
        for name in ('old-two', 'old-three'):
            path = self.runs / name
            path.mkdir()
            child = TaskJournal(path, self.workspace, 'original task', parent_run=prior)
            snapshots.append(old_file(child))  # v1 children lost edits/observations.
            prior = name
        loaded = load_task(self.runs, prior, self.workspace)
        self.assertEqual([row['path'] for row in loaded.export()['edits']], ['early.py'])
        self.assertIn('early.py', loaded.handoff())
        self.assertTrue(loaded.export()['observations'])
        for path, data in snapshots:
            self.assertEqual(path.read_bytes(), data)

    def test_unknown_legacy_interruption_cannot_be_cleared_by_an_unrelated_check(self):
        self.journal.save(phase='executing')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        data.update(version=1)
        del data['inflight'], data['unresolved'], data['failed_writes']
        for check in data.get('checks', []):
            check.pop('argv_id', None)
        self.path.write_text(json.dumps(data), encoding='utf-8')
        prior = load_task(self.runs, self.run, self.workspace)
        child_path = self.runs / 'child'
        child_path.mkdir()
        child = TaskJournal(child_path, self.workspace, 'continue', parent_run=self.run.name)
        child.inherit(prior)
        child.observe('run_command', {'argv': ['unrelated'], 'purpose': 'check'},
                      {'status': 'exited', 'returncode': 0})
        self.assertTrue(child.export()['uncertain'])
        self.assertTrue(child.completion_blockers())

    def test_rejects_invalid_schema_and_unsafe_paths(self):
        self.journal.save()
        original = self.path.read_bytes()
        foreign = self.root / 'foreign'
        foreign.mkdir()
        with self.assertRaises(ValueError):
            load_task(self.runs, self.run, foreign)
        with self.assertRaises(ValueError):
            load_task(self.runs, self.runs / 'elsewhere' / '..' / self.run.name, self.workspace)
        with self.assertRaises(ValueError):
            load_task(self.runs, self.run / 'child', self.workspace)
        wrong_run = self.runs / 'wrong-run'
        wrong_run.mkdir()
        (wrong_run / STATE_FILE).write_bytes(original)
        with self.assertRaises(ValueError):
            load_task(self.runs, wrong_run, self.workspace)
        for malformed in (
                b'{', b'\xff', b'x' * (STATE_LIMIT + 1),
                original.replace(b'"version":2', b'"version":999'),
                original.replace(b'"version":2', b'"version":2,"version":2'),
                original.replace(b'"plan":[]', b'"plan":[{"text":"bad","status":[]}]')):
            with self.subTest(length=len(malformed), prefix=malformed[:18]):
                self.path.write_bytes(malformed)
                with self.assertRaises(ValueError):
                    load_task(self.runs, self.run, self.workspace)

    def test_rejects_hardlinked_checkpoint_and_keeps_previous_on_replace_error(self):
        self.journal.save()
        original = self.path.read_bytes()
        with patch('project_agent.os.replace', side_effect=PermissionError('fixture')):
            with self.assertRaises(PermissionError):
                self.journal.save(phase='executing')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.journal.export()['phase'], 'ready')
        self.assertEqual(list(self.run.iterdir()), [self.path])
        link = self.root / 'hardlink.json'
        os.link(self.path, link)
        with self.assertRaises(ValueError):
            load_task(self.runs, self.run, self.workspace)
        with self.assertRaises(ValueError):
            self.journal.save()
        self.assertEqual(link.read_bytes(), original)

    def test_rejects_symlink_run_and_checkpoint(self):
        self.journal.save()
        reparse = self.runs / 'linked-run'
        if os.name == 'nt':
            from _winapi import CreateJunction

            CreateJunction(str(self.run), str(reparse))
        else:
            reparse.symlink_to(self.run, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            load_task(self.runs, reparse, self.workspace)
        target = self.root / 'task-copy.json'
        self.path.rename(target)
        if os.name == 'nt':
            CreateJunction(str(self.run), str(self.path))
        else:
            self.path.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            load_task(self.runs, self.run, self.workspace)

    def test_limits_utf8_and_redacts_progress(self):
        secret = 'sk-proj-' + 'a' * 30
        generic = 'short-password'
        journal = TaskJournal(self.run, self.workspace, f'处理 {secret} ' + '汉🙂' * 8000)
        for _ in range(20):
            journal.observe('read_file', {'path': 'secret.txt'}, {'ok': True, 'text': secret})
            journal.observe('run_command', {'argv': ['check', '--password', generic], 'purpose': 'check'}, {
                'status': 'exited', 'returncode': 1, 'stderr': f'password="{generic}" ' + '异常🙂' * 4000,
            })
        journal.save()
        data = self.path.read_bytes()
        self.assertLessEqual(len(data), STATE_LIMIT)
        self.assertNotIn(secret.encode('ascii'), data)
        self.assertNotIn(generic.encode('ascii'), data)
        self.assertIn('处理', json.loads(data)['task'])
        for budget in (0, 1, 2, 100, 550, 2500):
            self.assertLessEqual(len(journal.handoff(budget).encode('utf-8')), budget)

    def test_handoff_preserves_multiple_sections_under_load(self):
        self.journal.plan(['执行项目验证，确认实现完整' * 8] * 12)
        self.journal.observe('write_file', {'path': 'script.py'}, {'ok': True, 'changed': True})
        for index in range(5):
            self.check('check-' + str(index), code=index, stderr='需要进一步诊断' * 100)
        handoff = self.journal.handoff(1400)
        for expected in ('修复项目脚本', '退出码 4', '执行项目验证', 'script.py'):
            self.assertIn(expected, handoff)


if __name__ == '__main__':
    unittest.main()
