"""Exercise workspace opt-in, real acceptance commands and bounded repair in native chat."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def exercise(test):
    import tkinter as tk
    from client_app import ClientApp
    from project_checks import load_project_check, save_project_check
    from test_project_ui import FixtureModel

    scratch = Path(os.environ['AI_TRAINING_DATA'])
    project, other = scratch / 'project', scratch / 'other'
    project.mkdir()
    other.mkdir()
    source = project / 'value.py'
    source.write_text('value = 1\n', encoding='utf-8')
    command = json.dumps([sys.executable, '-B', '-c',
                          'from value import value; assert value == 2, "FIX_VALUE"; print("ACCEPTED")'])
    root = tk.Tk()
    test.addCleanup(root.destroy)
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda kind, error, traceback: errors.append(error)
    app = ClientApp(root)
    test.addCleanup(lambda: root.after_cancel(app.poll_id))
    app.backend, app.selected_model = 'local', 'text-default'
    app.open_project_assistant()
    with patch('project_chat.filedialog.askdirectory', return_value=str(project)):
        app.project.choose_workspace()
    with patch('project_chat.simpledialog.askstring', return_value=command):
        app.project.configure_fixed_check()
    test.assertEqual(load_project_check(app.sessions, project / '.'), command)
    test.assertEqual(load_project_check(app.sessions, other), '')

    approvals = []
    def turn(text, *, allow=True, next_draft=None, stop_check=False):
        app.input.delete('1.0', 'end')
        app.input.insert('1.0', text)
        begin = len(app.project.cards)
        app.send_chat()
        if next_draft:
            app.input.insert('1.0', next_draft)
        until = time.monotonic() + 18
        while app.busy and time.monotonic() < until:
            root.update()
            test.assertFalse(errors, errors)
            card = app.project.active
            if card and card.request:
                approvals.append(card.request['details'])
                if stop_check:
                    app.stop_generation()
                    card.decide(False)
                else:
                    card.decide(allow)
            time.sleep(.01)
        test.assertFalse(app.busy)
        return app.project.cards[begin:]

    first = FixtureModel(final='Task complete.')
    repaired = FixtureModel([('read_file', {'path': 'value.py'}),
                             ('replace_text', {'path': 'value.py', 'old': 'value = 1', 'new': 'value = 2'})])
    with patch.object(app.manager, 'ready', return_value=True), patch(
            'local_project_model.LocalProjectModel', side_effect=[first, repaired]):
        cards = turn('Repair value.py', next_draft='draft for my next task')
    test.assertEqual([card.result['fixed_check']['status'] for card in cards], ['failed', 'passed'])
    test.assertEqual(cards[0].result['status'], 'unfinished')
    test.assertTrue(cards[0].result['answer'].startswith('项目固定检查尚未通过'))
    test.assertEqual(cards[1].result['fixed_check']['stdout'], 'ACCEPTED')
    test.assertTrue(all(Path(card.result['snapshot_dir']).is_dir() for card in cards))
    test.assertEqual(cards[1].result['task_state']['parent_run'], Path(cards[0].result['run_dir']).name)
    test.assertIn('FIX_VALUE', repaired.requests[0][-1]['content'])
    test.assertIn('退出码：1', repaired.requests[0][-1]['content'])
    test.assertEqual([row['argv'] for row in approvals], [json.loads(command)] * 2)
    test.assertEqual(app.input.get('1.0', 'end-1c'), 'draft for my next task')
    rows = app.sessions.project_runs(app.session_id)
    test.assertEqual([r['result']['fixed_check']['returncode'] for r in rows], [1, 0])

    with patch.object(app.manager, 'ready', return_value=True), patch(
            'local_project_model.LocalProjectModel', side_effect=lambda **kwargs: FixtureModel()):
        source.write_text('value = 1\n', encoding='utf-8')
        before = len(approvals)
        rejected, = turn('Skip the check', allow=False)
        test.assertEqual(len(approvals) - before, 1)
        test.assertNotEqual(rejected.result['fixed_check']['status'], 'passed')
        test.assertEqual(rejected.result['status'], 'unfinished')
        stopped, = turn('Stop the check', stop_check=True)
        test.assertEqual(stopped.result['status'], 'stopped')
        test.assertEqual(stopped.result['fixed_check']['status'], 'stopped')
        app.project.set_permission_mode('automatic')
        before = len(approvals)
        capped = turn('Try to repair')
        test.assertEqual(len(approvals), before)
        test.assertEqual([c.result['fixed_check']['repair_attempt'] for c in capped], [0, 1, 2])
        test.assertTrue(all(c.result['status'] == 'unfinished' for c in capped))
        test.assertTrue(capped[-1].can_resume())
        capped[-1].check_command.set(json.dumps([sys.executable, '-V']))
        app.project.verify(capped[-1])
        until = time.monotonic() + 6
        while app.busy and time.monotonic() < until:
            root.update()
            time.sleep(.01)
        test.assertEqual(capped[-1].result['acceptance']['status'], 'passed')
        test.assertEqual(capped[-1].result['fixed_check']['status'], 'failed')
        test.assertEqual(capped[-1].result['status'], 'unfinished')
        from project_verification import run_verification
        slow = json.dumps([sys.executable, '-B', '-c', 'import time; time.sleep(5)'])
        save_project_check(app.sessions, project, slow)
        with patch('project_verification.run_verification',
                   side_effect=lambda *args, **kwargs: run_verification(*args, **kwargs, timeout=1)):
            timed_out, = turn('Try the slow check')
        test.assertEqual(timed_out.result['fixed_check']['status'], 'timeout')
        test.assertEqual(timed_out.result['status'], 'unfinished')
        app.project.set_permission_mode('readonly')
        with patch('project_verification.run_verification', side_effect=AssertionError('readonly check')) as check:
            readonly, = turn('Explain the file')
            check.assert_not_called()
            test.assertNotIn('fixed_check', readonly.result)
        save_project_check(app.sessions, project, '')
        test.assertEqual(load_project_check(app.sessions, project), '')


class FixedProjectCheckTests(unittest.TestCase):
    def test_saved_check_fails_repairs_and_rechecks_with_user_permissions(self):
        with tempfile.TemporaryDirectory(prefix='fixed-check-', dir=ROOT / 'development') as scratch:
            code = ('import sys,unittest;sys.path.insert(0,"development/tests");'
                    'from test_fixed_project_check import exercise;'
                    'case=unittest.FunctionTestCase(lambda:exercise(case));'
                    'sys.exit(not unittest.TextTestRunner().run(case).wasSuccessful())')
            result = subprocess.run([sys.executable, '-B', '-s', '-E', '-X', 'utf8', '-c', code],
                                    env=dict(os.environ, AI_TRAINING_DATA=scratch), cwd=ROOT,
                                    capture_output=True, encoding='utf-8', timeout=70)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
