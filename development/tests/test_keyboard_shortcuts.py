"""Exercise main-window shortcuts and native input bindings in isolated storage."""
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_client_settings import BUNDLED_PYTHON, ROOT


class NativeShortcutScenario(unittest.TestCase):
    def open_app(self):
        import tkinter as tk
        from client_app import ClientApp

        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.withdraw()
        app = ClientApp(root)
        self.addCleanup(lambda: root.after_cancel(app.poll_id))
        root.report_callback_exception = lambda kind, error, traceback: self.callback_errors.append(error)
        root.geometry('1080x720+2500+1400')
        root.deiconify()
        root.update()
        return app, root

    def runTest(self):
        import tkinter as tk
        from tkinter import ttk

        from keyboard_shortcuts import DEFAULT_SHORTCUTS, SETTINGS_KEY, SHORTCUTS, install_shortcuts
        from ui_kit import FlatButton

        self.callback_errors = []
        app, root = self.open_app()

        def press(widget, sequence):
            widget.focus_force()
            root.update()
            widget.event_generate(sequence)
            root.update()
            self.assertFalse(self.callback_errors, self.callback_errors)

        def draft():
            return app.input.get('1.0', 'end-1c')

        app.input.insert('1.0', '先保留草稿')
        self.assertTrue(app.chat_hint.get().startswith('Enter 发送'))
        with patch.object(app, 'send_chat') as send_chat:
            press(app.input, '<Return>')
            send_chat.assert_called_once_with()
            press(app.input, '<Control-Return>')
            press(app.input, '<Shift-Return>')
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

        # Reconfigure both shortcut types through the settings page.
        press(app.input, '<F1>')
        self.assertEqual(set(panel.shortcut_entries), set(DEFAULT_SHORTCUTS))
        bindings = root.bind('<KeyPress>')
        install_shortcuts(app)
        for mode in (1, 0, 1):
            panel.send_key_control.current(mode)
            panel.send_key_control.event_generate('<<ComboboxSelected>>')
        self.assertTrue(app.chat_hint.get().startswith('Ctrl + Enter 发送'))
        self.assertIn('Shift + Enter 换行', app.chat_hint.get())
        self.assertEqual(root.bind('<KeyPress>'), bindings)

        panel.query.set('侧栏')
        sidebar, notice, _ = panel.shortcut_entries['sidebar']
        for invalid, message in [('Ctrl+N', '已用于'), ('Ctrl+C', '文本编辑'), ('A', '例如')]:
            sidebar.delete(0, 'end')
            sidebar.insert(0, invalid)
            press(sidebar, '<Return>')
            self.assertIn(message, notice.get())
            self.assertEqual(app.shortcut_keys['sidebar'], 'Ctrl+B')
        sidebar.delete(0, 'end')
        sidebar.insert(0, ' f2 ')
        save_button = next(widget for widget in sidebar.master.winfo_children() if isinstance(widget, FlatButton))
        save_button.invoke()
        self.assertEqual(sidebar.get(), 'F2')
        self.assertIn('已保存', notice.get())
        panel.query.set('F2')
        self.assertEqual(sum(bool(card.winfo_manager()) for card, _ in panel.sections['shortcuts']), 1)
        app.select_page(0)
        if not app.sidebar.winfo_manager():
            app.toggle_sidebar()
        press(app.input, '<Control-b>')
        self.assertTrue(app.sidebar.winfo_manager())
        press(app.input, '<Shift-F2>')
        self.assertTrue(app.sidebar.winfo_manager())
        press(app.input, '<F2>')
        self.assertFalse(app.sidebar.winfo_manager())
        press(app.input, '<F2>')
        self.assertTrue(app.sidebar.winfo_manager())

        with patch.object(app, 'send_chat') as send_chat:
            press(app.input, '<Return>')
            press(app.input, '<Shift-Return>')
            self.assertEqual(draft(), '上一段对话的草稿\n\n')
            send_chat.assert_not_called()
            press(app.input, '<Control-Return>')
            send_chat.assert_called_once_with()
            with patch('keyboard_shortcuts.is_ime_event', return_value=True):
                press(app.input, '<Control-Return>')
                press(app.input, '<F2>')
            self.assertTrue(app.sidebar.winfo_manager())
            dialog = tk.Toplevel(root)
            root.update()
            dialog.grab_set()
            press(app.input, '<Control-Return>')
            press(app.input, '<F2>')
            self.assertTrue(app.sidebar.winfo_manager())
            send_chat.assert_called_once_with()
            dialog.destroy()

        app.flush_draft()
        self.doCleanups()
        app, root = self.open_app()
        panel = app.settings_panel
        self.assertEqual(app.send_key, 'Ctrl+Enter')
        self.assertEqual(panel.send_key_control.get(), 'Ctrl+Enter 发送')
        self.assertEqual(app.shortcut_keys['sidebar'], 'F2')
        self.assertEqual(panel.shortcut_entries['sidebar'][0].get(), 'F2')
        press(app.input, '<F2>')
        self.assertFalse(app.sidebar.winfo_manager())
        press(app.input, '<F2>')
        self.assertTrue(app.sidebar.winfo_manager())
        press(app.input, '<F1>')
        reset = next(widget for widget in panel.send_key_control.master.winfo_children()
                     if isinstance(widget, FlatButton))
        reset.invoke()
        self.assertEqual(app.sessions.get(SETTINGS_KEY), {'send_key': 'Enter', 'keys': DEFAULT_SHORTCUTS})
        self.assertEqual(panel.send_key_control.get(), 'Enter 发送')
        for shortcut in SHORTCUTS:
            self.assertEqual(panel.shortcut_entries[shortcut.name][0].get(), shortcut.keys)
        app.select_page(0)
        with patch.object(app, 'send_chat') as send_chat:
            press(app.input, '<Return>')
            send_chat.assert_called_once_with()

        app.input.delete('1.0', 'end')
        app.input.edit_reset()
        press(app.input, '<KeyPress-a>')
        self.assertEqual(draft(), 'a')
        press(app.input, '<Control-a>')
        self.assertEqual(app.input.get('sel.first', 'sel.last'), 'a\n')
        press(app.input, '<Control-z>')
        self.assertEqual(draft(), '')


class KeyboardShortcutTests(unittest.TestCase):
    def test_ime_process_key_and_windows_composition(self):
        from keyboard_shortcuts import is_ime_event

        with patch('keyboard_shortcuts._ime_api') as load_api:
            self.assertTrue(is_ime_event(SimpleNamespace(keycode=229)))
            load_api.assert_not_called()

        event = SimpleNamespace(keycode=13, widget=Mock())
        event.widget.winfo_id.return_value = 100
        imm = Mock()
        imm.ImmGetContext.return_value = 101
        imm.ImmGetCompositionStringW.side_effect = [2, 0, -1]
        with patch('keyboard_shortcuts.sys.platform', 'win32'), patch('keyboard_shortcuts._ime_api', return_value=imm):
            self.assertTrue(is_ime_event(event))
            self.assertFalse(is_ime_event(event))
            self.assertFalse(is_ime_event(event))
            imm.ImmGetCompositionStringW.assert_called_with(101, 8, None, 0)
            imm.ImmReleaseContext.assert_called_with(100, 101)
            self.assertEqual(imm.ImmReleaseContext.call_count, 3)
            imm.ImmGetContext.return_value = None
            self.assertFalse(is_ime_event(event))
            self.assertEqual(imm.ImmReleaseContext.call_count, 3)

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
