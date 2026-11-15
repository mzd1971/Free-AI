"""The project permission setting governs actual file and process execution."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

from project_agent import ProjectAgent
from test_project_agent import ScriptedModel


ROOT = Path(__file__).resolve().parents[2]


class ProjectPermissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / 'development')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.workspace = self.folder / 'project'
        self.workspace.mkdir()
        self.events = []
        self.command = {'argv': [sys.executable, '-B', '-c',
                                'from pathlib import Path; Path("executed.txt").write_text("actual process")']}

    def agent(self, mode, turns, approve, **options):
        model = ScriptedModel(*turns)
        if mode == 'readonly':
            model.expected_tools -= {'write_file', 'replace_text', 'run_command'}
        return ProjectAgent(self.workspace, model, runs_root=self.folder / 'runs',
                            permission_mode=mode, approve_command=approve,
                            on_event=options.pop('on_event', self.events.append), **options)

    def test_standard_asks_and_does_not_run_declined_command(self):
        approve = Mock(return_value=False)
        result = self.agent('standard', [[('run_command', self.command)], '已跳过。'], approve).run('执行任务')
        approve.assert_called_once()
        self.assertFalse((self.workspace / 'executed.txt').exists())
        self.assertEqual(result['permission_mode'], 'standard')
        self.assertEqual(approve.call_args.args[0]['permission_mode'], 'standard')

    def test_automatic_runs_real_process_without_approval_callback(self):
        approve = Mock(side_effect=AssertionError('Automatic must not request confirmation'))
        result = self.agent('automatic', [[('run_command', self.command)], '已运行。'], approve).run('执行任务')
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual((self.workspace / 'executed.txt').read_text(), 'actual process')
        approve.assert_not_called()
        request, = [event for event in self.events if event['type'] == 'tool']
        self.assertEqual(request['permission_mode'], 'automatic')
        saved = json.loads((Path(result['run_dir']) / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['permission_mode'], 'automatic')

    def test_readonly_rejects_even_invented_write_and_command_calls(self):
        approve = Mock(return_value=True)
        agent = self.agent('readonly', [[('write_file', {'path': 'new.txt', 'content': 'forbidden'}),
                                         ('run_command', self.command)], '只提供建议。'], approve)
        result = agent.run('查看项目')
        self.assertFalse((self.workspace / 'new.txt').exists())
        self.assertFalse((self.workspace / 'executed.txt').exists())
        self.assertFalse(result['changes'])
        approve.assert_not_called()
        replies = [event['result'] for event in self.events if event['type'] == 'tool_result']
        self.assertTrue(all(row['ok'] is False for row in replies))

    def test_automatic_still_honors_stop_before_starting_process(self):
        cancel = threading.Event()

        def event(row):
            if row['type'] == 'tool':
                cancel.set()

        approve = Mock()
        result = self.agent('automatic', [[('run_command', self.command)]], approve,
                            cancel_event=cancel, on_event=event).run('执行任务')
        self.assertEqual(result['status'], 'stopped')
        self.assertFalse((self.workspace / 'executed.txt').exists())
        approve.assert_not_called()

    def test_resuming_automatic_task_uses_new_standard_mode(self):
        first = self.agent('automatic', [[('list_files', {})]], Mock(), max_steps=1).run('继续项目')
        approve = Mock(return_value=False)
        second = self.agent('standard', [[('run_command', self.command)], '已跳过。'], approve).run(
            '继续', resume_from=first['run_dir'])
        self.assertEqual(second['permission_mode'], 'standard')
        self.assertEqual(second['task_state']['parent_run'], Path(first['run_dir']).name)
        self.assertFalse((self.workspace / 'executed.txt').exists())
        approve.assert_called_once()

    def test_invalid_mode_and_non_boolean_write_option_are_rejected(self):
        for mode in ('', 'full', 'true', True, None, []):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.agent(mode, [], Mock())
        with self.assertRaises(ValueError):
            self.agent('automatic', [], Mock(), allow_write='false')


if __name__ == '__main__':
    unittest.main()
