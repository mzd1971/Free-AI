"""Regression coverage for interrupted and unsuccessful project operations."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_task_state import STATE_FILE, TaskJournal, load_task
from project_agent import ProjectAgent
from test_project_agent import ScriptedModel


DEVELOPMENT = Path(__file__).resolve().parents[1]
VERSION_CHECK = {'argv': [sys.executable, '-V'], 'purpose': 'check'}


class TaskRecoveryEdges(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.workspace = root / 'project'
        self.workspace.mkdir()
        self.runs = root / 'runs'
        self.runs.mkdir()
        self.events = []

    def agent(self, *turns):
        return ProjectAgent(self.workspace, ScriptedModel(*turns), runs_root=self.runs,
                            approve_command=lambda request: True, on_event=self.events.append)

    def journal(self, name, previous=None, *, parent=None):
        directory = self.runs / name
        directory.mkdir()
        journal = TaskJournal(directory, self.workspace, 'Apply and verify the change.',
                              previous.export()['run_id'] if previous else parent)
        if previous:
            journal.inherit(previous)
        return journal

    def legacy_checkpoint(self, journal, phase):
        journal.save(phase=phase)
        path = self.runs / journal.export()['run_id'] / STATE_FILE
        state = json.loads(path.read_text(encoding='utf-8'))
        state['version'] = 1
        for field in ('inflight', 'unresolved', 'failed_writes'):
            del state[field]
        for check in state['checks']:
            del check['argv_id']
        path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        return path

    def test_readback_recovers_edit_committed_before_interruption(self):
        source = self.workspace / 'sample.txt'
        source.write_text('before', encoding='utf-8')
        read = [('read_file', {'path': source.name})]
        edit = [('replace_text', {'path': source.name, 'old': 'before', 'new': 'after'})]
        original = self.agent(read, edit)
        observe = original._observe

        def interrupt_after_write(name, arguments, result):
            if name == 'replace_text' and result.get('changed'):
                raise KeyboardInterrupt('fixture interruption after atomic write')
            return observe(name, arguments, result)

        with patch.object(original, '_observe', side_effect=interrupt_after_write):
            with self.assertRaises(KeyboardInterrupt):
                original.run('Apply the edit.')

        self.assertEqual(source.read_text(encoding='utf-8'), 'after')
        checkpoint = original.run_dir / STATE_FILE
        original_bytes = checkpoint.read_bytes()
        pending = load_task(self.runs, original.run_dir, self.workspace).export()
        self.assertTrue(pending['uncertain'])
        self.assertEqual([row['tool'] for row in pending['unresolved']], ['replace_text'])

        resumed = self.agent(read, 'The change is present.').run(
            'Continue.', resume_from=original.run_dir)
        self.assertEqual(resumed['status'], 'completed')
        self.assertFalse(resumed['task_state']['uncertain'])
        self.assertEqual(resumed['task_state']['unresolved'], [])
        self.assertEqual(resumed['task_state']['edits'], [
            {'path': source.name, 'sha256': hashlib.sha256(b'after').hexdigest()},
        ])
        self.assertEqual(resumed['changes'], [])
        self.assertEqual(checkpoint.read_bytes(), original_bytes)

    def test_different_secret_arguments_cannot_clear_interrupted_check(self):
        first_token, other_token = 'fixture-token-first', 'fixture-token-other'
        first = {'argv': ['fixture-tool', '--token', first_token], 'purpose': 'check'}
        other = {'argv': ['fixture-tool', '--token', other_token], 'purpose': 'check'}
        original = self.journal('interrupted')
        original.begin('run_command', first)
        resumed = self.journal('resumed', load_task(self.runs, 'interrupted', self.workspace))
        success = {'ok': True, 'status': 'exited', 'returncode': 0}

        resumed.begin('run_command', other)
        resumed.observe('run_command', other, success)
        resumed.save()
        state = load_task(self.runs, 'resumed', self.workspace).export()
        self.assertTrue(state['uncertain'])
        self.assertEqual(state['verification']['status'], 'blocked')
        self.assertEqual([row['status'] for row in state['verification']['checks']], ['pending', 'exited'])
        checkpoint = (self.runs / 'resumed' / STATE_FILE).read_text(encoding='utf-8')
        self.assertNotIn(first_token, checkpoint)
        self.assertNotIn(other_token, checkpoint)

        resumed.begin('run_command', first)
        resumed.observe('run_command', first, success)
        resumed.save()
        self.assertFalse(resumed.completion_blockers())

    def test_failed_write_blocks_until_same_file_is_repaired(self):
        source = self.workspace / 'sample.txt'
        source.write_text('before', encoding='utf-8')
        edit = [('replace_text', {'path': source.name, 'old': 'before', 'new': 'after'})]
        failed = self.agent(edit, 'Done.', 'Done.').run('Apply the edit.')
        self.assertEqual(failed['status'], 'unfinished')
        self.assertEqual(failed['task_state']['failed_writes'], [source.name])
        self.assertEqual(source.read_text(encoding='utf-8'), 'before')

        read = [('read_file', {'path': source.name})]
        repaired = self.agent(read, edit, 'Applied.').run('Continue.', resume_from=failed['run_dir'])
        self.assertEqual(repaired['status'], 'completed')
        self.assertEqual(repaired['task_state']['failed_writes'], [])
        self.assertEqual(source.read_text(encoding='utf-8'), 'after')

    def test_readback_also_clears_earlier_failed_write_for_same_path(self):
        path = 'sample.txt'
        edit = {'path': path, 'old': 'before', 'new': 'after'}
        original = self.journal('interrupted-retry')
        original.observe('replace_text', edit, {'ok': False})
        original.begin('replace_text', edit)
        digest = hashlib.sha256(b'after').hexdigest()
        original.write_intent(path, digest)

        resumed = self.journal('resume-retry', load_task(self.runs, 'interrupted-retry', self.workspace))
        self.assertEqual(resumed.export()['failed_writes'], [path])
        resumed.observe('read_file', {'path': path}, {'ok': True, 'path': path, 'sha256': digest})
        resumed.save()
        self.assertEqual(resumed.export()['failed_writes'], [])
        self.assertFalse(resumed.completion_blockers())

    def test_pending_plan_cannot_be_cleared_by_an_empty_update(self):
        step = {'text': 'Inspect the module', 'status': 'pending'}
        plan = [('update_plan', {'steps': [step]})]
        clear = [('update_plan', {'steps': []})]
        result = self.agent(plan, clear, 'Done.', 'Done.').run('Inspect the module.')
        self.assertEqual(result['status'], 'unfinished')
        self.assertEqual(result['task_state']['plan'], [step])
        updates = [event['result'] for event in self.events
                   if event['type'] == 'tool_result' and event['tool'] == 'update_plan']
        self.assertEqual([update['ok'] for update in updates], [True, False])

    def test_legacy_missing_parent_can_be_confirmed_without_waiving_checks(self):
        journal = self.journal('legacy', parent='unavailable-parent')
        journal.observe('run_command', VERSION_CHECK, {'status': 'exited', 'returncode': 1})
        checkpoint = self.legacy_checkpoint(journal, phase='executing')
        original_bytes = checkpoint.read_bytes()
        restored = load_task(self.runs, 'legacy', self.workspace).export()
        self.assertTrue(restored['uncertain'])
        self.assertEqual(restored['parent_run'], 'unavailable-parent')
        self.assertEqual(restored['verification']['checks'][0]['returncode'], 1)
        self.assertTrue(any(row['status'] == 'unavailable' for row in restored['observations']))

        unconfirmed = self.agent('Done.', 'Done.').run('Continue.', resume_from='legacy')
        self.assertEqual(unconfirmed['status'], 'unfinished')
        self.assertTrue(unconfirmed['task_state']['uncertain'])

        confirmed = self.agent('Done.', 'Done.').run(
            'Continue.', resume_from='legacy', recovery_confirmed=True)
        self.assertEqual(confirmed['status'], 'unfinished')
        self.assertFalse(confirmed['task_state']['uncertain'])
        self.assertEqual(confirmed['task_state']['verification']['status'], 'stale')
        self.assertTrue(any(row['tool'] == 'user_confirmation'
                            for row in confirmed['task_state']['observations']))

        check = [('run_command', VERSION_CHECK)]
        finished = self.agent(check, 'Verified.').run('Continue.', resume_from=confirmed['run_dir'])
        self.assertEqual(finished['status'], 'completed')
        self.assertEqual(finished['task_state']['verification']['status'], 'passed')
        self.assertEqual(checkpoint.read_bytes(), original_bytes)

    def test_legacy_descendant_inherits_uncertainty_from_missing_ancestor(self):
        first = self.journal('legacy-first', parent='missing-legacy')
        second = self.journal('legacy-second', parent='legacy-first')
        third = self.journal('legacy-third', parent='legacy-second')
        for journal in (first, second, third):
            self.legacy_checkpoint(journal, phase='ready')

        descendant = load_task(self.runs, 'legacy-third', self.workspace)
        self.assertTrue(descendant.export()['uncertain'])
        self.assertTrue(descendant.completion_blockers())
        result = self.agent('Done.', 'Done.').run('Continue.', resume_from='legacy-third')
        self.assertEqual(result['status'], 'unfinished')
        self.assertTrue(result['task_state']['uncertain'])

    def test_same_check_can_recover_from_invalid_working_directory(self):
        invalid = {**VERSION_CHECK, 'cwd': 'missing-directory'}
        result = self.agent([('run_command', invalid)], [('run_command', VERSION_CHECK)], 'Verified.').run(
            'Check the Python version.')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['task_state']['verification']['status'], 'passed')
        self.assertEqual([row['returncode'] for row in result['task_state']['verification']['checks']], [0])

    def test_other_working_directory_cannot_waive_refused_or_failed_check(self):
        for label, failed in (('denied', {'status': 'denied'}),
                              ('exited', {'status': 'exited', 'returncode': 1})):
            with self.subTest(status=label):
                journal = self.journal(f'check-{label}')
                journal.observe('run_command', {**VERSION_CHECK, 'cwd': 'subdir'}, failed)
                journal.observe('run_command', VERSION_CHECK, {'status': 'exited', 'returncode': 0})
                self.assertTrue(journal.completion_blockers())
                self.assertEqual([row['status'] for row in journal.export()['verification']['checks']],
                                 [label, 'exited'])

    def test_escaped_surrogate_is_rejected_as_a_tool_error_then_repaired(self):
        invalid = [('write_file', {'path': 'sample.txt', 'content': '\ud800'})]
        valid = [('write_file', {'path': 'sample.txt', 'content': 'ready'})]
        agent = self.agent(invalid, valid, 'Saved.')
        complete_tools = agent.model.complete_tools

        def model_reply(*args, **kwargs):
            reply = complete_tools(*args, **kwargs)
            for call in reply.get('tool_calls', []):
                function = call['function']
                function['arguments'] = json.dumps(json.loads(function['arguments']), ensure_ascii=True)
            return reply

        with patch.object(agent.model, 'complete_tools', side_effect=model_reply):
            result = agent.run('Write the file.')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((self.workspace / 'sample.txt').read_text(encoding='utf-8'), 'ready')
        outcomes = [event['result']['ok'] for event in self.events
                    if event['type'] == 'tool_result' and event['tool'] == 'write_file']
        self.assertEqual(outcomes, [False, True])
        self.assertFalse(result['task_state']['failed_writes'])


if __name__ == '__main__':
    unittest.main()
