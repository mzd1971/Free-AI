"""Exercise the native settings page using isolated preferences and API data."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
BUNDLED_PYTHON = ROOT / 'runtime' / 'python' / 'python.exe'


class NativeSettingsScenario(unittest.TestCase):
    def runTest(self):
        import tkinter as tk

        from client_app import ClientApp
        from coding_tools import load_preferences
        from paths import ARTIFACTS
        from ui_kit import FlatButton

        scratch = Path(os.environ['AI_TRAINING_DATA'])
        self.assertEqual(ARTIFACTS, scratch)
        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.withdraw()
        with patch.object(tk, 'Toplevel', side_effect=AssertionError('settings opened another window')):
            app = ClientApp(root)
        self.addCleanup(lambda: root.after_cancel(app.poll_id))
        callback_errors = []
        root.report_callback_exception = lambda kind, error, traceback: callback_errors.append(error)

        def visible_sections():
            return {category: sum(bool(widget.winfo_manager()) for widget, _ in sections)
                    for category, sections in panel.sections.items() if any(widget.winfo_manager() for widget, _ in sections)}

        def find_button(parent, title):
            pending = [parent]
            while pending:
                widget = pending.pop()
                if isinstance(widget, FlatButton) and widget.cget('text') == title:
                    return widget
                pending.extend(widget.winfo_children())
            self.fail(f'Missing button: {title}')

        panel = app.settings_panel
        app.select_page(2)
        root.geometry('940x680+2500+1400')
        root.deiconify()
        root.update()
        self.assertEqual(app.current_page, 2)
        self.assertFalse(app.sidebar.winfo_manager())
        self.assertTrue(panel.navigation.winfo_manager())
        app.toggle_sidebar()
        self.assertFalse(panel.navigation.winfo_manager())
        app.toggle_sidebar()
        self.assertTrue(panel.navigation.winfo_manager())
        app.select_page(0)
        self.assertTrue(app.sidebar.winfo_manager())
        app.select_page(2)
        self.assertEqual(list(panel.category_buttons),
                         ['general', 'coding', 'models', 'appearance', 'shortcuts', 'storage', 'about'])
        self.assertEqual(visible_sections(), {'general': 2})
        self.assertEqual(panel.heading.cget('text'), '常规与权限')
        self.assertEqual(app.project.permission_mode.get(), 'standard')
        self.assertTrue(panel.permission_buttons['standard'].cget('text').startswith('✓'))

        for query in ('长期记忆', '外置上下文', '记忆'):
            panel.query.set(query)
            root.update_idletasks()
            expected_sections = {'general': 1}
            if query != '外置上下文':
                expected_sections['shortcuts'] = 1
            self.assertEqual(visible_sections(), expected_sections)
            self.assertTrue(panel.memory_options[0].winfo_viewable())
        self.assertEqual(panel.memory_options[0].cget('variable'), app.memories.options[0].cget('variable'))
        panel.memory_options[0].invoke()
        self.assertFalse(app.memories.read_enabled.get())
        app.memories.options[0].invoke()
        self.assertTrue(app.memories.read_enabled.get())
        find_button(panel, '管理长期记忆').invoke()
        self.assertEqual(app.current_page, 0)
        self.assertTrue(app.memories.opened)
        self.assertTrue(app.memories.frame.winfo_manager())
        app.open_memory()
        app.select_page(2)

        panel.query.set('api 密钥')
        self.assertEqual(visible_sections(), {'models': 1})
        api = app.api_panel
        api.model.set('fixture-draft-model')
        panel.query.set('powershell')
        self.assertEqual(visible_sections(), {'coding': 1})
        panel.query.set('没有这个功能')
        self.assertEqual(visible_sections(), {})
        self.assertTrue(panel.empty_message.winfo_manager())
        app.show_api_settings()
        root.update()
        self.assertEqual(panel.query.get(), '')
        self.assertEqual(visible_sections(), {'models': 2})
        self.assertIs(app.api_panel, api)
        self.assertEqual(api.model.get(), 'fixture-draft-model')
        self.assertEqual([button.grid_info()['row'] for button in api.connection_buttons], [0, 0, 0, 0])
        for button in api.connection_buttons:
            self.assertLessEqual(button.winfo_x() + button.winfo_width(), button.master.winfo_width())
        # The client no longer reserves a permanent icon rail; deliberately
        # narrow the content enough to exercise the two-row button layout.
        panel.navigation.configure(width=400)
        root.update()
        self.assertEqual([button.grid_info()['row'] for button in api.connection_buttons], [0, 0, 1, 1])
        panel.navigation.configure(width=156)
        root.geometry('1350x720+2500+1400')
        root.update()
        self.assertEqual([button.grid_info()['row'] for button in api.connection_buttons], [0, 0, 0, 0])

        first = app.sessions.create()
        second = app.sessions.create()
        app.load_conversation(first)
        panel.category_buttons['general'].invoke()
        panel.permission_buttons['readonly'].invoke()
        self.assertEqual(app.sessions.project_settings(first)['permission_mode'], 'readonly')
        self.assertTrue(panel.permission_buttons['readonly'].cget('text').startswith('✓'))
        release = threading.Event()
        app.submit('测试进行中', release.wait, lambda result: None)
        try:
            self.assertTrue(app.busy)
            self.assertEqual(panel.permission_buttons['automatic'].cget('state'), 'disabled')
            panel.permission_buttons['automatic'].invoke()
            self.assertEqual(app.project.permission_mode.get(), 'readonly')
        finally:
            release.set()
        deadline = time.monotonic() + 4
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        self.assertFalse(app.busy)
        self.assertEqual(panel.permission_buttons['automatic'].cget('state'), 'normal')

        app.load_conversation(second)
        self.assertEqual(app.project.permission_mode.get(), 'standard')
        app.load_conversation(first)
        self.assertTrue(panel.permission_buttons['readonly'].cget('text').startswith('✓'))
        panel.permission_buttons['automatic'].invoke()
        self.assertEqual(app.sessions.project_settings(first)['permission_mode'], 'automatic')
        self.assertEqual(app.sessions.project_settings(second), {})
        app.new_conversation()
        self.assertEqual(app.project.permission_mode.get(), 'standard')

        panel.category_buttons['coding'].invoke()
        self.assertEqual(visible_sections(), {'coding': 2})
        self.assertFalse(panel.editor_path_row.winfo_manager())
        panel.editor.set('自定义程序')
        panel.shell.set('命令提示符（cmd）')
        panel.coding_changed()
        self.assertTrue(panel.editor_path_row.winfo_manager())
        editor = scratch / '测试 编辑器.exe'
        editor.touch()
        with patch('client_settings.filedialog.askopenfilename', return_value=str(editor)):
            find_button(panel, '浏览…').invoke()
        self.assertEqual(panel.editor_path.get(), str(editor))
        self.assertEqual(load_preferences(app.sessions), {'editor': 'default', 'shell': 'powershell', 'editor_path': ''})
        find_button(panel, '保存偏好').invoke()
        saved = {'editor': 'custom', 'shell': 'cmd', 'editor_path': str(editor)}
        self.assertEqual(load_preferences(app.sessions), saved)

        workspace = scratch / '项目 文件夹'
        workspace.mkdir()
        app.project.workspace.set(str(workspace))
        panel.editor.set('Visual Studio Code')
        panel.coding_changed()
        with patch('coding_tools.launch_editor', return_value={'tool': '测试 编辑器.exe'}) as launch_editor, patch(
                'coding_tools.launch_terminal', return_value={'tool': 'cmd.exe'}) as launch_terminal:
            find_button(panel, '用编辑器打开项目').invoke()
            find_button(panel, '在项目中打开终端').invoke()
        launch_editor.assert_called_once_with(str(workspace), saved)
        launch_terminal.assert_called_once_with(str(workspace), saved)

        panel.category_buttons['appearance'].invoke()
        self.assertEqual(visible_sections(), {'appearance': 2})
        app.skin_choice.set('经典深色')
        app.skin_picker.event_generate('<<ComboboxSelected>>')
        self.assertEqual(app.sessions.get('client_theme'), 'dark')
        self.assertIn('重新打开', app.skin_notice.get())
        for category in ('storage', 'about'):
            panel.category_buttons[category].invoke()
            self.assertEqual(visible_sections(), {category: 2})
        self.assertFalse(callback_errors, callback_errors)


class ClientSettingsTests(unittest.TestCase):
    def test_native_settings(self):
        with tempfile.TemporaryDirectory(prefix='ui-settings-', dir=ROOT / 'development') as scratch:
            env = dict(os.environ, AI_TRAINING_DATA=scratch)
            executable = str(BUNDLED_PYTHON) if BUNDLED_PYTHON.is_file() else sys.executable
            code = ('import sys, unittest; from pathlib import Path; '
                    'sys.path.insert(0, str(Path("development/tests").resolve())); '
                    'from test_client_settings import NativeSettingsScenario; '
                    'result = unittest.TextTestRunner(verbosity=2).run(NativeSettingsScenario()); '
                    'sys.exit(not result.wasSuccessful())')
            result = subprocess.run([executable, '-E', '-s', '-B', '-X', 'utf8', '-c', code],
                cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(ClientSettingsTests)


if __name__ == '__main__':
    unittest.main()
