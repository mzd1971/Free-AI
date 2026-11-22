"""Exercise main-window shortcuts and native input bindings in isolated storage."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_client_settings import BUNDLED_PYTHON, ROOT


class NativeShortcutScenario(unittest.TestCase):
    def runTest(self):
        import tkinter as tk
        from tkinter import ttk

        from client_app import ClientApp

        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.withdraw()
        app = ClientApp(root)
        self.addCleanup(lambda: root.after_cancel(app.poll_id))
        callback_errors = []
        root.report_callback_exception = lambda kind, error, traceback: callback_errors.append(error)
        root.geometry('1080x720+2500+1400')
        root.deiconify()
        root.update()

        def press(widget, sequence):
            widget.focus_force()
            root.update()
            widget.event_generate(sequence)
            root.update()
            self.assertFalse(callback_errors, callback_errors)

        def draft():
            return app.input.get('1.0', 'end-1c')

        app.input.insert('1.0', '先保留草稿')
        press(app.input, '<Return>')
        self.assertEqual(draft(), '先保留草稿\n')
        with patch.object(app, 'send_chat') as send_chat:
            press(app.input, '<Control-Return>')
            send_chat.assert_called_once_with()
        self.assertEqual(draft(), '先保留草稿\n')

        press(app.input, '<Control-b>')
        self.assertFalse(app.sidebar.winfo_manager())
        press(app.input, '<Control-b>')
        self.assertTrue(app.sidebar.winfo_manager())
        press(app.input, '<Control-comma>')
        self.assertEqual(app.current_page, 2)
        panel = app.settings_panel
        panel.query.set('输入额外文字')
        press(root, '<F1>')
        self.assertEqual(panel.category, 'shortcuts')
        self.assertEqual(panel.query.get(), '')
        self.assertEqual(panel.heading.cget('text'), '快捷键')
        panel.query.set('Ctrl+Shift+O')
        self.assertEqual(sum(bool(card.winfo_manager()) for card, _ in panel.sections['shortcuts']), 1)

        app.busy = app.cancellable = True
        search = next(widget for widget in panel.navigation.winfo_children() if isinstance(widget, ttk.Entry))
        press(search, '<Escape>')
        self.assertEqual(panel.query.get(), '')
        self.assertFalse(app.generation_cancel.is_set())
        press(search, '<Control-l>')
        self.assertEqual(app.current_page, 0)
        self.assertIs(root.focus_get(), app.input)

        with patch('project_chat.filedialog.askdirectory', return_value='') as picker:
            press(app.input, '<Control-Shift-O>')
            picker.assert_not_called()
            self.assertEqual(draft(), '先保留草稿\n')
            press(app.input, '<Control-n>')
            self.assertIn('先停止', app.status.get())

            dialog = tk.Toplevel(root)
            entry = ttk.Entry(dialog)
            entry.pack()
            root.update()
            press(entry, '<Escape>')
            press(entry, '<Control-comma>')
            self.assertFalse(app.generation_cancel.is_set())
            self.assertEqual(app.current_page, 0)
            dialog.destroy()

            app.cancellable = False
            press(app.input, '<Escape>')
            self.assertFalse(app.generation_cancel.is_set())
            app.cancellable = True
            press(app.input, '<Escape>')
            self.assertTrue(app.generation_cancel.is_set())
            app.busy = app.cancellable = False
            press(app.input, '<Control-Shift-O>')
            picker.assert_called_once()
            self.assertEqual(draft(), '先保留草稿\n')

        with patch.object(app, 'copy_answer') as copy_answer:
            press(app.input, '<Control-c>')
            copy_answer.assert_not_called()
            press(app.input, '<Control-Shift-C>')
            copy_answer.assert_called_once_with()
        press(app.input, '<Control-Shift-M>')
        self.assertTrue(app.memories.opened)
        press(app.input, '<Control-Shift-M>')
        self.assertFalse(app.memories.opened)

        identity = app.sessions.create()
        app.choose_session(identity)
        app.input.insert('1.0', '上一段对话的草稿')
        press(app.input, '<Control-n>')
        self.assertIsNone(app.session_id)
        self.assertEqual(draft(), '先保留草稿\n')
        app.choose_session(identity)
        self.assertEqual(draft(), '上一段对话的草稿')


class KeyboardShortcutTests(unittest.TestCase):
    def test_native_shortcuts(self):
        with tempfile.TemporaryDirectory(prefix='ui-shortcuts-', dir=ROOT / 'development') as scratch:
            executable = str(BUNDLED_PYTHON) if BUNDLED_PYTHON.is_file() else sys.executable
            code = ('import sys, unittest; from pathlib import Path; '
                    'sys.path.insert(0, str(Path("development/tests").resolve())); '
                    'from test_keyboard_shortcuts import NativeShortcutScenario; '
                    'result = unittest.TextTestRunner(verbosity=2).run(NativeShortcutScenario()); '
                    'sys.exit(not result.wasSuccessful())')
            result = subprocess.run([executable, '-E', '-s', '-B', '-X', 'utf8', '-c', code], cwd=ROOT,
                                    env=dict(os.environ, AI_TRAINING_DATA=scratch), capture_output=True,
                                    text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(KeyboardShortcutTests)


if __name__ == '__main__':
    unittest.main()
