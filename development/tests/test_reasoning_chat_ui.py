"""Exercise explanation cards through native chat, persistence and cancellation."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class NativeReasoningChat(unittest.TestCase):
    def runTest(self):
        if not os.environ.get('AI_TRAINING_DATA'):
            self.skipTest('Native scenario must run through its isolated subprocess wrapper')
        import tkinter as tk
        from client_app import ClientApp
        from remote_api import APICancelled
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        errors = []
        root.report_callback_exception = lambda kind, error, tb: errors.append(error)
        app = ClientApp(root)
        self.addCleanup(lambda: root.after_cancel(app.poll_id))
        app.backend, app.selected_model = 'local', 'text-default'
        app.memories.auto_enabled.set(False)
        app.memories.read_enabled.set(False)

        def settle():
            end = time.monotonic() + 10
            while time.monotonic() < end:
                root.update()
                self.assertFalse(errors, errors)
                if not app.busy:
                    return
                time.sleep(.01)
            self.fail('native reasoning task did not finish')

        def send(text):
            app.input.delete('1.0', 'end')
            app.input.insert('1.0', text)
            app.send_chat()
            settle()

        class Model:
            supports_reasoning_callback = True
            def complete_tools(self, messages, tools, **kwargs):
                kwargs['on_reasoning']('先核对提供的数字。')
                kwargs['on_reasoning']('再检查结果。')
                return {'role': 'assistant', 'content': '正式回答：4',
                        'reasoning_content': '先核对提供的数字。再检查结果。'}

        with patch.object(app.manager, 'ready', return_value=True), patch(
                'local_project_model.LocalProjectModel', return_value=Model()):
            send('回答这个问题')
        identity = app.session_id
        card = app.project.cards[-1]
        self.assertIn('先核对', card.reasoning_panel.text)
        self.assertTrue(card.reasoning_panel.winfo_manager())
        self.assertEqual(app.last_answer, '正式回答：4')
        stored = app.sessions.project_runs(identity)[-1]['result']['reasoning']
        self.assertEqual(stored, card.reasoning_panel.text)
        card.reasoning_panel.toggle()
        self.assertFalse(card.reasoning_panel.expanded)
        app.load_conversation(identity)
        self.assertEqual(app.project.cards[-1].reasoning_panel.text, stored)
        self.assertNotIn('先核对', str(app.history))

        app.new_conversation()
        app.project.assistant_enabled.set(False)
        def plain_chat(*args, **kwargs):
            kwargs['on_reasoning']('普通回答的独立说明。')
            kwargs['on_token']('普通回答。')
            return {'answer': '普通回答。'}
        with patch.object(app.manager, 'ready', return_value=True), patch('qwen_chat.chat', side_effect=plain_chat):
            send('解释这个概念')
        identity = app.session_id
        self.assertEqual(app.sessions.reasoning(identity), {1: '普通回答的独立说明。'})
        self.assertEqual(app.sessions.messages(identity)[1]['content'], '普通回答。')
        app.load_conversation(identity)
        self.assertEqual(app._reasoning_views[0][1].text, '普通回答的独立说明。')

        def interrupted(*args, **kwargs):
            kwargs['on_reasoning']('停止前已返回的说明。')
            kwargs['cancel_event'].set()
            raise APICancelled('cancelled')
        with patch.object(app.manager, 'ready', return_value=True), patch('qwen_chat.chat', side_effect=interrupted), patch('logging.exception'):
            send('继续分析')
        self.assertIn('已停止', app.last_answer)
        self.assertEqual(app.sessions.reasoning(identity)[3], '停止前已返回的说明。')
        self.assertFalse(errors)


class ReasoningChatUITest(unittest.TestCase):
    def test_native_reasoning_display(self):
        with tempfile.TemporaryDirectory(prefix='reasoning-ui-', dir=ROOT / 'development') as scratch:
            code = ('import sys, unittest; sys.path.insert(0, "development/tests"); '
                    'from test_reasoning_chat_ui import NativeReasoningChat; '
                    'result = unittest.TextTestRunner().run(NativeReasoningChat()); sys.exit(not result.wasSuccessful())')
            result = subprocess.run([str(ROOT / 'runtime/python/python.exe'), '-E', '-s', '-B', '-X', 'utf8', '-c', code],
                                    cwd=ROOT, env=dict(os.environ, AI_TRAINING_DATA=scratch),
                                    capture_output=True, text=True, encoding='utf-8', timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(ReasoningChatUITest)
