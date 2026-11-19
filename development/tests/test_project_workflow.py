"""Exercise snapshots and explicit checks through the native conversation UI."""
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
    from test_project_ui import FixtureModel

    scratch = Path(os.environ['AI_TRAINING_DATA'])
    workspace = scratch / 'workspace'
    workspace.mkdir()
    source = workspace / 'value.py'
    source.write_text('value = 0\n', encoding='utf-8')
    (workspace / 'old.txt').write_text('original', encoding='utf-8')
    root = tk.Tk()
    root.withdraw()
    test.addCleanup(root.destroy)
    errors = []
    root.report_callback_exception = lambda cls, error, tb: errors.append(error)
    app = ClientApp(root)
    test.addCleanup(lambda: root.after_cancel(app.poll_id))
    app.backend, app.selected_model = 'local', 'text-default'
    app.open_project_assistant()
    with patch('project_chat.filedialog.askdirectory', return_value=str(workspace)):
        app.project.choose_workspace()

    def settle():
        until = time.monotonic() + 15
        while time.monotonic() < until:
            root.update()
            test.assertFalse(errors, errors)
            card = app.project.active
            if card and card.request:
                card.decide(True)
            if not app.busy:
                return
            time.sleep(.01)
        test.fail('conversation worker did not finish')

    def draft(text):
        app.input.delete('1.0', 'end')
        app.input.insert('1.0', text)

    command = [sys.executable, '-B', '-c', 'from value import value; assert value == 2; print("ACCEPTED")']
    edit = [sys.executable, '-B', '-c',
            'from pathlib import Path; Path("value.py").write_text("value = 1\\n"); '
            'Path("new.txt").write_text("created"); Path("old.txt").unlink()']
    with patch.object(app.manager, 'ready', return_value=True), patch('local_project_model.LocalProjectModel') as model:
        model.return_value = FixtureModel([('run_command', {'argv': edit})])
        draft('通过命令修改项目')
        app.send_chat()
        settle()
        first = app.project.cards[-1]
        identity = app.session_id
        test.assertEqual(first.result['snapshot']['count'], 3, first.result)
        first.toggle()
        first.review_button.invoke()
        diff = first.review_text.get('1.0', 'end-1c')
        test.assertIn('old.txt', diff)
        test.assertIn('new.txt', diff)
        test.assertIn('value = 1', diff)
        first.check_command.set('freeai-missing-test-program-72345')
        app.project.verify(first)
        settle()
        test.assertEqual(first.result['acceptance']['status'], 'error')
        test.assertEqual(first.result['status'], 'unfinished')
        test.assertTrue(first.can_resume())
        first.toggle_check()
        first.check_command.set(json.dumps(command))
        first.run_check_button.invoke()
        settle()
        test.assertEqual(first.result['acceptance']['status'], 'failed', first.result)
        test.assertEqual(first.result['status'], 'unfinished')
        test.assertTrue(first.can_resume())
        test.assertIn('AssertionError', first.activity)
        test.assertIn('AssertionError', app.history[-1]['content'])
        test.assertIn('AssertionError', app.sessions.messages(identity)[-1]['content'])

        model.return_value = FixtureModel([
            ('read_file', {'path': 'value.py'}),
            ('replace_text', {'path': 'value.py', 'old': 'value = 1', 'new': 'value = 2'}),
            ('run_command', {'argv': command, 'purpose': 'check'}),
        ])
        first.resume_button.invoke()
        settle()
        second = app.project.cards[-1]
        test.assertIn('AssertionError', json.dumps(model.return_value.requests))
        test.assertEqual(source.read_text(), 'value = 2\n')
        test.assertEqual(second.result['status'], 'review')
        test.assertTrue(second.result['acceptance']['stale'])
        second.check_command.set(json.dumps(command))
        app.project.verify(second)
        settle()
        test.assertEqual(second.result['acceptance']['status'], 'passed', second.result['acceptance'])
        test.assertIn('ACCEPTED', second.activity)
        test.assertIn('这条检查已通过', second.progress.cget('text'))

        # A crash after the archives were saved but before the final card update.
        interrupted = {**second.result, 'status': 'running'}
        interrupted.pop('snapshot')
        app.sessions.update_project_reply(identity, second.assistant_index, interrupted, str(workspace), second.activity)

        app.new_conversation()
        app.load_conversation(identity)
        first, second = app.project.cards
        test.assertTrue(Path(second.result['snapshot_dir']).is_dir())
        test.assertEqual(second.result['acceptance']['status'], 'passed')
        test.assertTrue(second.result['acceptance']['stale'])
        test.assertEqual(second.result['snapshot']['count'], 1)
        # The card's snapshot controls are shared by the optional Harness engine.
        second.result['engine'] = 'harness'
        second.finish(second.result)
        test.assertEqual(second.undo_button.cget('state'), 'normal')
        second.confirm_undo()
        app.project.undo(second)
        test.assertEqual(source.read_text(), 'value = 2\n')
        second.undo_select_all.invoke()
        app.project.undo(second)
        test.assertEqual(source.read_text(), 'value = 1\n')
        test.assertFalse(second.can_resume())
        test.assertNotIn('这条检查已通过', second.status.get())
        (workspace / 'new.txt').write_text('user changed it', encoding='utf-8')
        app.project.undo(first)
        test.assertEqual(source.read_text(), 'value = 0\n')
        test.assertEqual((workspace / 'old.txt').read_text(), 'original')
        test.assertEqual((workspace / 'new.txt').read_text(), 'user changed it')
        test.assertIn('new.txt', first.activity)

        app.project.set_permission_mode('readonly')
        with patch('project_verification.run_verification') as blocked:
            app.project.verify(first)
            blocked.assert_not_called()


class ProjectWorkflowTests(unittest.TestCase):
    def test_real_files_command_fail_repair_check_reopen_and_restore(self):
        with tempfile.TemporaryDirectory(prefix='workflow-', dir=ROOT / 'development') as scratch:
            command = ('import sys, unittest; sys.path.insert(0, "development/tests"); '
                       'from test_project_workflow import exercise; '
                       'case = unittest.FunctionTestCase(lambda: exercise(case)); '
                       'sys.exit(not unittest.TextTestRunner().run(case).wasSuccessful())')
            result = subprocess.run([sys.executable, '-E', '-s', '-B', '-X', 'utf8', '-c', command],
                env=dict(os.environ, AI_TRAINING_DATA=scratch), cwd=ROOT, capture_output=True,
                encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
