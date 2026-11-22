"""Exercise the native chat memory panel with isolated storage and model I/O."""

import json
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
            def __init__(self, candidates=None):
                self.prompts = []
                self.candidates = candidates

            def generate(self, prompt, history, **kwargs):
                self.prompts.append(prompt)
                if self.candidates is not None and 'evidence_quote' in prompt:
                    return json.dumps(self.candidates, ensure_ascii=False)
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
            self.assertEqual(app.memory_button.cget('text'), '长期记忆')
            self.assertIn('读取开启 · 自动保存开启', panel.mode_state.get())
            app.backend = 'api'
            panel.refresh_mode()
            self.assertIn('未授权', panel.mode_state.get())
            self.assertIn('尚未获准', panel.chat_notice({}, '请记住我的偏好', backend='api'))
            self.assertIn('已停止自动整理', panel.chat_notice({'memory_status': 'cancelled'}, '请记住'))
            self.assertIn('自动整理失败', panel.chat_notice({'memory_status': 'failed'}, '请记住'))
            self.assertEqual(panel.read_notice(['id1', 'id1', 'id2', None]).count('2 条'), 1)
            app.project.enabled.set(True)
            with patch('harness_settings.load_preferences', return_value={'engine': 'harness'}) as execution:
                panel.refresh_mode()
                self.assertIn('未授权长期记忆', panel.mode_state.get())
                execution.assert_not_called()
                panel.remote_enabled.set(True)
                panel.refresh_mode()
                self.assertEqual(panel.mode_state.get(), 'Harness（联网模式）· 读取开启 · 暂不支持自动保存')
                panel.read_enabled.set(False)
                panel.refresh_mode()
                self.assertEqual(panel.mode_state.get(), 'Harness（联网模式）· 读取关闭 · 暂不支持自动保存')
                app.backend = 'local'
                panel.read_enabled.set(True)
                panel.refresh_mode()
                self.assertEqual(panel.mode_state.get(), 'Harness（本地模式）· 读取开启 · 暂不支持自动保存')
            app.project.enabled.set(False)
            panel.remote_enabled.set(False)
            app.backend = 'local'
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

            quote = '我的构建工具固定使用 uv。'
            saving_runtime = FakeRuntime([{'title': '构建工具', 'kind': 'fact', 'evidence_quote': quote}])
            app.selected_model = 'text-default'
            # This suite covers the legacy automatic extraction path. Unified
            # tool-memory behavior has its own native UI integration suite.
            app.project.assistant_enabled.set(False)
            put(app.input, '请记住：' + quote)
            with patch.object(app.manager, 'ready', return_value=True), patch('qwen_chat.runtime', return_value=saving_runtime):
                app.send_chat()
                settle(app)
            self.assertEqual(len(saving_runtime.prompts), 2)
            automatic = next(row for row in panel.rows if row['title'] == '构建工具')
            self.assertEqual((automatic['source'], automatic['status']), ('agent', 'active'))
            self.assertFalse(automatic['verified'])
            self.assertIn('[长期记忆] AI 已记下：构建工具', app.last_answer)
            self.assertIn('[长期记忆] AI 已记下：构建工具', app.sessions.messages(app.session_id)[-1]['content'])
            select(panel, automatic['id'])
            self.assertIn('尚未核实', panel.provenance.cget('text'))
            self.assertEqual(panel.verify_button.cget('text'), '核实这条')
            evidence = store.get(automatic['id'])['evidence']
            panel.source_button.invoke()
            self.assertEqual(panel.source_button.cget('text'), '收起来源')
            self.assertEqual(panel.source_text.cget('state'), 'disabled')
            self.assertEqual(panel.source_text.get('1.0', 'end-1c'), evidence)
            self.assertIn('用户原话：' + quote, evidence)
            with patch.object(app.root, 'clipboard_clear') as clear, patch.object(app.root, 'clipboard_append') as copy:
                panel.source_copy_button.invoke()
            clear.assert_called_once()
            copy.assert_called_once_with(evidence)
            panel.source_button.invoke()
            self.assertEqual(panel.source_frame.winfo_manager(), '')
            self.assertEqual(panel.source_button.cget('text'), '查看来源')
            panel.pin_option.invoke()
            self.assertTrue(store.get(automatic['id'])['pinned'])
            self.assertIn('在适用范围内时优先提供', panel.pin_scope_note.cget('text'))
            self.assertIn('上下文空间不足时可能省略', panel.pin_scope_note.cget('text'))
            self.assertTrue(any('置顶 · AI 未核实 · 构建工具' in item for item in panel.listbox.get(0, 'end')))
            panel.verify_button.invoke()
            verified = store.get(automatic['id'])
            self.assertEqual((verified['source'], verified['verified'], verified['status']), ('agent', True, 'active'))
            self.assertIn('AI 记下 · 已核实', panel.provenance.cget('text'))
            self.assertEqual(panel.verify_button.cget('text'), '已核实')
            self.assertEqual(panel.verify_button.cget('state'), 'disabled')
            self.assertIn('不再被自动覆盖', panel.notice.get())

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
            self.assertTrue(app.last_answer.startswith('测试聊天回答'))
            self.assertIn('本轮向模型提供了 2 条记忆作为参考', app.last_answer)
            self.assertIn(quote, runtime.prompts[0])

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
            reopened = new_app()
            restored = reopened.memories
            reopened.open_memory()
            select(restored, automatic['id'])
            self.assertTrue(restored.pin_enabled.get())
            self.assertIn('已核实', restored.provenance.cget('text'))
            restored.source_button.invoke()
            self.assertEqual(restored.source_text.get('1.0', 'end-1c'), evidence)
            self.assertTrue(any('置顶 · AI 已核实 · 构建工具' in item for item in restored.listbox.get(0, 'end')))
            self.assertFalse(restored.read_enabled.get())
            self.assertFalse(restored.auto_enabled.get())
            self.assertTrue(restored.remote_enabled.get())
            restored.options[0].invoke()
            self.assertEqual(restored.snapshot('api'), {'read': True, 'write': False})
            reopened.new_conversation()
            reopened.project.assistant_enabled.set(False)
            reopened.backend, reopened.selected_model = 'local', 'text-default'
            after_restart = FakeRuntime()
            put(reopened.input, '请简单解释二分查找。')
            with patch.object(reopened.manager, 'ready', return_value=True), patch('qwen_chat.runtime', return_value=after_restart):
                reopened.send_chat()
                settle(reopened)
            self.assertIn(quote, after_restart.prompts[0])
            self.assertIn('本轮向模型提供了', reopened.last_answer)
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
