"""Exercise native chat routing and persistence without a component or model service."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]


def exercise_chat(test):
    import tkinter as tk

    from client_app import ClientApp
    from harness_settings import save_preferences
    from project_agent import ProjectAgent
    from test_project_ui import FixtureModel
    from ui_kit import FlatButton

    scratch = Path(os.environ['AI_TRAINING_DATA'])
    workspace = scratch / 'project'
    workspace.mkdir()
    executable = scratch / 'dsh.exe'
    calls = []
    operation = '{"kind":"edit","title":"修改 README","locations":[{"path":"README.md"}]}'

    class FakeHarness:
        def __init__(self, folder, model, *, runs_root, approve_command, on_event, **options):
            self.run_dir = runs_root / uuid.uuid4().hex
            self.workspace = folder
            self.approve = approve_command
            self.event = on_event

        def run(self, task, history=None, resume_from=None):
            calls.append((task, resume_from))
            self.run_dir.mkdir(parents=True)
            metadata = {'version': 1, 'engine': 'harness', 'workspace': os.path.normcase(self.workspace),
                        'home_id': '0' * 32, 'session_id': 'fixture', 'route': 'fixture',
                        'permission_mode': 'standard'}
            (self.run_dir / 'harness.json').write_text(json.dumps(metadata), encoding='utf-8')
            result = {'engine': 'harness', 'run_dir': str(self.run_dir), 'permission_mode': 'standard',
                      'changes': [], 'answer': '需要验收。', 'status': 'review', 'continuation_available': True}
            self.event({'type': 'start', 'engine': 'harness', 'run_dir': str(self.run_dir)})
            if resume_from is None:
                decision = self.approve({'engine': 'harness', 'executable': 'DeepSeek Harness',
                    'cwd': self.workspace, 'timeout': 1800, 'operation': operation, 'argv': []})
                if not decision:
                    raise AssertionError('fixture operation was not approved')
            return result

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
    identity = app.session_id

    def draft(value):
        app.input.delete('1.0', 'end')
        app.input.insert('1.0', value)

    def settle(wait_for_request=False):
        until = time.monotonic() + 6
        while time.monotonic() < until:
            root.update()
            test.assertFalse(errors, errors)
            card = app.project.active
            if wait_for_request and card and card.request:
                return card
            if not app.busy:
                test.assertFalse(wait_for_request, 'task finished without requesting permission')
                return
            time.sleep(.01)
        test.fail('project chat did not settle')

    def engine(value):
        save_preferences(app.sessions, {'engine': value, 'executable': str(executable)})

    engine('harness')
    text = '检查文档'
    draft(text)
    with ExitStack() as stack:
        fixture = stack.enter_context(patch('harness_agent.HarnessAgent', side_effect=FakeHarness))
        model = stack.enter_context(patch('local_project_model.LocalProjectModel', return_value=FixtureModel()))
        stack.enter_context(patch.object(app.manager, 'ready', return_value=True))
        builtin = stack.enter_context(patch('project_agent.ProjectAgent', wraps=ProjectAgent))
        for reason in ('missing component', 'readonly'):
            with test.subTest(preflight=reason):
                if reason == 'readonly':
                    executable.touch()
                    app.project.set_permission_mode('readonly')
                app.send_chat()
                test.assertFalse(app.busy)
                test.assertEqual(app.input.get('1.0', 'end-1c'), text)
                test.assertEqual(app.sessions.messages(identity), [])
                test.assertEqual(app.session_id, identity)
                test.assertIn('草稿', app.status.get())
                fixture.assert_not_called()
                model.assert_not_called()
        app.project.set_permission_mode('standard')
        app.send_chat()
        identity = app.session_id
        test.assertIsNotNone(identity)
        card = settle(wait_for_request=True)
        test.assertIn('组件操作', card.command_label.cget('text'))
        test.assertIn(operation, card.arguments.get('1.0', 'end-1c'))
        test.assertNotIn('完整参数', card.arguments.get('1.0', 'end-1c'))
        buttons = []
        widgets = card.approval_box.winfo_children()
        while widgets:
            widget = widgets.pop()
            if isinstance(widget, FlatButton):
                buttons.append(widget.cget('text'))
            widgets.extend(widget.winfo_children())
        test.assertTrue(buttons)
        waiting_status = card.status.get()
        card.decide(True)
        settle()
        builtin.assert_not_called()
        test.assertEqual(calls, [(text, None)])
        test.assertEqual(card.result['status'], 'review')
        test.assertTrue(card.can_resume())
        test.assertEqual(card.undo_button.cget('state'), 'disabled')
        test.assertIn('待验收', card.status.get())
        test.assertEqual(app.sessions.project_runs(identity)[0]['result']['engine'], 'harness')
        # Even a persisted nonempty file list is not a Free AI rollback manifest for this engine.
        stale = {**card.result, 'changes': [{'path': 'README.md'}]}
        app.sessions.update_project_reply(identity, card.assistant_index, stale, str(workspace), card.activity)
        app.new_conversation()
        app.load_conversation(identity)
        original = app.project.cards[0]
        test.assertTrue(original.can_resume())
        test.assertTrue(original.resume_button.winfo_manager())
        test.assertEqual(original.result['engine'], 'harness')
        test.assertEqual(original.result['status'], 'review')
        test.assertEqual(original.undo_button.cget('state'), 'disabled')
        with patch('project_agent.rollback_run') as rollback:
            app.project.undo(original)
            rollback.assert_not_called()
        engine('builtin')
        original.resume_button.invoke()
        settle()
        test.assertEqual(calls[-1][1], original.result['run_dir'])
        test.assertEqual(app.project.cards[-1].result['engine'], 'harness')
        builtin.assert_not_called()

        # Write a resumable built-in run, remove the post-upgrade engine field and reopen it.
        model.return_value = FixtureModel([('update_plan', {'steps': ['待完成']})])
        draft('准备后续任务')
        app.send_chat()
        settle()
        legacy = app.project.cards[-1]
        test.assertEqual(legacy.result['status'], 'unfinished')
        record = dict(legacy.result)
        record.pop('engine', None)
        app.sessions.update_project_reply(identity, legacy.assistant_index, record, str(workspace), legacy.activity)
        app.new_conversation()
        app.load_conversation(identity)
        engine('harness')
        model.return_value = FixtureModel([('update_plan', {'steps': [
            {'text': '待完成', 'status': 'completed'}]})])
        app.project.cards[-1].resume_button.invoke()
        settle()
        test.assertEqual(app.project.cards[-1].result['engine'], 'builtin')
        test.assertEqual(app.project.cards[-1].result['status'], 'completed')
        test.assertEqual(fixture.call_count, 2)
        test.assertEqual(builtin.call_count, 2)
        test.assertFalse(any('命令' in title for title in buttons), buttons)
        test.assertNotIn('命令', waiting_status)


class HarnessChatTests(unittest.TestCase):
    def test_chat_engine_selection_approval_and_resume(self):
        with tempfile.TemporaryDirectory(prefix='harness-chat-', dir=ROOT / 'development') as scratch:
            command = ('import sys, unittest; sys.path.insert(0, "development/tests"); '
                       'from test_harness_chat import exercise_chat; '
                       'test = unittest.FunctionTestCase(lambda: exercise_chat(test)); '
                       'sys.exit(not unittest.TextTestRunner().run(test).wasSuccessful())')
            result = subprocess.run([sys.executable, '-E', '-s', '-B', '-X', 'utf8', '-c', command],
                                    env=dict(os.environ, AI_TRAINING_DATA=scratch), cwd=ROOT,
                                    capture_output=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
