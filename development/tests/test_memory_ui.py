"""Exercise the native chat memory panel with isolated storage and model I/O."""

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


class NativeMemoryScenario(unittest.TestCase):
    """Run only in a child process with the app data directory redirected."""

    def runTest(self):
        import tkinter as tk

        from client_app import ClientApp
        from paths import ARTIFACTS

        scratch = Path(os.environ['AI_TRAINING_DATA'])
        self.assertEqual(ARTIFACTS, scratch)
        first, second = scratch / 'work-one', scratch / 'work-two'
        first.mkdir()
        second.mkdir()
        callback_errors = []

        def new_app():
            root = tk.Tk()
            root.withdraw()
            self.addCleanup(lambda: root.destroy() if root.children else None)
            root.report_callback_exception = lambda kind, error, traceback: callback_errors.append(error)
            return ClientApp(root)

        def put(widget, value):
            widget.delete('1.0', 'end')
            widget.insert('1.0', value)

        def save(panel, title, content, scope='这个助手'):
            panel.new()
            panel.title.set(title)
            panel.scope.set(scope)
            put(panel.content, content)
            panel.save_button.invoke()
            self.assertIsNotNone(panel.identity, panel.notice.get())
            return panel.identity

        def select(panel, identity):
            index = next(index for index, row in enumerate(panel.rows) if row['id'] == identity)
            panel.listbox.selection_clear(0, 'end')
            panel.listbox.selection_set(index)
            panel.select()
            self.assertEqual(panel.identity, identity)

        def settle(app):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                app.root.update()
                self.assertFalse(callback_errors, callback_errors)
                if not app.busy:
                    return
                time.sleep(.01)
            self.fail('chat worker did not finish')

        class FakeRuntime:
            def __init__(self):
                self.prompts = []

            def generate(self, prompt, history, **kwargs):
                self.prompts.append(prompt)
                return '测试聊天回答'

        with patch.object(tk, 'Toplevel', side_effect=AssertionError('memory opened another window')):
            app = new_app()
            panel, store = app.memories, app.personal_memory
            self.assertIn('本轮没有新增记忆', panel.chat_notice({'memory_attempted': True}, '请记住我的偏好'))
            self.assertIn('本轮未启用自动记忆', panel.chat_notice({}, '请记住我的偏好'))
            self.assertEqual(panel.chat_notice({'memory_attempted': True}, '解释一下递归'), '')
            self.assertIn('本次未能读取长期记忆', panel.chat_notice({'memory_warning': '本次未能读取长期记忆。'}, '继续'))
            self.assertEqual(len(app.pages), 3)
            self.assertEqual(panel.snapshot('api'), {'read': False, 'write': False})
            self.assertEqual(panel.snapshot('local'), {'read': True, 'write': True})
            app.open_memory()
            self.assertTrue(panel.opened)
            self.assertIs(panel.frame.master, app.pages[0])
            self.assertEqual(panel.frame.winfo_manager(), 'place')

            preference = '回答时先给中文结论，再给简短说明。'
            manual = save(panel, '回答格式', preference)
            self.assertEqual(store.get(manual)['source'], 'user')
            collision = store.remember('回答格式', '改用英文', kind='preference')
            self.assertEqual(collision['id'], manual)
            self.assertEqual(store.get(manual)['content'], preference)

            proposed = store.remember('输出单位', 'AI 建议用英制', kind='preference')
            panel.refresh()
            select(panel, proposed['id'])
            self.assertIn('尚未核实', panel.provenance.cget('text'))
            put(panel.content, '输出单位始终使用国际单位制。')
            panel.save_button.invoke()
            self.assertEqual(store.get(proposed['id'])['source'], 'user')
            self.assertEqual(store.get(proposed['id'])['content'], '输出单位始终使用国际单位制。')
            panel.status_button.invoke()
            self.assertEqual(store.get(proposed['id'])['status'], 'disabled')
            self.assertNotIn(proposed['id'], store.before_call('输出单位')['memory_ids'])
            panel.status_button.invoke()
            self.assertEqual(store.get(proposed['id'])['status'], 'active')
            self.assertIn(proposed['id'], store.before_call('输出单位')['memory_ids'])
            panel.delete_button.invoke()
            self.assertEqual(store.get(proposed['id'])['status'], 'active')
            self.assertEqual(panel.delete_pending, proposed['id'])
            panel.delete_button.invoke()
            with self.assertRaises(ValueError):
                store.get(proposed['id'])
            self.assertNotIn(proposed['id'], store.before_call('输出单位')['memory_ids'])

            panel.options[1].invoke()  # Isolate memory retrieval from automatic extraction.
            app.selected_model, app.backend = 'text-default', 'local'
            runtime = FakeRuntime()
            put(app.input, '请说明回答格式。')
            with patch.object(app.manager, 'ready', return_value=True), patch('qwen_chat.runtime', return_value=runtime):
                app.send_chat()
                settle(app)
            self.assertEqual(len(runtime.prompts), 1)
            self.assertIn('长期记忆资料开始', runtime.prompts[0])
            self.assertIn(preference, runtime.prompts[0])
            self.assertEqual(app.last_answer, '测试聊天回答')

            identity, history = app.session_id, list(app.history)
            put(app.input, '这段草稿仍未发送')
            for expected in (False, True):
                app.open_memory()
                self.assertIs(panel.opened, expected)
                self.assertEqual(app.session_id, identity)
                self.assertEqual(app.history, history)
                self.assertEqual(app.input.get('1.0', 'end-1c'), '这段草稿仍未发送')
            app.new_conversation()
            self.assertEqual(app.history, [])
            self.assertEqual(store.get(manual)['content'], preference)
            self.assertIn(manual, [row['id'] for row in panel.rows])

            app.project.set_mode(True)
            with patch('project_chat.filedialog.askdirectory', return_value=str(first)):
                app.project.choose_workspace()
            project = save(panel, '项目约定', '本项目以后统一用四空格缩进。', '当前项目')
            self.assertEqual(store.get(project)['scope'], 'project')
            for folder in (second, first):
                with patch('project_chat.filedialog.askdirectory', return_value=str(folder)):
                    app.project.choose_workspace()
                visible = [row['id'] for row in panel.rows]
                self.assertIn(manual, visible)
                self.assertEqual(project in visible, folder == first)

            panel.options[0].invoke()
            panel.options[2].invoke()
            self.assertEqual(panel.snapshot('api'), {'read': False, 'write': False})
            app.close()
            restored = new_app().memories
            self.assertFalse(restored.read_enabled.get())
            self.assertFalse(restored.auto_enabled.get())
            self.assertTrue(restored.remote_enabled.get())
            restored.options[0].invoke()
            restored.options[1].invoke()
            self.assertEqual(restored.snapshot('api'), {'read': True, 'write': True})
            self.assertFalse(callback_errors, callback_errors)


class MemoryUITest(unittest.TestCase):
    def test_native_memory_workflow(self):
        with tempfile.TemporaryDirectory(prefix='ui-memory-', dir=ROOT / 'development') as scratch:
            env = dict(os.environ, AI_TRAINING_DATA=scratch)
            executable = str(BUNDLED_PYTHON) if BUNDLED_PYTHON.is_file() else sys.executable
            code = (
                'import sys, unittest; from pathlib import Path; '
                'sys.path.insert(0, str(Path("development/tests").resolve())); '
                'from test_memory_ui import NativeMemoryScenario; '
                'result = unittest.TextTestRunner(verbosity=2).run(NativeMemoryScenario()); '
                'sys.exit(not result.wasSuccessful())'
            )
            result = subprocess.run([executable, '-E', '-s', '-B', '-X', 'utf8', '-c', code],
                cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(MemoryUITest)


if __name__ == '__main__':
    unittest.main()
