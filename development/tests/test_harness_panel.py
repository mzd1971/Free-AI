"""Native Harness settings with isolated app storage and simulated component I/O."""
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


def exercise_settings(test):
    import tkinter as tk

    import harness_settings
    from client_app import ClientApp
    from harness_panel import ENGINES, OFFICIAL_GUIDE

    scratch = Path(os.environ['AI_TRAINING_DATA'])
    fixture = scratch / 'dsh.exe'
    fixture.touch()
    root = tk.Tk()
    root.withdraw()
    test.addCleanup(root.destroy)
    callback_errors = []
    root.report_callback_exception = lambda kind, error, traceback: callback_errors.append(error)
    with patch('harness_settings.probe') as probe, patch('harness_settings.install_component') as install:
        app = ClientApp(root)
        test.addCleanup(lambda: root.after_cancel(app.poll_id))
        settings = app.settings_panel
        panel = settings.harness
        app.select_page(2)
        settings.select_category('coding')
        test.assertEqual(ENGINES[panel.engine.get()], 'builtin')
        test.assertFalse(panel.options.winfo_manager())
        panel.selector.current(1)
        panel.selector.event_generate('<<ComboboxSelected>>')
        root.update()
        test.assertTrue(panel.options.winfo_manager())
        probe.assert_not_called()
        install.assert_not_called()
        test.assertEqual(harness_settings.load_preferences(app.sessions)['engine'], 'builtin')

        with patch('harness_panel.filedialog.askopenfilename', return_value=str(fixture)):
            panel.browse.invoke()
        panel.save.invoke()
        test.assertEqual(harness_settings.load_preferences(app.sessions),
                         {'engine': 'harness', 'executable': str(fixture)})
        with patch('harness_panel.webbrowser.open', return_value=True) as opened:
            panel.docs.invoke()
            opened.assert_called_once_with(OFFICIAL_GUIDE)

        def settle():
            until = time.monotonic() + 6
            while app.busy and time.monotonic() < until:
                root.update()
                time.sleep(.01)
            test.assertFalse(app.busy)
            test.assertFalse(callback_errors, callback_errors)

        entered = threading.Event()
        release = threading.Event()
        test.addCleanup(release.set)
        def fake_probe(path, cancel_event):
            test.assertIsNot(threading.current_thread(), threading.main_thread())
            test.assertEqual(path, str(fixture))
            test.assertIs(cancel_event, app.generation_cancel)
            entered.set()
            release.wait(4)
            return {'version': '1.2.3', 'resume': True, 'message': '组件可用。'}

        probe.side_effect = fake_probe
        panel.check.invoke()
        test.assertTrue(entered.wait(2))
        test.assertTrue(app.busy)
        test.assertEqual(str(panel.selector.cget('state')), 'disabled')
        test.assertEqual(panel.save.cget('state'), 'disabled')
        panel.install.invoke()
        install.assert_not_called()
        release.set()
        settle()
        test.assertIn('1.2.3', panel.notice.get())
        test.assertIn('支持任务续跑', panel.notice.get())

        # A failed probe leaves both the field and the previously saved setting intact.
        panel.executable.set(str(scratch / 'missing.exe'))
        probe.side_effect = OSError('组件不可用')
        panel.check.invoke()
        settle()
        test.assertIn('组件不可用', panel.notice.get())
        test.assertEqual(panel.executable.get(), str(scratch / 'missing.exe'))
        test.assertEqual(harness_settings.load_preferences(app.sessions)['executable'], str(fixture))

        install.return_value = scratch / 'installed' / 'dsh.exe'
        panel.install.invoke()
        settle()
        install.assert_called_once_with(cancel_event=app.generation_cancel)
        test.assertEqual(panel.executable.get(), str(install.return_value))
        test.assertIn('已安装', panel.notice.get())
        test.assertEqual(harness_settings.load_preferences(app.sessions)['executable'], str(fixture))

        def cancelled_probe(path, cancel_event):
            cancel_event.wait(3)
            raise OSError('组件检查已取消')

        probe.side_effect = cancelled_probe
        panel.check.invoke()
        app.stop_generation()
        settle()
        test.assertIn('已停止', panel.notice.get())
        test.assertEqual(str(panel.selector.cget('state')), 'readonly')


class HarnessPanelTests(unittest.TestCase):
    def test_native_coding_settings(self):
        with tempfile.TemporaryDirectory(prefix='ui-harness-', dir=ROOT / 'development') as scratch:
            env = dict(os.environ, AI_TRAINING_DATA=scratch)
            command = ('import sys, unittest; sys.path.insert(0, "development/tests"); '
                       'from test_harness_panel import exercise_settings; '
                       'test = unittest.FunctionTestCase(lambda: exercise_settings(test)); '
                       'sys.exit(not unittest.TextTestRunner().run(test).wasSuccessful())')
            result = subprocess.run([sys.executable, '-E', '-s', '-B', '-X', 'utf8', '-c', command],
                                    env=env, cwd=ROOT, capture_output=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
