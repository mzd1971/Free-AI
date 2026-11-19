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
        self.tool_names = []

    def complete_tools(self, messages, tools, **kwargs):
        self.requests.append(json.loads(json.dumps(messages)))
        self.tool_names.append({tool['function']['name'] for tool in tools})
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
            self.assertEqual(app.project.permissions_button.cget('text'), '逐条确认命令 ▾')
            with patch.object(app, 'open_project_in_editor') as editor, patch.object(app, 'open_project_terminal') as terminal:
                permissions.invoke('在编辑器中打开项目')
                permissions.invoke('在终端中打开项目')
                editor.assert_called_once_with(str(workspace))
                terminal.assert_called_once_with(str(workspace))
            for option, key in [('允许文件工具修改', 'allow_write'), ('参考经验', 'use_memory')]:
                initial = app.sessions.project_settings(identity)[key]
                permissions.invoke(option)
                self.assertEqual(app.sessions.project_settings(identity)[key], not initial)
                permissions.invoke(option)
                self.assertEqual(app.sessions.project_settings(identity)[key], initial)

            model = FixtureModel([
                ('read_file', {'path': 'value.py'}),
                ('replace_text', {'path': 'value.py', 'old': 'value = 1', 'new': 'value = 2'}),
                ('run_command', {'argv': command, 'timeout': 25, 'purpose': 'check'}),
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
            self.assertIn('验证 · 已通过', card.progress.cget('text'))
            self.assertTrue(Path(card.result['run_dir'], 'result.json').is_file())
            self.assertEqual(card.assistant_index, 3)
            self.assertIn('CHECK_OK', app.sessions.project_runs(identity)[0]['activity'])
            self.assertFalse(card.tools.winfo_manager())
            card.toggle()
            self.assertTrue(card.expanded)
            self.assertTrue(card.tools.winfo_manager())
            self.assertIn('CHECK_OK', card.detail.get('1.0', 'end-1c'))
            source.write_bytes(b'value = 101\n')
            card.review_button.invoke()
            self.assertTrue(card.review_box.winfo_manager())
            self.assertFalse(card.detail.winfo_manager())
            diff = card.review_text.get('1.0', 'end-1c')
            self.assertIn('value.py', diff)
            self.assertIn('-value = 1\n+value = 2\n', diff)
            self.assertNotIn('101', diff)
            self.assertTrue(card.review_text.tag_ranges('added'))
            self.assertTrue(card.review_text.tag_ranges('removed'))
            self.assertEqual(source.read_bytes(), b'value = 101\n')
            source.write_bytes(b'value = 2\n')
            card.review_button.invoke()
            self.assertFalse(card.review_box.winfo_manager())
            self.assertTrue(card.detail.winfo_manager())
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
                restored.undo_select_all.invoke()
                app.project.undo(restored)
                self.assertEqual(source.read_text(), 'value = 1\n')
                self.assertIn('[已撤回文件修改]', restored.activity)
                self.assertIn('需要重新验证', restored.progress.cget('text'))
                annotated = app.history[3]['content']
                self.assertTrue(annotated.startswith('检查完成\n\n'))
                self.assertIn('用户已执行文件撤回。已恢复文件：value.py。', annotated)
                self.assertIn(annotated, app.transcript.get('1.0', 'end'))
                self.assertEqual(app.sessions.messages(identity)[3]['content'], annotated)
                self.assertEqual(app.last_answer, '后续说明完成')
                self.assertEqual(len(app.history), 6)
                restored.review_button.invoke()
                self.assertIn('-value = 1\n+value = 2\n', restored.review_text.get('1.0', 'end-1c'))
                self.assertEqual(source.read_bytes(), b'value = 1\n')
                app.new_conversation()
                app.load_conversation(identity)
                restored = app.project.cards[0]
                self.assertIn('[已撤回文件修改]', restored.activity)
                self.assertIn('需要重新验证', restored.progress.cget('text'))
                self.assertEqual(restored.undo_button.button_state, 'disabled')
                self.assertEqual(app.history[3]['content'], annotated)

            draft(app, '等待批准时停止')
            stopped_model = FixtureModel([('run_command', {'argv': cancelled_command})])
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=stopped_model):
                app.send_chat()
                waiting = settle(app, wait_for_approval=True)
                self.assertEqual(waiting.request['details']['permission_mode'], 'standard')
                self.assertFalse(app.project.set_permission_mode('automatic'))
                self.assertEqual(app.project.permission_mode.get(), 'standard')
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
                    app.sessions, '_write_project_run', side_effect=OSError('fixture write failure')), self.assertLogs():
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

            app.new_conversation()
            app.open_project_assistant()
            with patch('project_chat.filedialog.askdirectory', return_value=str(workspace)):
                app.project.choose_workspace()
            retry_command = [sys.executable, '-B', '-c', 'print("RECOVERY_CHECK_OK")']
            interrupted_model = FixtureModel([
                ('write_file', {'path': 'parent-only.txt', 'content': 'original run'}),
                ('update_plan', {'steps': [{'text': '运行本地检查', 'status': 'in_progress'}]}),
                ('run_command', {'argv': retry_command, 'purpose': 'check'}),
            ])
            draft(app, '运行检查并记录结果')
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=interrupted_model):
                app.send_chat()
                interrupted = settle(app, wait_for_approval=True)
                recovery_id = app.session_id
                checkpoint = app.sessions.project_runs(recovery_id)[0]
                self.assertEqual(checkpoint['assistant_index'], 1)
                self.assertEqual(checkpoint['result']['status'], 'running')
                self.assertEqual(len(app.sessions.messages(recovery_id)), 2)
                self.assertIn('计划进度 0/1', interrupted.progress.cget('text'))
                self.assertIn('尚未验证', interrupted.progress.cget('text'))
                self.assertTrue(checkpoint['result']['run_dir'])
                task_file = Path(checkpoint['result']['run_dir'], 'task.json')
                interrupted_state = task_file.read_bytes()
                app.stop_generation()
                settle(app)
            self.assertEqual(interrupted.result['status'], 'stopped')
            self.assertTrue(interrupted.resume_button.winfo_manager())

            # Reopen the last committed checkpoint as if the app closed before the run could finish.
            task_file.write_bytes(interrupted_state)
            app.sessions.update_project_reply(recovery_id, 1, checkpoint['result'], str(workspace), checkpoint['activity'])
            app.new_conversation()
            with patch('local_project_model.LocalProjectModel', side_effect=AssertionError('restoring ran a task')):
                app.load_conversation(recovery_id)
            recovered = app.project.cards[0]
            self.assertEqual(recovered.result['status'], 'interrupted')
            self.assertIn('上次任务中断', recovered.status.get())
            self.assertIn('运行本地检查', recovered.progress.cget('text'))
            self.assertTrue(recovered.resume_button.winfo_manager())
            self.assertTrue(recovered.recovery_button.winfo_manager())
            self.assertEqual(recovered.undo_button.button_state, 'normal')
            with patch('project_chat.messagebox.askyesno', return_value=False) as question, patch.object(
                    app.project, 'send') as send:
                recovered.recovery_button.invoke()
                send.assert_not_called()
                self.assertIn('RECOVERY_CHECK_OK', question.call_args.args[1])
                self.assertIn('副作用', question.call_args.args[1])
            with patch('project_chat.messagebox.askyesno', return_value=True), patch.object(app.project, 'send') as send:
                recovered.recovery_button.invoke()
                send.assert_called_once_with(resume_from=recovered.result['run_dir'], recovery_confirmed=True)
            self.assertEqual(app.history, app.sessions.messages(recovery_id))
            self.assertEqual(len(app.history), 2)

            resume_draft = '继续，检查前先查看当前工作区'
            draft(app, resume_draft)
            app.project.workspace.set(str(alternate))
            app.project.resume(recovered)
            self.assertFalse(app.busy)
            self.assertIn('原来的项目文件夹', app.status.get())
            self.assertEqual(app.input.get('1.0', 'end-1c'), resume_draft)
            app.project.workspace.set(str(workspace))
            resumed_model = FixtureModel([('run_command', {'argv': retry_command, 'purpose': 'check'}),
                ('update_plan', {'steps': [{'text': '运行本地检查', 'status': 'completed'}]})])
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=resumed_model):
                app.project.resume(recovered)
                resumed = settle(app, wait_for_approval=True)
                self.assertNotIn('RECOVERY_CHECK_OK', resumed.activity)
                self.assertEqual(recovered.resume_button.button_state, 'disabled')
                resumed.decide(True)
                settle(app)
            self.assertIn('RECOVERY_CHECK_OK', resumed.activity)
            self.assertNotEqual(resumed.result['run_dir'], checkpoint['result']['run_dir'])
            self.assertIn('验证 · 已通过', resumed.progress.cget('text'))
            self.assertFalse(resumed.recovery_button.winfo_manager())
            self.assertFalse(resumed.result['changes'])
            self.assertEqual(resumed.undo_button.button_state, 'disabled')
            resumed.toggle()
            self.assertIn('parent-only.txt', resumed.detail.get('1.0', 'end'))
            self.assertEqual(app.history[2]['content'], resume_draft)
            self.assertEqual(app.history, app.sessions.messages(recovery_id))
            self.assertEqual([row['assistant_index'] for row in app.sessions.project_runs(recovery_id)], [1, 3])
            # Recover a stale card containing inherited edits while its own rollback manifest is empty.
            stale_card = {**resumed.result, 'status': 'running', 'changes': [{'path': 'parent-only.txt'}]}
            app.sessions.update_project_reply(recovery_id, 3, stale_card, str(workspace), resumed.activity)
            app.new_conversation()
            app.load_conversation(recovery_id)
            replayed_child = app.project.cards[-1]
            self.assertEqual(replayed_child.undo_button.button_state, 'disabled')
            self.assertEqual(replayed_child.result['changes'], [])
            self.assertEqual((workspace / 'parent-only.txt').read_text(), 'original run')

            # Human verification acknowledges side effects without completing the outstanding test.
            interrupted_card = app.project.cards[0]
            with patch('project_chat.messagebox.askyesno', return_value=True), patch.object(
                    app.manager, 'ready', return_value=True), patch('local_project_model.LocalProjectModel',
                    return_value=FixtureModel([('update_plan', {'steps': [
                        {'text': '运行本地检查', 'status': 'completed'}]})])):
                interrupted_card.recovery_button.invoke()
                settle(app)
            confirmed = app.project.cards[-1]
            self.assertEqual(confirmed.result['status'], 'unfinished')
            self.assertFalse(confirmed.result['task_state']['uncertain'])
            self.assertFalse(confirmed.recovery_button.winfo_manager())
            self.assertIn('检查尚未执行', confirmed.progress.cget('text'))
            self.assertIn('user_confirmation', [row['tool'] for row in confirmed.result['task_state']['observations']])

            legacy_id = app.sessions.create()
            app.sessions.set_project_settings(legacy_id, {'enabled': True, 'workspace': str(workspace),
                                                           'allow_write': False})
            app.load_conversation(legacy_id)
            self.assertEqual(app.project.permission_mode.get(), 'readonly')
            self.assertEqual(app.project.permissions_button.cget('text'), '只看项目 ▾')
            self.assertEqual(permissions.entrycget('允许文件工具修改', 'state'), 'disabled')
            self.assertIn('文件修改和命令执行关闭', app.chat_hint.get())
            readonly_model = FixtureModel([
                ('read_file', {'path': 'value.py'}),
                ('write_file', {'path': 'must-not-write.py', 'content': 'value = 0\n'}),
                ('run_command', {'argv': cancelled_command}),
            ])
            draft(app, '查看项目文件')
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=readonly_model), patch(
                    'project_chat.ActivityCard.show_approval') as approval:
                app.send_chat()
                settle(app)
                approval.assert_not_called()
            self.assertFalse(child_marker.exists())
            self.assertFalse((workspace / 'must-not-write.py').exists())
            self.assertIn('read_file', readonly_model.tool_names[0])
            self.assertFalse({'write_file', 'replace_text', 'run_command'} & readonly_model.tool_names[0])
            self.assertEqual(app.project.cards[-1].result['permission_mode'], 'readonly')

            permissions.invoke('自动执行命令')
            self.assertEqual(app.project.permission_mode.get(), 'automatic')
            self.assertEqual(app.project.permissions_button.cget('text'), '自动执行命令 ▾')
            self.assertIn('可访问项目目录之外的文件和网络', app.chat_hint.get())
            app.new_conversation()
            self.assertEqual(app.project.permission_mode.get(), 'standard')
            app.load_conversation(legacy_id)
            self.assertEqual(app.project.permission_mode.get(), 'automatic')
            with patch('project_chat.filedialog.askdirectory', return_value=str(workspace)):
                app.project.choose_workspace()
            self.assertEqual(app.project.permission_mode.get(), 'automatic')
            auto_model = FixtureModel([('run_command', {'argv': [sys.executable, '-B', '-c', 'print("AUTO_OK")']})])
            draft(app, '运行一条本地测试命令')
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=auto_model), patch(
                    'project_chat.ActivityCard.show_approval') as approval:
                app.send_chat()
                settle(app)
                approval.assert_not_called()
            automatic = app.project.cards[-1]
            self.assertEqual(automatic.result['permission_mode'], 'automatic')
            self.assertIn('自动运行命令', automatic.activity)
            self.assertIn('AUTO_OK', automatic.activity)
            self.assertIn('自动执行命令', automatic.progress.cget('text'))
            self.assertEqual(app.sessions.project_runs(legacy_id)[-1]['result']['permission_mode'], 'automatic')
            with patch('project_chat.filedialog.askdirectory', return_value=str(alternate)):
                app.project.choose_workspace()
            self.assertEqual(app.project.permission_mode.get(), 'standard')
            self.assertEqual(app.sessions.project_settings(legacy_id)['permission_mode'], 'standard')

            app.load_conversation(recovery_id)
            recovered = app.project.cards[0]
            self.assertEqual(recovered.result['permission_mode'], 'standard')
            self.assertTrue(app.project.set_permission_mode('readonly'))
            restart_model = FixtureModel([('run_command', {'argv': cancelled_command})])
            with patch.object(app.manager, 'ready', return_value=True), patch(
                    'local_project_model.LocalProjectModel', return_value=restart_model), patch(
                    'project_chat.ActivityCard.show_approval') as approval:
                app.project.resume(recovered)
                settle(app)
                approval.assert_not_called()
            self.assertFalse(child_marker.exists())
            restarted = app.project.cards[-1]
            self.assertEqual(restarted.result['permission_mode'], 'readonly')
            self.assertEqual(restarted.result['task_state']['parent_run'], Path(recovered.result['run_dir']).name)

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
