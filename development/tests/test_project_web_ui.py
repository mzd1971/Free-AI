"""Exercise project web permission and sources through the native Tk conversation."""
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


class NativeProjectWebScenario(unittest.TestCase):
    def runTest(self):
        import tkinter as tk

        from agent_web import WebError
        from app_state import Sessions
        from client_app import ClientApp
        from harness_settings import save_preferences
        from project_agent import ProjectAgent

        scratch = Path(os.environ['AI_TRAINING_DATA'])
        workspace = scratch / 'project'
        workspace.mkdir()
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        errors = []
        root.report_callback_exception = lambda kind, error, traceback: errors.append(error)
        app = ClientApp(root)
        self.addCleanup(lambda: root.after_cancel(app.poll_id))
        app.backend, app.selected_model = 'local', 'text-default'
        project = app.project
        first = app.sessions.create()
        second = app.sessions.create()
        app.load_conversation(first)
        with patch('project_chat.filedialog.askdirectory', return_value=str(workspace)):
            project.folder_button.invoke()
        self.assertTrue(project.permissions_button.winfo_manager())
        with patch.object(project.permissions_menu, 'tk_popup') as popup:
            project.permissions_button.invoke()
            popup.assert_called_once()
        menu = project.permissions_menu
        option = '允许联网查资料'
        self.assertEqual(menu.entrycget(option, 'state'), 'normal')
        self.assertFalse(project.allow_web.get())

        def send(task):
            app.input.delete('1.0', 'end')
            app.input.insert('1.0', task)
            app.send_chat()
            self.assertTrue(app.busy, app.status.get())

        def settle():
            deadline = time.monotonic() + 10
            while app.busy and time.monotonic() < deadline:
                root.update()
                self.assertFalse(errors, errors)
                time.sleep(.01)
            self.assertFalse(app.busy, 'project generation did not settle')
            self.assertFalse(errors, errors)
            return project.cards[-1]

        found = 'https://example.org/guide'
        other = 'https://example.org/results'
        read = 'https://docs.example.org/reference'
        unavailable = 'https://example.org/unavailable'
        fetched_at = '2026-11-22T08:00:00+00:00'
        with patch.object(app.manager, 'ready', return_value=True), patch(
                'project_agent.ProjectAgent', wraps=ProjectAgent) as runner, patch(
                'agent_web.WebTools') as web, patch('local_project_model.LocalProjectModel') as model, patch(
                'project_chat.ActivityCard.show_approval') as approval, patch('webbrowser.open') as browser, patch(
                'project_chat.os.startfile', create=True) as open_file:
            offline = FixtureModel()
            model.return_value = offline
            send('查看本地项目')
            card = settle()
            self.assertIs(runner.call_args.kwargs['allow_web'], False)
            self.assertFalse({'web_search', 'web_fetch'} & offline.tool_names[0])
            self.assertIs(card.result['allow_web'], False)
            web.assert_not_called()

            menu.invoke(option)
            self.assertTrue(project.allow_web.get())
            self.assertIs(app.sessions.project_settings(first)['allow_web'], True)
            self.assertIn('内置执行可自动搜索和读取公开网页', app.chat_hint.get())
            app.load_conversation(second)
            self.assertFalse(project.allow_web.get())
            self.assertFalse(project.menu_allow_web.get())
            app.sessions = Sessions(scratch)
            app.load_conversation(first)
            self.assertTrue(project.allow_web.get())
            self.assertTrue(project.menu_allow_web.get())
            model.return_value = FixtureModel([
                ('web_search', {'query': '参考文档'}), ('web_fetch', {'url': found}),
                ('web_fetch', {'url': unavailable}),
            ])
            web.return_value.search.return_value = {'query': '参考文档', 'provider': 'fixture', 'fetched_at': fetched_at,
                'results': [{'title': '指南', 'url': found, 'snippet': '搜索摘要'},
                            {'title': '搜索帮助', 'url': other, 'snippet': '搜索摘要'},
                            {'title': '指南索引', 'url': read, 'snippet': '搜索摘要'},
                            {'title': '指南', 'url': found, 'snippet': '重复结果'}]}
            release = threading.Event()

            def fetch(url, **options):
                if url == unavailable:
                    if not release.wait(10):
                        raise AssertionError('checkpoint fixture was not released')
                    raise WebError('读取公开网页超时。')
                return {'title': '官方参考', 'url': read, 'text': '此网页正文仅供模型参考',
                        'links': [], 'truncated': True, 'fetched_at': fetched_at}

            web.return_value.fetch.side_effect = fetch
            send('搜索公开网页并查阅参考文档')
            self.assertEqual(menu.entrycget(option, 'state'), 'disabled')
            menu.invoke(option)
            self.assertTrue(project.menu_allow_web.get())
            project.menu_allow_web.set(False)
            project.save_web_permission()
            self.assertTrue(project.allow_web.get())
            self.assertTrue(project.menu_allow_web.get())
            try:
                deadline = time.monotonic() + 5
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    checkpoint = app.sessions.project_runs(first)[-1]
                    if 'page' in {item['kind'] for item in checkpoint['result'].get('web_sources', [])}:
                        break
                    time.sleep(.01)
                else:
                    self.fail('successful web tools did not save sources before the run finished')
                self.assertTrue(app.busy)
                self.assertEqual(checkpoint['result']['status'], 'running')
                self.assertEqual({(row['url'], row['kind']) for row in checkpoint['result']['web_sources']},
                                 {(found, 'search'), (other, 'search'), (read, 'search'), (read, 'page')})
                self.assertEqual(len(checkpoint['result']['web_sources']), 4)
                self.assertNotIn('此网页正文仅供模型参考', str(checkpoint))
                self.assertIn('已读取网页 1 条', project.active.progress.cget('text'))
            finally:
                release.set()
            card = settle()
            self.assertEqual(menu.entrycget(option, 'state'), 'normal')
            self.assertIs(runner.call_args.kwargs['allow_web'], True)
            self.assertEqual(card.result['status'], 'unfinished')
            self.assertTrue({'web_search', 'web_fetch'} <= model.return_value.tool_names[0])
            approval.assert_not_called()
            self.assertIn('搜索结果 3 条', card.progress.cget('text'))
            self.assertIn('已读取网页 1 条', card.progress.cget('text'))
            card.expand_button.invoke()
            body = card.detail.get('1.0', 'end-1c')
            for content in (found, other, read, '搜索结果 · 搜索帮助', '已读取网页 · 官方参考',
                            '搜索词 · 参考文档', '原因 · 读取公开网页超时。', '网页文字已达到读取上限。'):
                self.assertIn(content, body)
            self.assertNotIn('此网页正文仅供模型参考', body)
            app.new_conversation()
            self.assertFalse(project.allow_web.get())
            app.load_conversation(first)
            restored = project.cards[-1]
            self.assertEqual(restored.result['web_sources'], card.result['web_sources'])
            self.assertIn('已读取网页 1 条', restored.progress.cget('text'))
            restored.expand_button.invoke()
            self.assertIn('原因 · 读取公开网页超时。', restored.detail.get('1.0', 'end-1c'))

            # Restore the last checkpoint as if the process ended before the final response.
            app.sessions.update_project_reply(first, checkpoint['assistant_index'], checkpoint['result'],
                                              checkpoint['workspace'], checkpoint['activity'])
            app.new_conversation()
            app.load_conversation(first)
            interrupted = project.cards[-1]
            self.assertEqual(interrupted.result['status'], 'interrupted')
            self.assertEqual(interrupted.result['web_sources'], checkpoint['result']['web_sources'])
            self.assertEqual(interrupted.result['web_sources'], restored.result['web_sources'])
            self.assertIn('已读取网页 1 条', interrupted.progress.cget('text'))
            interrupted.expand_button.invoke()
            self.assertIn('本轮联网来源\n', interrupted.detail.get('1.0', 'end-1c'))
            self.assertIn('已读取网页 · 官方参考\n' + read, interrupted.detail.get('1.0', 'end-1c'))

            executable = scratch / 'harness.exe'
            executable.touch()
            save_preferences(app.sessions, {'engine': 'harness', 'executable': str(executable)})
            with patch('harness_agent.HarnessAgent') as harness:
                harness.return_value.run.return_value = {'status': 'review', 'answer': '待验收', 'run_dir': '',
                                                         'engine': 'harness', 'changes': []}
                send('检查项目')
                harness_card = settle()
                self.assertNotIn('allow_web', harness.call_args.kwargs)
                self.assertNotIn('allow_web', harness_card.result)
                self.assertTrue(project.allow_web.get())
            browser.assert_not_called()
            open_file.assert_not_called()

        with self.assertRaises(ValueError):
            app.sessions.set_project_settings(first, {'allow_web': 'true'})
        menu.invoke(option)
        self.assertIs(app.sessions.project_settings(first)['allow_web'], False)


class ProjectWebUITest(unittest.TestCase):
    def test_native_project_web_workflow(self):
        with tempfile.TemporaryDirectory(prefix='ui-project-web-', dir=ROOT / 'development') as scratch:
            executable = str(BUNDLED_PYTHON) if BUNDLED_PYTHON.is_file() else sys.executable
            code = ('import sys, unittest; from pathlib import Path; '
                    'sys.path.insert(0, str(Path("development/tests").resolve())); '
                    'from test_project_web_ui import NativeProjectWebScenario; '
                    'result = unittest.TextTestRunner(verbosity=2).run(NativeProjectWebScenario()); '
                    'sys.exit(not result.wasSuccessful())')
            result = subprocess.run([executable, '-E', '-s', '-B', '-X', 'utf8', '-c', code], cwd=ROOT,
                                    env=dict(os.environ, AI_TRAINING_DATA=scratch), capture_output=True,
                                    text=True, encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(ProjectWebUITest)
