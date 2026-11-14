"""Exercise the native Tk project conversation and a real child process in isolated storage."""
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
BUNDLED_PYTHON = ROOT / 'runtime' / 'python' / 'python.exe'


class FixtureModel:
    max_output_tokens = 1024

    def __init__(self, calls=(), final='检查完成'):
        self.calls = list(calls)
        self.final = final
        self.requests = []

    def complete_tools(self, messages, tools, **kwargs):
        self.requests.append(json.loads(json.dumps(messages)))
        if not self.calls:
            return {'role': 'assistant', 'content': self.final, 'tool_calls': []}
        name, arguments = self.calls.pop(0)
        return {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': f'call_{len(self.requests)}', 'type': 'function',
             'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


class NativeProjectScenario(unittest.TestCase):
    """Run only in a subprocess whose data root was redirected before app import."""

    def runTest(self):
        import tkinter as tk

        from client_app import ClientApp
        from paths import ARTIFACTS

        scratch = Path(os.environ['AI_TRAINING_DATA'])
        self.assertEqual(ARTIFACTS, scratch)
        workspace = scratch / 'workspace'
        workspace.mkdir()
        source = workspace / 'value.py'
        source.write_bytes(b'value = 1\n')
        child_marker = workspace / 'must-not-run'
        command = [sys.executable, '-B', '-c',
                   'from value import value; assert value == 2; print("CHECK_OK")']
        cancelled_command = [sys.executable, '-B', '-c',
                             'from pathlib import Path; Path("must-not-run").touch()']

        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        callback_errors = []
        root.report_callback_exception = lambda kind, error, traceback: callback_errors.append(error)

        def settle(app, approve=False, wait_for_approval=False):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                root.update()
                self.assertFalse(callback_errors, callback_errors)
                card = app.project.active
                if card and card.request:
                    summary = card.command_label.cget('text')
                    executable = str(Path(sys.executable).resolve())
                    self.assertIn(executable, summary)
                    self.assertIn(str(workspace), summary)
                    self.assertIn('最长', summary)
                    if wait_for_approval:
                        return card
                    if approve:
                        card.decide(True)
                if not app.busy:
                    self.assertFalse(wait_for_approval, 'task ended without an approval card')
                    return None
                time.sleep(.01)
            self.fail('native Tk task queue did not settle')

        def draft(app, value):
            app.input.delete('1.0', 'end')
            app.input.insert('1.0', value)

        with patch.object(tk, 'Toplevel', side_effect=AssertionError('project mode opened another window')):
            app = ClientApp(root)
            app.selected_model = 'text-default'
            app.backend = 'local'
            self.assertFalse(app.project.enabled.get())
            draft(app, '这是原来的聊天问题')
            with patch.object(app.manager, 'ready', return_value=True), patch('qwen_chat.chat',
                    return_value={'answer': '这是原来的聊天回答'}) as chat:
                app.send_chat()
                settle(app)
                chat.assert_called_once()
            identity = app.session_id
            initial_history = list(app.history)
            self.assertEqual([row['content'] for row in initial_history], ['这是原来的聊天问题', '这是原来的聊天回答'])

            first_task = '把 value 改为 2 并运行检查'
            draft(app, first_task)
            app.open_project_assistant()
            self.assertTrue(app.project.enabled.get())
            self.assertEqual(app.session_id, identity)
            self.assertEqual(app.history, initial_history)
            self.assertEqual(app.input.get('1.0', 'end-1c'), first_task)
            with patch('project_chat.filedialog.askdirectory', return_value=str(workspace)):
                app.project.choose_workspace()
            self.assertEqual(app.project.workspace.get(), str(workspace))
            self.assertEqual(app.project.folder_button.cget('text'), workspace.name + ' ▾')
            self.assertTrue(app.project.permissions_button.winfo_manager())
            self.assertFalse(app.project.location.winfo_manager())
            permissions = app.project.permissions_menu
            for option, key in [(0, 'allow_write'), (1, 'use_memory')]:
                initial = app.sessions.project_settings(identity)[key]
                permissions.invoke(option)
                self.assertEqual(app.sessions.project_settings(identity)[key], not initial)
                permissions.invoke(option)
                self.assertEqual(app.sessions.project_settings(identity)[key], initial)

            model = FixtureModel([
                ('read_file', {'path': 'value.py'}),
                ('replace_text', {'path': 'value.py', 'old': 'value = 1', 'new': 'value = 2'}),
                ('run_command', {'argv': command, 'timeout': 25}),
            ])
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=model) as local:
                app.send_chat()
                self.assertTrue(app.busy)
                settle(app, approve=True)
                local.assert_called_once_with(model_id='text-default')
            card = app.project.cards[-1]
            self.assertEqual(card.result['status'], 'completed', card.result)
            self.assertEqual(source.read_text(), 'value = 2\n')
            self.assertIn('CHECK_OK', card.activity)
            self.assertTrue(Path(card.result['run_dir'], 'result.json').is_file())
            self.assertEqual(card.assistant_index, 3)
            self.assertIn('CHECK_OK', app.sessions.project_runs(identity)[0]['activity'])
            self.assertFalse(card.tools.winfo_manager())
            card.toggle()
            self.assertTrue(card.expanded)
            self.assertTrue(card.tools.winfo_manager())
            self.assertIn('CHECK_OK', card.detail.get('1.0', 'end-1c'))
            card.toggle()
            self.assertFalse(card.expanded)
            self.assertFalse(card.detail.winfo_manager())
            self.assertFalse(card.tools.winfo_manager())

            second_task = '继续刚才的工作，说明改动'
            followup = FixtureModel(final='后续说明完成')
            draft(app, second_task)
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=followup):
                app.send_chat()
                settle(app)
            self.assertEqual(len(followup.requests), 1)
            original_workspace_note = f'该项目回复对应的工作区：{workspace}\n\n'
            self.assertEqual(followup.requests[0][1:], initial_history + [
                {'role': 'user', 'content': first_task},
                {'role': 'assistant', 'content': original_workspace_note + '检查完成'},
                {'role': 'user', 'content': second_task},
            ])
            self.assertEqual([row['assistant_index'] for row in app.sessions.project_runs(identity)], [3, 5])

            with patch('local_project_model.LocalProjectModel', side_effect=AssertionError('load executed a task')):
                app.new_conversation()
                self.assertFalse(app.project.enabled.get())
                self.assertFalse(app.project.workspace.get())
                self.assertEqual(app.project.cards, [])
                self.assertEqual(app.history, [])
                app.load_conversation(identity)
                self.assertTrue(app.project.enabled.get())
                self.assertEqual(app.project.workspace.get(), str(workspace))
                self.assertEqual(len(app.project.cards), 2)
                self.assertFalse(app.busy)
                restored = app.project.cards[0]
                self.assertIn('CHECK_OK', restored.activity)
                self.assertEqual(restored.assistant_index, 3)
                restored.confirm_undo()
                self.assertTrue(restored.undo_box.winfo_manager())
                app.project.undo(restored)
                self.assertEqual(source.read_text(), 'value = 1\n')
                self.assertIn('[已撤回文件修改]', restored.activity)
                annotated = app.history[3]['content']
                self.assertTrue(annotated.startswith('检查完成\n\n'))
                self.assertIn('用户已执行文件撤回。已恢复文件：value.py。', annotated)
                self.assertIn(annotated, app.transcript.get('1.0', 'end'))
                self.assertEqual(app.sessions.messages(identity)[3]['content'], annotated)
                self.assertEqual(app.last_answer, '后续说明完成')
                self.assertEqual(len(app.history), 6)
                app.new_conversation()
                app.load_conversation(identity)
                restored = app.project.cards[0]
                self.assertIn('[已撤回文件修改]', restored.activity)
                self.assertEqual(restored.undo_button.button_state, 'disabled')
                self.assertEqual(app.history[3]['content'], annotated)

            draft(app, '等待批准时停止')
            stopped_model = FixtureModel([('run_command', {'argv': cancelled_command})])
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=stopped_model):
                app.send_chat()
                waiting = settle(app, wait_for_approval=True)
                app.stop_generation()
                settle(app)
            self.assertFalse(child_marker.exists())
            self.assertIsNone(app.project.active)
            self.assertIsNone(waiting.request)
            self.assertEqual(waiting.result['status'], 'stopped')
            runs = app.sessions.project_runs(identity)
            self.assertEqual(runs[-1]['result']['status'], 'stopped')
            self.assertEqual(runs[-1]['assistant_index'], 7)
            self.assertEqual(stopped_model.requests[0][4]['content'], original_workspace_note + annotated)

            unsent = '这段草稿必须保留'
            draft(app, unsent)
            message_count = len(app.history)
            with patch.object(app.manager, 'ready', return_value=False):
                app.send_chat()
            self.assertFalse(app.busy)
            self.assertEqual(app.input.get('1.0', 'end-1c'), unsent)
            self.assertIn('文字模型', app.status.get())
            app.backend = 'api'
            with patch.object(app, 'make_api_client', side_effect=ValueError('fixture unavailable')):
                app.send_chat()
            self.assertFalse(app.busy)
            self.assertEqual(app.input.get('1.0', 'end-1c'), unsent)
            self.assertIn('API 配置', app.status.get())
            self.assertEqual(len(app.history), message_count)
            self.assertEqual(app.sessions.project_runs(identity), runs)

            alternate = scratch / 'another-workspace'
            alternate.mkdir()
            alternate_source = alternate / 'value.py'
            alternate_source.write_bytes(b'value = 10\n')
            with patch('project_chat.filedialog.askdirectory', return_value=str(alternate)):
                app.project.choose_workspace()
            app.backend = 'local'
            alternate_task = '查看当前文件再把 value 改为 11'
            draft(app, alternate_task)
            alternate_model = FixtureModel([
                ('read_file', {'path': 'value.py'}),
                ('replace_text', {'path': 'value.py', 'old': 'value = 10', 'new': 'value = 11'}),
            ])
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=alternate_model):
                app.send_chat()
                settle(app)
            inputs = alternate_model.requests[0]
            self.assertIn(str(alternate), inputs[0]['content'])
            self.assertEqual(inputs[4]['content'], original_workspace_note + annotated)
            self.assertIn('本轮已切换项目目录。请先重新检查当前工作区的文件。', inputs[-1]['content'])
            self.assertEqual(app.session_id, identity)
            self.assertEqual(app.project.cards[-1].result['status'], 'completed')
            self.assertEqual(app.project.cards[-1].workspace, str(alternate))
            self.assertEqual(source.read_text(), 'value = 1\n')
            self.assertEqual(alternate_source.read_text(), 'value = 11\n')
            self.assertEqual(app.sessions.messages(identity)[3]['content'], annotated)
            self.assertEqual(app.sessions.messages(identity)[8]['content'], alternate_task)
            self.assertEqual(app.history[3]['content'], annotated)

            app.new_conversation()
            app.open_project_assistant()
            with patch('project_chat.filedialog.askdirectory', return_value=str(workspace)):
                app.project.choose_workspace()
            app.backend = 'local'
            draft(app, '模拟结束阶段数据库故障')
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=FixtureModel()), patch.object(
                    app.sessions, '_write_project_run', side_effect=OSError('fixture write failure')):
                app.send_chat()
                settle(app)
            failed_id = app.session_id
            unsaved = app.project.cards[-1]
            self.assertIsNone(app.pending_chat)
            self.assertIsNone(unsaved.assistant_index)
            self.assertEqual(app.history[-1]['content'], '检查完成')
            self.assertEqual([row['role'] for row in app.sessions.messages(failed_id)], ['user'])
            self.assertEqual(app.sessions.project_runs(failed_id), [])
            self.assertTrue(Path(unsaved.result['run_dir'], 'result.json').is_file())
            self.assertIn('未能保存本轮对话', app.status.get())

            draft(app, '继续未成功保存的本轮工作')
            unsaved_followup = FixtureModel()
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=unsaved_followup):
                app.send_chat()
                settle(app)
            self.assertEqual(unsaved_followup.requests[0][2]['content'], original_workspace_note + '检查完成')

            def broken_update(_):
                raise RuntimeError('fixture Tk callback failure')

            processed = []
            app.events.put(('update', broken_update, None))
            app.events.put(('update', processed.append, 'next update'))
            deadline = time.monotonic() + 2
            while not processed and time.monotonic() < deadline:
                root.update()
                time.sleep(.01)
            self.assertEqual(processed, ['next update'])
            self.assertFalse(callback_errors)


class ProjectUITest(unittest.TestCase):
    def test_native_project_workflow(self):
        with tempfile.TemporaryDirectory(prefix='ui-project-', dir=ROOT / 'development') as scratch:
            env = dict(os.environ, AI_TRAINING_DATA=scratch)
            executable = str(BUNDLED_PYTHON) if BUNDLED_PYTHON.is_file() else sys.executable
            code = ('import sys, unittest; from pathlib import Path; '
                    'sys.path.insert(0, str(Path("development/tests").resolve())); '
                    'from test_project_ui import NativeProjectScenario; '
                    'result = unittest.TextTestRunner(verbosity=2).run(NativeProjectScenario()); '
                    'sys.exit(not result.wasSuccessful())')
            result = subprocess.run([executable, '-E', '-s', '-B', '-X', 'utf8', '-c', code],
                cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(ProjectUITest)


if __name__ == '__main__':
    unittest.main()
