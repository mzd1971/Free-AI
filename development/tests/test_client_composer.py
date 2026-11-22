"""Exercise draft ownership and composer behavior with native Tk."""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


class NativeComposerScenario(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='case-', dir=os.environ['AI_TRAINING_DATA'])
        self.addCleanup(scratch.cleanup)
        self.data = Path(scratch.name)
        self.errors = []
        self.app = self.open_app()

    def open_app(self):
        import tkinter as tk
        import client_app

        root = tk.Tk()
        root.withdraw()
        with patch.object(client_app, 'ARTIFACTS', self.data), patch(
                'urllib.request.urlopen', side_effect=AssertionError('Composer tests must stay offline.')):
            app = client_app.ClientApp(root)
        root.report_callback_exception = lambda _kind, error, _trace: self.errors.append(error)

        def cleanup():
            try:
                if root.winfo_exists():
                    app.busy = False
                    app.close()
            except tk.TclError:
                pass
            self.assertEqual(self.errors, [])

        self.addCleanup(cleanup)
        return app

    def wait_for_tk(self, milliseconds=300):
        import tkinter as tk

        ready = tk.BooleanVar(self.app.root, value=False)
        self.app.root.after(milliseconds, lambda: ready.set(True))
        self.app.root.wait_variable(ready)

    def text(self):
        return self.app.input.get('1.0', 'end-1c')

    def test_switching_sessions_restores_unsent_text(self):
        first, second = self.app.sessions.create(), self.app.sessions.create()
        self.app.choose_session(first)
        self.app.input.insert('1.0', '未发送草稿  A\n')
        self.app.choose_session(second)
        self.app.input.insert('1.0', '未发送草稿  B')
        self.app.choose_session(first)
        self.assertEqual(self.text(), '未发送草稿  A\n')
        self.app.choose_session(second)
        self.assertEqual(self.text(), '未发送草稿  B')

    def test_new_conversation_and_session_attachments_restore_on_restart(self):
        image = self.data / 'fixture.png'
        csv = self.data / 'expense.csv'
        image.write_bytes(b'fixture only')
        csv.write_text('amount\n17\n', encoding='utf-8')
        session = self.app.sessions.create()
        self.app.choose_session(session)
        self.app.input.insert('1.0', '查看图片')
        with patch.object(self.app.manager, 'ready', return_value=True), patch(
                'desktop_app.filedialog.askopenfilename', return_value=str(image)):
            self.app.attach_image()
        self.app.new_conversation()
        self.app.input.insert('1.0', '新会话草稿')
        with patch('client_app.filedialog.askopenfilename', return_value=str(csv)):
            self.app.choose_skill_file()
        self.app.close()

        self.app = self.open_app()
        self.assertIsNone(self.app.session_id)
        self.assertEqual(self.text(), '新会话草稿')
        self.assertEqual(self.app.skill_file, str(csv))
        self.assertEqual(self.app.skill.get(), 'csv_summary')
        self.assertIn('expense.csv', self.app.attachment_label.cget('text'))
        self.app.choose_session(session)
        self.assertEqual(self.text(), '查看图片')
        self.assertEqual(self.app.image_path, str(image))
        self.assertIsNone(self.app.skill_file)
        self.assertIn('fixture.png', self.app.attachment_label.cget('text'))
        self.app.clear_attachments()
        self.app.new_conversation()
        self.app.choose_session(session)
        self.assertIsNone(self.app.image_path)
        self.assertEqual(self.text(), '查看图片')

    def test_autosave_and_failed_switch_keep_the_editor_and_session_intact(self):
        first, second = self.app.sessions.create(), self.app.sessions.create()
        self.app.choose_session(first)
        self.app.input.insert('1.0', '已自动保存')
        self.wait_for_tk()
        self.assertEqual(self.app.sessions.draft(first)[0], '已自动保存')

        self.app.input.insert('end', '仍在编辑')
        with patch.object(self.app.sessions, 'save_draft', side_effect=sqlite3.OperationalError('fixture')):
            self.app.choose_session(second)
        self.assertEqual(self.app.session_id, first)
        self.assertEqual(self.text(), '已自动保存仍在编辑')
        self.assertIn('草稿未能保存', self.app.status.get())

    def test_send_commits_only_current_draft_and_keeps_subsequent_edit(self):
        self.app.select_skill('calculator')
        with patch.object(self.app, 'submit') as submit:
            self.app.input.insert('1.0', '1 + 1')
            self.assertTrue(self.app.flush_draft())
            self.app.send_chat()
        submit.assert_called_once()
        session = self.app.session_id
        self.assertTrue(session)
        self.assertEqual(self.app.sessions.messages(session), [{'role': 'user', 'content': '1 + 1'}])
        self.assertEqual(self.app.sessions.draft(None)[0], '')
        self.assertEqual(self.app.draft_session_id, session)
        self.assertEqual(self.text(), '')

        self.app.input.insert('1.0', '下一条仍在编辑')
        self.wait_for_tk()
        self.assertEqual(self.app.sessions.draft(session)[0], '下一条仍在编辑')

    def test_failed_send_and_missing_attachment_keep_draft(self):
        self.app.select_skill('calculator')
        self.app.input.insert('1.0', '1 + 1')
        self.assertTrue(self.app.flush_draft())
        with patch.object(self.app.sessions, 'append', side_effect=sqlite3.OperationalError('fixture')):
            self.app.send_chat()
        self.assertEqual(self.text(), '1 + 1')
        self.assertEqual(self.app.sessions.draft(None)[0], '1 + 1')
        self.assertIsNone(self.app.session_id)

        self.app.select_skill('csv_summary')
        self.app.skill_file = str(self.data / 'no-longer-present.csv')
        self.app.send_chat()
        self.assertIn('找不到', self.app.chat_hint.get())
        self.assertEqual(self.text(), '1 + 1')

    def test_composer_wraps_display_lines_and_drawer_stays_above_send(self):
        root = self.app.root
        root.geometry('1280x900+0+0')
        root.deiconify()
        root.update()
        self.assertEqual(int(self.app.input.cget('height')), 2)

        self.app.input.insert('1.0', '这是一行没有手工换行的长文本。' * 100)
        root.update()
        self.assertGreater(int(self.app.input.cget('height')), 2)
        self.assertLessEqual(int(self.app.input.cget('height')), 8)
        self.assertTrue(self.app.input_scrollbar.winfo_viewable())
        self.assertLess(self.app.input.yview()[1] - self.app.input.yview()[0], 1)

        self.app.text_size_choice.set('特大')
        self.app.change_text_size()
        root.geometry('940x640+0+0')
        self.app.open_memory()
        root.update()
        self.assertGreaterEqual(int(self.app.input.cget('height')), 2)
        self.assertLess(int(self.app.input.cget('height')), 8)
        self.assertTrue(self.app.send_button.winfo_viewable())
        self.assertLessEqual(self.app.send_button.winfo_rootx() + self.app.send_button.winfo_width(),
                             root.winfo_rootx() + root.winfo_width())
        self.assertLessEqual(self.app.send_button.winfo_rooty() + self.app.send_button.winfo_height(),
                             root.winfo_rooty() + root.winfo_height())
        self.assertTrue(self.app.memories.frame.winfo_viewable())
        self.assertLessEqual(self.app.memories.frame.winfo_rooty() + self.app.memories.frame.winfo_height(),
                             self.app.context_bar.winfo_rooty())
        self.app.input.delete('1.0', 'end')
        root.update()
        self.assertEqual(int(self.app.input.cget('height')), 2)
        self.assertFalse(self.app.input_scrollbar.winfo_viewable())

    def test_home_and_conversation_use_distinct_layouts(self):
        app, root = self.app, self.app.root
        app.text_size_choice.set('标准')
        app.change_text_size()
        root.geometry('1600x960+0+0')
        root.deiconify()
        root.update()
        page = app.pages[0]
        self.assertTrue(app.hero.winfo_viewable())
        self.assertFalse(app.transcript_area.winfo_viewable())
        self.assertGreater(app.composer.winfo_width(), page.winfo_width() * .6)
        home_center = app.chat_stack.winfo_y() + app.chat_stack.winfo_height() / 2
        self.assertAlmostEqual(home_center / page.winfo_height(), .52, delta=.05)
        self.assertLess(app.context_bar.winfo_rooty(), app.composer.winfo_rooty())
        self.assertTrue(app.project.permissions_button.winfo_viewable())
        self.assertLess(app.more_button.winfo_rootx(), app.model_button.winfo_rootx())
        self.assertLess(app.model_button.winfo_rootx(), app.send_button.winfo_rootx())

        # A long validation hint must grow the empty state without hiding setup.
        root.geometry('940x740+0+0')
        app.text_size_choice.set('特大')
        app.change_text_size()
        root.update()
        before = app.chat_stack.winfo_height()
        app.chat_hint.set('所选模型暂时无法处理这张图片，请在模型与扩展选择支持图片的模型，或者先移除附件后再次发送。' * 3)
        root.update()
        self.assertGreater(app.chat_stack.winfo_height(), before)
        self.assertGreaterEqual(app.hint_label.winfo_height(), app.hint_label.winfo_reqheight())
        self.assertLessEqual(app.sidebar.winfo_width(), 280)
        self.assertEqual(app.send_button.winfo_width(), app.send_button.winfo_height())

        app.chat_hint.set('Ctrl + Enter 发送 · Enter 换行')
        session = app.sessions.create()
        app.sessions.append(session, 'user', '看看这个项目')
        app.sessions.append(session, 'assistant', '**中文排版**保持正文内容。')
        app.choose_session(session)
        app.input.insert('1.0', '未发送的草稿')
        root.update()
        self.assertFalse(app.hero.winfo_viewable())
        self.assertTrue(app.transcript_area.winfo_viewable())
        self.assertAlmostEqual(app.chat_stack.winfo_y() + app.chat_stack.winfo_height(),
                               page.winfo_height() - 12, delta=3)
        app.project.set_mode(True)
        root.update()
        self.assertFalse(app.project_summary.winfo_viewable())
        app.toggle_project_details()
        root.update()
        self.assertTrue(app.project_summary.winfo_viewable())
        self.assertLessEqual(app.project_summary.winfo_rooty() + app.project_summary.winfo_height(),
                             app.context_bar.winfo_rooty())
        self.assertTrue(app.project.permissions_button.winfo_viewable())
        app.toggle_project_details()
        root.update()
        self.assertFalse(app.project_summary.winfo_viewable())
        app.toggle_sidebar()
        root.update()
        self.assertFalse(app.sidebar.winfo_viewable())
        self.assertTrue(app.sidebar_button.winfo_viewable())
        app.toggle_sidebar()
        root.update()
        self.assertTrue(app.sidebar.winfo_viewable())
        self.assertEqual(self.text(), '未发送的草稿')

    def test_starter_preserves_existing_draft_and_never_sends(self):
        app, root = self.app, self.app.root
        root.deiconify()
        root.update()
        app.input.insert('1.0', '已经写下的要求')
        with patch.object(app, 'send_chat') as send:
            app.starter_buttons[0].invoke()
            root.update()
            self.assertTrue(self.text().startswith('已经写下的要求\n\n'))
            self.assertIn('清晰的计划', self.text())
            self.assertIs(root.focus_get(), app.input)
            self.assertFalse(app.placeholder.winfo_viewable())
            from ui_kit import COLORS
            self.assertEqual(app.composer.border, COLORS['focus'])
            send.assert_not_called()
        self.assertTrue(app.flush_draft())
        self.assertEqual(app.sessions.draft(None)[0], self.text())
        app.input.delete('1.0', 'end')
        app.input.insert('1.0', '  \n')
        app.use_starter('新的要求')
        self.assertTrue(self.text().startswith('  \n'), 'Whitespace in a draft also belongs to the user')

    def test_busy_input_is_editable_and_shortcut_does_not_send_or_stop(self):
        root = self.app.root
        root.deiconify()
        root.update()
        self.app.input.focus_force()
        root.update()
        self.app.input.insert('1.0', '先写下一条')
        self.app.busy = True
        self.app._sync_composer_activity()
        root.update()
        with patch.object(self.app, 'send_chat') as send:
            self.app.input.event_generate('<Return>')
            self.app.input.event_generate('<Control-Return>')
            self.assertIsNone(self.app.enter_send(SimpleNamespace(keycode=229)))
            root.update()
            send.assert_not_called()
        self.assertEqual(self.text(), '先写下一条\n')
        self.assertTrue(self.app.busy_draft_label.winfo_viewable())
        self.assertFalse(self.app.generation_cancel.is_set())
        self.app.send_or_stop()
        self.assertTrue(self.app.generation_cancel.is_set())
        self.assertEqual(self.text(), '先写下一条\n')

    def test_placeholder_aligns_with_caret_and_click_transfers_focus(self):
        from tkinter import font as tkfont

        app, root = self.app, self.app.root
        root.geometry('1100x760+0+0')
        root.deiconify()
        root.focus_force()
        root.update()
        for choice, geometry in (('标准', '1100x760'), ('大', '940x740'), ('特大', '1100x820')):
            with self.subTest(size=choice):
                app.input.delete('1.0', 'end')
                app.text_size_choice.set(choice)
                app.change_text_size()
                root.geometry(geometry + '+0+0')
                root.update()
                app.more_button.focus_set()
                root.update()
                self.assertIs(root.focus_get(), app.more_button)
                self.assertTrue(app.placeholder.winfo_viewable())
                x, y, _, height, baseline = app.input.dlineinfo('insert')
                font = tkfont.Font(root=root, font=app.placeholder.cget('font'))
                prompt_baseline = (app.placeholder.winfo_y()
                    + app.placeholder.winfo_pixels(app.placeholder.cget('pady')) + font.metrics('ascent'))
                self.assertAlmostEqual(prompt_baseline, y + baseline, delta=1)
                self.assertGreaterEqual(height, font.metrics('linespace'))
                app.placeholder.event_generate('<Button-1>', x=20, y=8)
                app.placeholder.event_generate('<ButtonRelease-1>', x=20, y=8)
                root.update()
                self.assertIs(root.focus_get(), app.input)
                self.assertFalse(app.placeholder.winfo_viewable(), 'The hint must hide as soon as editing starts')

                # Native Text bindings must retain the insertion point after
                # Chinese text and a newline, without putting the hint in data.
                app.input.insert('insert', '你好 Free AI')
                app.input.event_generate('<Return>')
                root.update()
                self.assertEqual(app.input.index('insert'), '2.0')
                self.assertEqual(self.text(), '你好 Free AI\n')
                self.assertFalse(app.placeholder.winfo_viewable())
                app.input.insert('insert', '第二行')
                app.input.event_generate('<Left>')
                app.input.insert('insert', '✓')
                root.update()
                self.assertEqual(self.text(), '你好 Free AI\n第二✓行')
                self.assertIsNotNone(app.input.bbox('insert'))
                app.more_button.focus_set()
                root.update()
                self.assertFalse(app.placeholder.winfo_viewable(), 'A nonempty draft must never show the hint')
                app.input.focus_set()
                app.input.delete('1.0', 'end')
                root.update()
                self.assertFalse(app.placeholder.winfo_viewable(), 'Clearing while editing must not cover the caret')
                app.more_button.focus_set()
                root.update()
                self.assertTrue(app.placeholder.winfo_viewable())
                self.assertEqual(self.text(), '')
                self.assertEqual(app.placeholder.winfo_x(), app.input.dlineinfo('1.0')[0])


class ClientComposerTests(unittest.TestCase):
    def test_native_composer(self):
        with tempfile.TemporaryDirectory(prefix='client-composer-', dir=ROOT / 'development') as scratch:
            code = ('import sys, unittest; sys.path.insert(0, "development/tests"); '
                    'from test_client_composer import NativeComposerScenario; '
                    'suite=unittest.defaultTestLoader.loadTestsFromTestCase(NativeComposerScenario); '
                    'result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(not result.wasSuccessful())')
            bundled = ROOT / 'runtime/python/python.exe'
            executable = str(bundled) if bundled.is_file() else sys.executable
            result = subprocess.run([executable, '-E', '-s', '-B', '-X', 'utf8', '-c', code], cwd=ROOT,
                                    env=dict(os.environ, AI_TRAINING_DATA=scratch), capture_output=True,
                                    text=True, encoding='utf-8', timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(ClientComposerTests)
