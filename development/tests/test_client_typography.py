"""Native resizing must preserve work and leave the main actions reachable."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class NativeTypographyScenario(unittest.TestCase):
    def runTest(self):
        import tkinter as tk
        import tkinter.font as tkfont
        from unittest.mock import patch
        from client_app import ClientApp
        from ui_preferences import load_text_size

        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.withdraw()
        with patch('urllib.request.urlopen', side_effect=AssertionError('Startup must remain offline')):
            app = ClientApp(root)
        self.addCleanup(lambda: root.after_cancel(app.poll_id))
        errors = []
        root.report_callback_exception = lambda kind, error, traceback: errors.append(str(error))
        app.input.insert('1.0', '不要丢失这段未发送的需求')
        app.api_panel.model.set('unsaved-model-draft')
        app.history = [{'role': 'user', 'content': '阅读布局测试'}, {'role': 'assistant', 'content': '文字内容。' * 180}]
        app.write(app.transcript, '阅读布局测试\n' + '清晰的正文。' * 180)
        before = app.transcript.get('1.0', 'end')
        normal = tkfont.Font(root=root, font=app.transcript.cget('font')).actual('size')
        app.text_size_choice.set('特大')
        app.change_text_size()
        self.assertEqual(load_text_size(), 'extra_large')
        bigger = tkfont.Font(root=root, font=app.transcript.cget('font')).actual('size')
        self.assertGreater(bigger, normal)
        app.project.set_mode(True)

        for geometry in ('1920x1000+0+0', '940x760+0+0'):
            root.geometry(geometry)
            root.deiconify()
            root.update()
            app.layout_chat()
            root.update()
            for button in (app.send_button, app.model_button, app.more_button,
                           app.project.folder_button, app.project.permissions_button):
                self.assertTrue(button.winfo_viewable(), (geometry, button.cget('text')))
                self.assertGreaterEqual(button.winfo_rootx(), app.main.winfo_rootx())
                self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), root.winfo_rootx() + root.winfo_width())
                self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), root.winfo_rooty() + root.winfo_height())
            self.assertGreater(app.transcript.winfo_height(), 110)
            self.assertLess(app.chat_stack.winfo_width(), 1250)
            # Length and skills are now reachable through the overflow menu,
            # instead of taking space away from the model and send controls.
            with patch('tkinter.Menu.tk_popup'):
                app.answer_menu()
                menus = [widget for widget in root.winfo_children() if isinstance(widget, tk.Menu)]
                self.assertTrue(any(menu.entrycget(index, 'label') == '对话长度…'
                                    for menu in menus for index in range((menu.index('end') or 0) + 1)
                                    if menu.type(index) == 'command'))
        self.assertEqual(app.input.get('1.0', 'end').strip(), '不要丢失这段未发送的需求')
        self.assertEqual(app.transcript.get('1.0', 'end'), before)
        self.assertEqual(app.api_panel.model.get(), 'unsaved-model-draft')
        app.text_size_choice.set('标准')
        app.change_text_size()
        root.update()
        self.assertEqual(tkfont.Font(root=root, font=app.transcript.cget('font')).actual('size'), normal)

        app.show_capabilities('hf-models')
        panel = app.hf_model_panel
        panel.query.set('Qwen small')
        app.show_capabilities('models')
        app.show_capabilities('hf-models')
        root.update()
        self.assertIs(panel, app.hf_model_panel)
        self.assertEqual(panel.query.get(), 'Qwen small')
        self.assertTrue(panel.winfo_viewable())
        self.assertEqual(len(app.pages), 3)
        self.assertFalse(errors, errors)


class ClientTypographyTests(unittest.TestCase):
    def test_sizes_layout_and_draft_preservation(self):
        with tempfile.TemporaryDirectory(prefix='client-type-', dir=ROOT/'development') as scratch:
            code = ('import sys, unittest; sys.path.insert(0, "development/tests"); '
                    'from test_client_typography import NativeTypographyScenario; '
                    'r=unittest.TextTestRunner(verbosity=2).run(NativeTypographyScenario()); sys.exit(not r.wasSuccessful())')
            bundled_python = ROOT/'runtime/python/python.exe'
            executable = str(bundled_python) if bundled_python.is_file() else sys.executable
            result = subprocess.run([executable, '-E', '-s', '-B', '-X', 'utf8', '-c', code],
                cwd=ROOT, env=dict(os.environ, AI_TRAINING_DATA=scratch), capture_output=True,
                text=True, encoding='utf-8', timeout=40, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(ClientTypographyTests)
