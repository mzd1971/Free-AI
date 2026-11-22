"""Exercise the unified assistant through the native composer in isolated storage."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_project_ui import BUNDLED_PYTHON, FixtureModel, ROOT


class AssistantFixtureModel(FixtureModel):
    max_output_tokens = 2048

    def complete_tools(self, messages, tools, **kwargs):
        self.context = kwargs['context']
        return super().complete_tools(messages, tools, **kwargs)


class NativeUnifiedChatScenario(unittest.TestCase):
    def runTest(self):
        import tkinter as tk

        from app_state import Sessions
        from client_app import ClientApp
        from harness_settings import save_preferences
        from paths import ARTIFACTS
        from project_checks import load_project_check
        from project_chat import assistant_workspace
        from project_snapshot import start_snapshot

        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        failures = []
        root.report_callback_exception = lambda kind, error, traceback: failures.append(error)
        app = ClientApp(root)
        self.addCleanup(lambda: root.after_cancel(app.poll_id))
        app.backend, app.selected_model = 'local', 'text-default'
        project = app.project
        menu = project.permissions_menu
        web_option = '允许联网查资料'
        tool_option = '自动使用工具'
        utilities = {'calculate', 'check_json', 'current_time', 'update_plan', 'remember_memory'}

        def draft(task):
            app.input.delete('1.0', 'end')
            app.input.insert('1.0', task)

        def send(task):
            draft(task)
            app.send_chat()
            self.assertTrue(app.busy, app.status.get())

        def wait_for(predicate):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                root.update()
                self.assertFalse(failures, failures)
                if predicate():
                    return
                time.sleep(.01)
            self.fail('Tk task did not reach the expected state')

        def settle():
            wait_for(lambda: not app.busy)
            return project.cards[-1] if project.cards else None

        self.assertTrue(project.permissions_button.winfo_manager())
        with patch.object(menu, 'tk_popup') as popup:
            project.permissions_button.invoke()
            popup.assert_called_once()
        self.assertEqual(project.chat_button.cget('text'), '智能助手')
        self.assertTrue(project.assistant_enabled.get())
        self.assertFalse(project.allow_web.get())
        for option in ('自动执行命令', '允许文件工具修改', '在编辑器中打开项目',
                       '在终端中打开项目', '固定项目检查…'):
            self.assertEqual(menu.entrycget(option, 'state'), 'disabled')
        with self.assertRaises(ValueError):
            assistant_workspace('../workspace', create=True)
        legacy_identity = app.sessions.create()
        app.load_conversation(legacy_identity)
        self.assertTrue(project.assistant_enabled.get())
        app.new_conversation()
        draft('先保留这条聊天草稿')
        with patch.object(app.manager, 'ready', return_value=False), patch.object(app, 'show_capabilities') as picker:
            app.send_chat()
            picker.assert_called_once_with('models')
        app.backend = 'api'
        with patch.object(app, 'make_api_client', side_effect=ValueError('test config')), patch.object(
                app, 'show_api_settings') as settings:
            app.send_chat()
            settings.assert_called_once_with()
        app.backend = 'local'
        self.assertFalse(app.busy)
        self.assertIsNone(app.session_id)
        self.assertEqual(app.input.get('1.0', 'end-1c'), '先保留这条聊天草稿')
        menu.invoke(tool_option)
        with patch.object(app.manager, 'ready', return_value=True), patch(
                'qwen_chat.chat', return_value={'answer': '普通回答'}) as chat:
            send('使用关闭工具后的聊天')
            self.assertIsNone(settle())
            chat.assert_called_once()
            first_plain_session = app.session_id
        app.new_conversation()
        self.assertTrue(project.assistant_enabled.get())
        app.load_conversation(first_plain_session)
        self.assertFalse(project.assistant_enabled.get())
        app.new_conversation()
        workspace = ARTIFACTS / 'sample_project'
        workspace.mkdir()
        project_file = workspace / 'value.txt'
        project_file.write_text('project fixture', encoding='utf-8')
        harness_executable = ARTIFACTS / 'harness.exe'
        harness_executable.touch()
        save_preferences(app.sessions, {'engine': 'harness', 'executable': str(harness_executable)})

        preference = '今后回复时使用简体中文。'
        offline = AssistantFixtureModel([
            ('calculate', {'expression': '(6 + 4) * 3'}),
            ('check_json', {'text': '{"active": true}'}),
            ('remember_memory', {'title': '回复语言', 'content': preference, 'kind': 'preference'}),
        ], final='结果为 30，JSON 有效。')
        with patch.object(app.manager, 'ready', return_value=True), patch('local_project_model.LocalProjectModel') as model, \
                patch('agent_web.WebTools') as web, patch('harness_agent.HarnessAgent') as harness, \
                patch('project_snapshot.start_snapshot', wraps=start_snapshot) as snapshot, \
                patch('project_chat.load_project_check', wraps=load_project_check) as fixed:
            model.return_value = offline
            send('计算 (6 + 4) * 3，检查 JSON，并记住我的偏好。' + preference)
            card = settle()
            identity = app.session_id
            self.assertEqual(card.result['capability_mode'], 'assistant')
            self.assertEqual(card.result['status'], 'completed', card.result['answer'])
            self.assertEqual(offline.tool_names[0], utilities)
            self.assertEqual(offline.context, 16384)
            self.assertEqual(card.status.get(), '已回复')
            self.assertFalse(card.result['allow_web'])
            self.assertEqual(project.workspace.get(), '')
            self.assertEqual(Path(card.workspace), ARTIFACTS / 'assistant_workspaces' / identity)
            self.assertEqual(list(Path(card.workspace).iterdir()), [])
            self.assertTrue(Path(card.result['run_dir']).is_relative_to(ARTIFACTS / 'project_runs'))
            self.assertNotIn('assistant_workspaces', str(offline.requests))
            self.assertEqual([row['content'] for row in app.personal_memory.list()], [preference])
            results = [json.loads(row['content']) for row in offline.requests[-1] if row['role'] == 'tool']
            self.assertEqual(results[0]['evidence']['value'], 30)
            self.assertTrue(results[1]['tool_execution_verified'])
            card.expand_button.invoke()
            self.assertIn('保存长期记忆', card.detail.get('1.0', 'end'))
            self.assertFalse(card.check_tools.winfo_manager())
            self.assertFalse(card.undo_button.winfo_manager())
            self.assertFalse(card.review_button.winfo_manager())
            with patch('project_chat.filedialog.askdirectory', return_value=card.workspace):
                project.folder_button.invoke()
            self.assertFalse(project.enabled.get())
            self.assertFalse(project.workspace.get())
            web.assert_not_called()

            menu.invoke(web_option)
            self.assertTrue(project.allow_web.get())
            self.assertIn('允许联网查资料', app.chat_hint.get())
            web.return_value.search.return_value = {'query': '参考', 'results': [
                {'title': '指南', 'url': 'https://example.org/guide', 'snippet': '结果'}]}
            model.return_value = AssistantFixtureModel([('web_search', {'query': '参考'})], final='已找到指南。')
            send('查找参考文档')
            online_card = settle()
            self.assertEqual(model.return_value.tool_names[0], utilities | {'web_search', 'web_fetch'})
            self.assertTrue(online_card.result['allow_web'])
            self.assertEqual(online_card.result['web_sources'][0]['url'], 'https://example.org/guide')
            self.assertIn(preference, str(model.return_value.requests[0]))

            csv_path = ARTIFACTS / 'counts.csv'
            csv_path.write_text('count\n2\n4\n', encoding='utf-8')
            image_path = ARTIFACTS / 'image.png'
            with patch('client_app.filedialog.askopenfilename', return_value=str(csv_path)):
                app.choose_skill_file(assistant=True)
            draft('分析附件中的数值')
            history_before = list(app.history)
            for skill, use_tools, picture in (('general', False, None), ('writing', True, None),
                                               ('general', True, str(image_path))):
                with self.subTest(skill=skill, use_tools=use_tools, picture=picture):
                    app.select_skill(skill)
                    if project.assistant_enabled.get() != use_tools:
                        menu.invoke(tool_option)
                    app.image_path = picture
                    app.send_chat()
                    self.assertFalse(app.busy)
                    self.assertEqual(app.history, history_before)
                    self.assertIs(project.cards[-1], online_card)
                    self.assertEqual(app.skill_file, str(csv_path))
                    self.assertEqual(app.input.get('1.0', 'end-1c'), '分析附件中的数值')
                    self.assertIn('已选 CSV 仍保留', app.chat_hint.get())
            app.image_path = None
            model.return_value = AssistantFixtureModel([('current_time', {})], final='已读取本机时间。')
            send('现在几点？')
            clock_card = settle()
            self.assertTrue(clock_card.assistant)
            self.assertTrue(clock_card.result['csv_attachment'])
            self.assertIn('summarize_csv', model.return_value.tool_names[0])

            entered = threading.Event()
            released = threading.Event()
            blocked = AssistantFixtureModel()
            complete = blocked.complete_tools

            def wait_for_stop(*args, **kwargs):
                entered.set()
                if not released.wait(10):
                    raise AssertionError('stopped fixture was not released')
                return complete(*args, **kwargs)

            blocked.complete_tools = wait_for_stop
            model.return_value = blocked
            with patch('client_app.filedialog.askopenfilename', return_value=str(csv_path)):
                app.choose_skill_file(assistant=True)
            send('分析附件中的数值')
            try:
                wait_for(entered.is_set)
                self.assertEqual(menu.entrycget(web_option, 'state'), 'disabled')
                app.stop_generation()
            finally:
                released.set()
            stopped = settle()
            self.assertEqual(stopped.result['status'], 'stopped')
            self.assertTrue(stopped.can_resume())
            self.assertTrue(stopped.result['csv_attachment'])
            self.assertIn('summarize_csv', blocked.tool_names[0])
            harness.assert_not_called()
            snapshot.assert_not_called()
            fixed.assert_not_called()

            menu.invoke(tool_option)
            app.new_conversation()
            self.assertTrue(project.assistant_enabled.get())
            self.assertFalse(project.allow_web.get())
            app.sessions = Sessions(ARTIFACTS)
            app.load_conversation(identity)
            self.assertFalse(project.assistant_enabled.get())
            self.assertTrue(project.allow_web.get())
            restored_online = next(card for card in project.cards if card.assistant_index == online_card.assistant_index)
            self.assertEqual(restored_online.result['web_sources'], online_card.result['web_sources'])
            restored = project.cards[-1]
            self.assertTrue(restored.assistant)
            self.assertTrue(restored.can_resume())
            restored.resume_button.invoke()
            self.assertFalse(app.busy)
            self.assertIn('重新选择', app.status.get())

            menu.invoke(web_option)
            with patch('project_chat.filedialog.askdirectory', return_value=str(workspace)):
                project.folder_button.invoke()
            self.assertTrue(project.enabled.get())
            self.assertEqual(menu.entrycget('允许文件工具修改', 'state'), 'normal')
            app.skill_file = str(csv_path)
            model.return_value = AssistantFixtureModel([('summarize_csv', {})], final='已统计两行。')
            restored.resume_button.invoke()
            resumed = settle()
            self.assertEqual(resumed.result['status'], 'completed')
            self.assertTrue(resumed.assistant)
            self.assertEqual(resumed.workspace, restored.workspace)
            self.assertEqual(resumed.result['task_state']['parent_run'], Path(restored.result['run_dir']).name)
            self.assertEqual(model.return_value.tool_names[0], utilities | {'summarize_csv'})
            self.assertNotIn(ARTIFACTS.name, str(model.return_value.requests))
            observation = json.loads(next(row['content'] for row in model.return_value.requests[-1] if row['role'] == 'tool'))
            self.assertEqual(observation['evidence']['data_rows'], 2)
            harness.assert_not_called()
            snapshot.assert_not_called()
            fixed.assert_not_called()

            save_preferences(app.sessions, {'engine': 'builtin'})
            model.return_value = FixtureModel([('read_file', {'path': project_file.name})])
            send('查看当前项目')
            project_card = settle()
            self.assertFalse(project_card.assistant)
            self.assertEqual(Path(project_card.workspace), workspace)
            self.assertTrue(utilities | {'read_file', 'run_command'} <= model.return_value.tool_names[0])
            project_card.expand_button.invoke()
            self.assertTrue(project_card.check_tools.winfo_manager())
            self.assertTrue(project_card.review_button.winfo_manager())
            snapshot.assert_called_once()
            fixed.assert_called_once()


class UnifiedChatUITest(unittest.TestCase):
    def test_native_assistant_and_project_workflow(self):
        with tempfile.TemporaryDirectory(prefix='ui-unified-chat-', dir=ROOT / 'development') as scratch:
            executable = str(BUNDLED_PYTHON) if BUNDLED_PYTHON.is_file() else sys.executable
            code = ('import sys, unittest; from pathlib import Path; '
                    'sys.path.insert(0, str(Path("development/tests").resolve())); '
                    'from test_unified_chat_ui import NativeUnifiedChatScenario; '
                    'result = unittest.TextTestRunner(verbosity=2).run(NativeUnifiedChatScenario()); '
                    'sys.exit(not result.wasSuccessful())')
            result = subprocess.run([executable, '-E', '-s', '-B', '-X', 'utf8', '-c', code], cwd=ROOT,
                                    env=dict(os.environ, AI_TRAINING_DATA=scratch), capture_output=True,
                                    text=True, encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(UnifiedChatUITest)
