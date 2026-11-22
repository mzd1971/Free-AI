"""Exercise current-date replies through the native chat composer."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
BUNDLED_PYTHON = ROOT / 'runtime' / 'python' / 'python.exe'


class NativeClockScenario(unittest.TestCase):
    """Run Tk and the client against an isolated data and model directory."""

    def runTest(self):
        import tkinter as tk

        from app_state import Sessions
        from client_app import ClientApp
        from paths import ARTIFACTS

        scratch = Path(os.environ['AI_TRAINING_DATA'])
        self.assertEqual(ARTIFACTS, scratch)
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(lambda: root.destroy() if root.children else None)
        callback_errors = []
        root.report_callback_exception = lambda kind, error, traceback: callback_errors.append(error)
        app = ClientApp(root)
        app.selected_model = 'text-default'
        self.assertFalse(app.manager.ready(app.selected_model))
        self.assertFalse(app.api_settings.public()['key_available'])
        app.memories.remote_enabled.set(True)

        store = app.personal_memory
        stale = store.remember('今日日期', '今天是2023年11月20日。', evidence='旧对话')
        self.assertEqual(stale['source'], 'agent')
        original_memory = store.path.read_bytes()
        fixed = datetime(2026, 11, 22, 16, 30, tzinfo=timezone(timedelta(hours=8)))

        def wait_for_reply():
            deadline = time.monotonic() + 15
            while app.busy and time.monotonic() < deadline:
                root.update()
                if app.busy:
                    time.sleep(.01)
            self.assertFalse(callback_errors, callback_errors)
            self.assertFalse(app.busy, 'date reply worker did not finish')
            self.assertIsNone(app.pending_chat, app.status.get())

        for backend in ('local', 'api'):
            with self.subTest(backend=backend):
                app.new_conversation()
                app.backend = backend
                self.assertEqual(app.memories.snapshot(backend), {'read': True, 'write': True})
                expected_history = []
                with ExitStack() as checks:
                    clock = checks.enter_context(patch('clock_context.current_time', return_value=fixed))
                    backends_and_memory = (
                        patch.object(app.manager, 'ready', side_effect=AssertionError('model readiness checked')),
                        patch.object(app, 'make_api_client', side_effect=AssertionError('API client requested')),
                        patch('qwen_chat.runtime', side_effect=AssertionError('local runtime requested')),
                        patch.object(store, '_connect', side_effect=AssertionError('long-term memory accessed')),
                    )
                    blocked_calls = [checks.enter_context(guard) for guard in backends_and_memory]
                    for message in ('今天几号', '搜索今天的日期'):
                        with self.subTest(message=message):
                            app.input.insert('1.0', message)
                            app.send_button.invoke()
                            wait_for_reply()
                            answer = app.last_answer
                            self.assertIn('2026年11月22日', answer)
                            self.assertIn('本机系统时钟（UTC+08:00）', answer)
                            self.assertNotIn('2023', answer)
                            self.assertIn(answer, app.transcript.get('1.0', 'end-1c'))
                            self.assertIn('已保存', app.chat_hint.get())
                            self.assertEqual(app.input.get('1.0', 'end-1c'), '')
                            expected_history.extend([
                                {'role': 'user', 'content': message},
                                {'role': 'assistant', 'content': answer},
                            ])
                            self.assertEqual(Sessions(scratch).messages(app.session_id), expected_history)
                    self.assertEqual(clock.call_count, 2)
                    for blocked in blocked_calls:
                        blocked.assert_not_called()
                self.assertEqual(store.path.read_bytes(), original_memory)
        app.close()


class ClockUITest(unittest.TestCase):
    def test_native_date_replies_without_model_or_api_credentials(self):
        with tempfile.TemporaryDirectory(prefix='ui-clock-', dir=ROOT / 'development') as scratch:
            env = dict(os.environ, AI_TRAINING_DATA=scratch)
            executable = str(BUNDLED_PYTHON) if BUNDLED_PYTHON.is_file() else sys.executable
            code = (
                'import sys, unittest; from pathlib import Path; '
                'sys.path.insert(0, str(Path("development/tests").resolve())); '
                'from test_clock_ui import NativeClockScenario; '
                'result = unittest.TextTestRunner(verbosity=2).run(NativeClockScenario()); '
                'sys.exit(not result.wasSuccessful())'
            )
            result = subprocess.run([executable, '-E', '-s', '-B', '-X', 'utf8', '-c', code],
                cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(ClockUITest)


if __name__ == '__main__':
    unittest.main()
