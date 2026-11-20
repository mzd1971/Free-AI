"""Verify the command transcript actually sent to the project model."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from project_agent import OBSERVATION_LIMIT, ProjectAgent
from test_project_agent import ScriptedModel


class CommandObservations(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.model = ScriptedModel()
        self.agent = ProjectAgent(self.project, self.model, runs_root=self.root / 'runs',
                                  context=16384, approve_command=lambda _: True)

    def test_verbose_unittest_failure_retains_diagnostic_and_full_event(self):
        source = (
            'import unittest\n'
            'def clean_names(names): return names\n'
            'class NamesTests(unittest.TestCase):\n'
            '    def test_z_case_sensitive(self): clean_names([], case_sensitive=True)\n'
            'for n in range(48):\n'
            '    setattr(NamesTests, f"test_keeps_original_spelling_{n:02}", lambda self: None)\n'
        )
        (self.project / 'test_names.py').write_text(source, encoding='utf-8')
        self.model.turns = [[('run_command', {'argv': [sys.executable, '-B', '-m', 'unittest', '-v', 'test_names']})],
                            '检查结束。']
        result = self.agent.run('运行检查并说明实际结果。')
        events = (Path(result['run_dir']) / 'events.jsonl').read_text(encoding='utf-8').splitlines()
        raw = next(row['result'] for row in map(json.loads, events) if row['type'] == 'tool_result')
        message = self.model.calls[1][-1]['content']
        seen = json.loads(message)
        self.assertEqual((seen['ok'], seen['status'], seen['returncode']), (False, 'exited', 1))
        self.assertTrue(seen['stderr'].startswith('test_keeps_original_spelling_00'))
        self.assertIn("TypeError: clean_names() got an unexpected keyword argument 'case_sensitive'", seen['stderr'])
        self.assertTrue(seen['stderr'].strip().endswith('FAILED (errors=1)'))
        self.assertTrue(seen['stderr_truncated'])
        self.assertFalse(raw['stderr_truncated'])
        self.assertGreater(len(raw['stderr']), len(seen['stderr']))
        self.assertIn('test_keeps_original_spelling_24', raw['stderr'])
        self.assertLessEqual(len(message.encode()), OBSERVATION_LIMIT)

    def test_failed_command_shares_escaped_byte_budget_and_keeps_both_ends(self):
        # Include one-byte chars which cost up to six bytes when JSON-escaped.
        sample = '汉🙂\r\n\t"\\\x01' * 400
        original = {'ok': False, 'status': 'exited', 'returncode': 8,
                    'stdout': 'OUT_START' + sample + 'OUT_END', 'stderr': 'ERR_START' + sample + 'ERR_END',
                    'stdout_truncated': False, 'stderr_truncated': False}
        for budget in (300, 911, 1800):
            with self.subTest(budget=budget):
                message = self.agent._observation(original, budget)
                seen = json.loads(message)
                self.assertLessEqual(len(message.encode()), budget)
                self.assertGreater(len(message.encode()), budget - 34)
                self.assertEqual((seen['ok'], seen['status'], seen['returncode']), (False, 'exited', 8))
                for field, label in (('stdout', 'OUT'), ('stderr', 'ERR')):
                    self.assertTrue(seen[field].startswith(label + '_START'))
                    self.assertTrue(seen[field].endswith(label + '_END'))
                    self.assertTrue(seen[field + '_truncated'])
                    self.assertFalse(original[field + '_truncated'])
                self.assertGreater(len(seen['stderr']), len(seen['stdout']))

    def test_short_stream_and_preexisting_capture_flags_survive(self):
        raw = {'ok': True, 'status': 'exited', 'returncode': 0,
               'stdout': 'header' + '.' * 4000 + 'done', 'stderr': 'partial capture',
               'stdout_truncated': False, 'stderr_truncated': True}
        observed = json.loads(self.agent._observation(raw, 450))
        self.assertEqual(observed['stderr'], 'partial capture')
        self.assertTrue(observed['stderr_truncated'])
        self.assertTrue(observed['stdout'].endswith('done'))
        self.assertTrue(observed['stdout_truncated'])
