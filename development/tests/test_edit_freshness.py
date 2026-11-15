"""Check optimistic project edits through the real agent and filesystem."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import project_agent
from project_agent import ProjectAgent
from test_project_agent import ScriptedModel


DEVELOPMENT = Path(__file__).resolve().parents[1]
FILE = 'sample.txt'
OVERWRITE = ('write_file', {'path': FILE, 'content': 'replacement\n'})


def read(offset=0, limit=8192):
    return 'read_file', {'path': FILE, 'offset': offset, 'limit': limit}


def replace(old='broken', new='repaired'):
    return 'replace_text', {'path': FILE, 'old': old, 'new': new}


class PagingModel(ScriptedModel):
    def complete_tools(self, messages, tools, *, cancel_event, context):
        if callable(self.turns[0]):
            self.turns[0] = self.turns[0](messages)
        return super().complete_tools(messages, tools, cancel_event=cancel_event, context=context)


class EditFreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        self.project = folder / 'project'
        self.project.mkdir()
        self.runs = folder / 'runs'
        self.file = self.project / FILE
        self.events = []

    def run_agent(self, *turns, callback=None, **options):
        self.model = PagingModel(*turns, '操作结束。')

        def observe(event):
            self.events.append(event)
            if callback is not None:
                callback(event)

        agent = ProjectAgent(self.project, self.model, runs_root=self.runs, approve_command=None,
                            on_event=observe, **options)
        result = agent.run('按指定步骤操作并保留外部编辑。')
        self.assertEqual(result['status'], 'completed', result)
        return result

    def results(self, name):
        return [event['result'] for event in self.events if event['type'] == 'tool_result'
                and event['tool'] == name]

    def test_blind_and_same_batch_read_overwrites_are_refused(self):
        self.file.write_bytes(b'original\n')
        outcome = self.run_agent([OVERWRITE], [read(), OVERWRITE], [OVERWRITE])
        attempts = self.results('write_file')
        self.assertEqual([row['ok'] for row in attempts], [False, False, True])
        self.assertIn('read_file', attempts[0]['message'])
        self.assertEqual(len(outcome['changes']), 1)
        self.assertEqual(self.file.read_bytes(), b'replacement\n')

    def test_complete_paging_required_for_overwrite(self):
        self.file.write_bytes(b'one two three')
        self.run_agent([read(limit=4)], [OVERWRITE], [read(offset=4)], [OVERWRITE])
        blocked, passed = self.results('write_file')
        self.assertIn('尚未完整读取', blocked['message'])
        self.assertTrue(passed['changed'])

    def test_utf8_tool_output_clipping_counts_only_delivered_bytes(self):
        original = ('甲🙂乙\n' * 450).encode('utf-8')
        self.file.write_bytes(original)

        def read_next_page(messages):
            observations = [json.loads(row['content']) for row in messages if row['role'] == 'tool']
            self.first_page = next(row for row in reversed(observations) if 'next_offset' in row)
            return [read(offset=self.first_page['next_offset'])]

        self.run_agent([read()], [OVERWRITE], read_next_page, [OVERWRITE], context=16384)
        self.assertEqual(self.results('read_file')[0]['content'].encode('utf-8'), original)
        self.assertIsInstance(self.first_page['next_offset'], int)
        visible = self.first_page['content'].encode('utf-8')
        self.assertEqual(self.first_page['next_offset'], len(visible))
        self.assertEqual(visible, original[:len(visible)])
        self.assertLess(len(visible), len(original))
        self.assertEqual([row['ok'] for row in self.results('write_file')], [False, True])

    def test_eof_only_read_does_not_grant_replace(self):
        self.file.write_bytes(b'broken')
        self.run_agent([read(offset=6)], [replace()])
        attempt, = self.results('replace_text')
        self.assertFalse(attempt['ok'])
        self.assertEqual(self.file.read_bytes(), b'broken')

    def test_replace_requires_visible_target_and_preserves_partial_limit(self):
        original = b'broken prefix hidden suffix\n'
        self.file.write_bytes(original)
        outcome = self.run_agent(
            [read(limit=7)], [replace('hidden', 'lost')], [replace()], [OVERWRITE],
        )
        rejected, replaced = self.results('replace_text')
        self.assertFalse(rejected['ok'])
        self.assertTrue(replaced['changed'])
        self.assertFalse(self.results('write_file')[0]['ok'])
        self.assertEqual(self.file.read_bytes(), original.replace(b'broken', b'repaired'))
        self.assertEqual(len(outcome['changes']), 1)

    def test_external_append_refuses_stale_replace_then_reread_preserves_it(self):
        original = 'broken\n'.encode('utf-8')
        concurrent = '外部编辑追加的注释\n'.encode('utf-8')
        self.file.write_bytes(original)

        def append_once(event):
            if event['type'] == 'tool_result' and event['tool'] == 'read_file':
                if len(self.results('read_file')) == 1:
                    with self.file.open('ab') as stream:
                        stream.write(concurrent)

        outcome = self.run_agent([read()], [replace()], [read()], [replace()], callback=append_once)
        stale, success = self.results('replace_text')
        self.assertFalse(stale['ok'])
        self.assertIn('已变化', stale['message'])
        self.assertTrue(success['changed'])
        self.assertEqual(self.file.read_bytes(), b'repaired\n' + concurrent)
        change, = outcome['changes']
        self.assertEqual((Path(outcome['run_dir']) / change['backup']).read_bytes(), original + concurrent)

    def test_deleted_and_recreated_file_needs_fresh_read(self):
        self.file.write_bytes(b'first')

        def remove_then_recreate(event):
            if event['type'] == 'tool_result' and event['tool'] == 'read_file':
                reads = len(self.results('read_file'))
                if reads == 1:
                    self.file.unlink()
                elif reads == 2:
                    self.file.write_bytes(b'outside second')

        outcome = self.run_agent(
            [read()], [OVERWRITE], [read()], [OVERWRITE], [read()], [replace('second', 'updated')],
            callback=remove_then_recreate,
        )
        self.assertEqual(self.results('read_file')[1]['status'], 'not_found')
        for attempt in self.results('write_file'):
            self.assertFalse(attempt['ok'])
            self.assertIn('已变化', attempt['message'])
        self.assertEqual(self.file.read_bytes(), b'outside updated')
        self.assertEqual(len(outcome['changes']), 1)

    def test_stale_write_does_not_recreate_deleted_parent_directories(self):
        parent = self.project / 'nested'
        parent.mkdir()
        file = parent / FILE
        file.write_bytes(b'original')
        relative = f'nested/{FILE}'

        def remove_directory(event):
            if event['type'] == 'tool_result' and event['tool'] == 'read_file':
                file.unlink()
                parent.rmdir()

        outcome = self.run_agent(
            [('read_file', {'path': relative})], [('write_file', {'path': relative, 'content': 'new'})],
            callback=remove_directory,
        )
        self.assertIn('已变化', self.results('write_file')[0]['message'])
        self.assertFalse(parent.exists())
        changes = Path(outcome['run_dir']) / 'changes.json'
        self.assertEqual(json.loads(changes.read_text(encoding='utf-8'))['directories'], {})

    def test_external_write_during_atomic_commit_keeps_external_bytes(self):
        self.file.write_bytes(b'broken')
        real_atomic = project_agent._atomic_bytes
        checkpoints = []

        def race(path, data, **options):
            if path == self.file:
                run, = self.runs.iterdir()
                checkpoint = json.loads((run / 'changes.json').read_text(encoding='utf-8'))
                checkpoints.extend(checkpoint['changes'])
                self.file.write_bytes(b'broken + external')
            return real_atomic(path, data, **options)

        with patch.object(project_agent, '_atomic_bytes', side_effect=race):
            outcome = self.run_agent([read()], [replace()])
        self.assertEqual(len(checkpoints), 1)
        self.assertFalse(checkpoints[0]['applied'])
        self.assertIn('已变化', self.results('replace_text')[0]['message'])
        self.assertEqual(outcome['changes'], [])
        self.assertEqual(self.file.read_bytes(), b'broken + external')
        run = Path(outcome['run_dir'])
        self.assertEqual(list(run.glob('after-*')), [])
        self.assertEqual(list(run.glob('backup-*')), [])

    def test_applied_snapshots_of_successive_changes_are_immutable(self):
        self.file.write_bytes(b'broken')
        outcome = self.run_agent([read()], [replace()], [replace('repaired', 'reviewed')], [OVERWRITE])
        run = Path(outcome['run_dir'])
        changes = json.loads((run / 'changes.json').read_text(encoding='utf-8'))['changes']
        self.assertEqual(len(changes), 3)
        self.assertEqual(changes, outcome['changes'])
        self.file.write_bytes(b'external edit after agent run')
        for change, before, after in zip(changes, (b'broken', b'repaired', b'reviewed'),
                                         (b'repaired', b'reviewed', b'replacement\n')):
            self.assertTrue(change['applied'])
            self.assertEqual((run / change['backup']).read_bytes(), before)
            self.assertEqual((run / change['after_backup']).read_bytes(), after)
            self.assertEqual(change['after_sha256'], hashlib.sha256(after).hexdigest())
