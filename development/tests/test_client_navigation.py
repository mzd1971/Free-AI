"""Settings always offer a visible route back without changing the conversation."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class NativeNavigationScenario(unittest.TestCase):
    def runTest(self):
        import tkinter as tk
        from client_app import ClientApp
        from ui_kit import FlatButton, set_text_size

        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = ClientApp(root)
        self.addCleanup(lambda: root.after_cancel(app.poll_id))
        errors = []
        root.report_callback_exception = lambda kind, error, trace: errors.append(str(error))
        root.geometry('1080x760+9000+9000')
        root.deiconify()
        root.update()
        identity = app.sessions.create()
        app.load_conversation(identity)
        app.input.insert('1.0', '还没发送的项目要求')
        app.api_panel.model.set('尚未保存的设置')
        transcript = app.transcript.get('1.0', 'end-1c')

        for collapsed in (False, True):
            if bool(app.sidebar.winfo_manager()) == collapsed:
                app.toggle_sidebar()
            for page in (2, 1):
                app.select_page(page)
                if page == 2 and app.settings_panel.navigation.winfo_manager():
                    app.toggle_sidebar()
                set_text_size(root, 'extra_large')
                root.update()
                visible_back = [widget for widget in app.header.winfo_children()
                                if isinstance(widget, FlatButton) and widget.winfo_viewable()
                                and widget.cget('text') == '返回对话']
                self.assertEqual(len(visible_back), 1,
                                 '设置/模型页缺少可见的返回对话入口，折叠菜单后不能直接返回。')
                back = visible_back[0]
                self.assertLessEqual(back.winfo_rootx() + back.winfo_width(),
                                     root.winfo_rootx() + root.winfo_width())
                app.busy = True
                back.invoke()
                root.update()
                self.assertEqual(app.current_page, 0)
                self.assertEqual(app.session_id, identity)
                self.assertEqual(app.input.get('1.0', 'end-1c'), '还没发送的项目要求')
                self.assertEqual(app.transcript.get('1.0', 'end-1c'), transcript)
                self.assertEqual(app.api_panel.model.get(), '尚未保存的设置')
                self.assertTrue(app.busy)
                self.assertFalse(app.generation_cancel.is_set())
                self.assertEqual(bool(app.sidebar.winfo_manager()), not collapsed)
                self.assertFalse(back.winfo_manager())
                app.busy = False
        self.assertFalse(errors, errors)


class NavigationTests(unittest.TestCase):
    def test_return_from_settings_keeps_conversation_and_draft(self):
        with tempfile.TemporaryDirectory(prefix='navigation-', dir=ROOT / 'development/tmp') as data:
            executable = ROOT / 'runtime/python/python.exe'
            code = ('import sys, unittest; sys.path.insert(0,"development/tests"); '
                    'from test_client_navigation import NativeNavigationScenario; '
                    'r=unittest.TextTestRunner(verbosity=2).run(NativeNavigationScenario()); '
                    'sys.exit(not r.wasSuccessful())')
            result = subprocess.run([str(executable), '-E', '-s', '-B', '-X', 'utf8', '-c', code],
                cwd=ROOT, env=dict(os.environ, AI_TRAINING_DATA=data),
                capture_output=True, text=True, encoding='utf-8', timeout=35)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(NavigationTests)


if __name__ == '__main__':
    unittest.main()
