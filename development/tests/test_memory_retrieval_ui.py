"""Use the real native retrieval tab and store with isolated app data."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
BUNDLED_PYTHON = ROOT / 'runtime' / 'python' / 'python.exe'


class NativeRetrievalScenario(unittest.TestCase):
    def runTest(self):
        import sqlite3
        import tkinter as tk

        from client_app import ClientApp
        from paths import ARTIFACTS
        from ui_kit import set_text_size

        scratch = Path(os.environ['AI_TRAINING_DATA'])
        self.assertEqual(ARTIFACTS, scratch)
        root = tk.Tk()
        callback_errors = []
        root.report_callback_exception = lambda kind, error, traceback: callback_errors.append(error)
        app = ClientApp(root)
        self.addCleanup(app.close)
        store = app.personal_memory
        primary = store.remember('构建脚本', '从仓库根目录执行构建脚本。', evidence='用户原话：执行构建脚本。')
        disabled = store.remember('旧版构建脚本', '过去使用单独的脚本。')
        store.set_status(disabled['id'], 'disabled')
        project = scratch / 'first-project'
        store.remember('项目构建脚本', '在项目目录执行。', scope='project', workspace=project)
        store.remember('其他项目脚本', '属于其他项目。', scope='project', workspace=scratch / 'second-project')
        app.project.assistant_enabled.set(False)
        set_text_size(root, 'extra_large')
        root.geometry(f'{app.px(940)}x{app.px(640)}+9000+9000')
        root.update()
        app.open_memory()
        root.update()
        panel, preview = app.memories, app.memories.retrieval
        self.assertTrue(app._memory_drawer)
        self.assertTrue(panel.save_button.winfo_ismapped())
        self.assertTrue(panel.delete_button.winfo_ismapped())
        self.assertFalse(preview.winfo_ismapped())

        index = next(i for i, item in enumerate(panel.rows) if item['id'] == primary['id'])
        panel.listbox.selection_set(index)
        panel.select()
        panel.title.set('未保存的修改')
        panel.delete_button.invoke()
        self.assertEqual(panel.delete_pending, primary['id'])
        panel.retrieval_tab.invoke()
        root.update()
        self.assertTrue(preview.winfo_ismapped())
        self.assertFalse(panel.save_button.winfo_ismapped())
        self.assertFalse(panel.delete_button.winfo_ismapped())
        self.assertEqual(panel.save_button.cget('state'), 'disabled')
        self.assertEqual(panel.delete_button.cget('state'), 'disabled')
        self.assertIsNone(panel.delete_pending)

        def audit_count():
            with sqlite3.connect(store.path) as conn:
                return conn.execute('SELECT COUNT(*) FROM memory_audit').fetchone()[0]

        before = store.list()
        audit_before = audit_count()
        query = '怎样执行构建脚本？'
        preview.query.set(query)
        expected = store.preview_call(query, budget=1400)
        with patch.object(store, 'preview_call', wraps=store.preview_call) as call:
            preview.test_button.invoke()
            call.assert_called_once_with(query, workspace='', budget=1400, max_items=None, minimum_match=1)
        self.assertEqual(preview.context_text.get('1.0', 'end-1c'), expected['context'])
        self.assertIn('预览采用 1 条', preview.summary.get())
        details = preview.items_text.get('1.0', 'end-1c')
        for phrase in ('已采用', '未采用', '旧版构建脚本', '已停用', '用户原话：执行构建脚本。'):
            self.assertIn(phrase, details)
        self.assertNotIn('项目构建脚本', details)
        self.assertEqual(store.list(), before)
        self.assertEqual(audit_count(), audit_before)

        with patch.object(preview, 'clipboard_clear') as clear, patch.object(preview, 'clipboard_append') as copy:
            preview.copy_button.invoke()
        clear.assert_called_once_with()
        copy.assert_called_once_with(expected['context'])
        self.assertEqual(preview.summary.get(), '内容已复制。')

        app.backend = 'api'
        panel.refresh_mode()
        self.assertIn('当前回答不会读取记忆', preview.availability.get())
        preview.limit_input.current(1)
        preview.match_input.current(2)
        self.assertFalse(preview.results.winfo_manager())
        with patch.object(store, 'preview_call', wraps=store.preview_call) as call:
            preview.test_button.invoke()
            call.assert_called_once_with(query, workspace='', budget=1400, max_items=3, minimum_match=6)
            app.busy = True
            panel.sync_busy()
            self.assertEqual(preview.test_button.cget('state'), 'disabled')
            preview.test_button.invoke()
            call.assert_called_once()
            app.busy = False
            panel.sync_busy()

        app.backend = 'local'
        app.project.assistant_enabled.set(True)
        panel.refresh_mode()
        self.assertIn('智能助手', preview.availability.get())
        self.assertIn('1800', preview.availability.get())
        self.assertFalse(panel._retrieval_route('今天星期几')['read_enabled'])
        app.tool_mode.set(True)
        panel.refresh_mode()
        self.assertIn('当前回答不会读取记忆', preview.availability.get())
        app.tool_mode.set(False)
        app.project.enabled.set(True)
        app.project.workspace.set(str(project))
        panel.refresh()
        self.assertFalse(preview.results.winfo_manager())
        preview.test_button.invoke()
        project_details = preview.items_text.get('1.0', 'end-1c')
        self.assertIn('项目构建脚本', project_details)
        self.assertNotIn('其他项目脚本', project_details)
        preview.query.set('完全无关的主题')
        preview.test_button.invoke()
        self.assertIn('预览采用 0 条', preview.summary.get())
        self.assertIn('没有找到与问题匹配', preview.items_text.get('1.0', 'end-1c'))
        self.assertEqual(preview.copy_button.cget('state'), 'disabled')

        preview.query.set(query)
        preview.test_button.invoke()
        self.assertIn('构建脚本', preview.context_text.get('1.0', 'end-1c'))
        self.assertEqual(preview.copy_button.cget('state'), 'normal')
        panel.manage_tab.invoke()
        root.update()
        self.assertEqual(panel.title.get(), '未保存的修改')
        self.assertTrue(panel.save_button.winfo_ismapped())
        panel.save_button.invoke()
        self.assertEqual(store.get(primary['id'])['title'], '未保存的修改')
        panel.retrieval_tab.invoke()
        self.assertEqual(preview.query.get(), query)
        self.assertEqual(preview.limit_input.current(), 1)
        self.assertEqual(preview.match_input.current(), 2)
        self.assertFalse(preview.results.winfo_manager())
        self.assertEqual(preview.context_text.get('1.0', 'end-1c'), '')
        self.assertEqual(preview.items_text.get('1.0', 'end-1c'), '')
        self.assertEqual(preview.copy_button.cget('state'), 'disabled')
        panel.toggle()
        panel.toggle()
        panel.retrieval_tab.invoke()
        self.assertEqual(preview.query.get(), '')
        self.assertEqual(preview.limit_input.current(), 0)
        self.assertEqual(preview.match_input.current(), 0)
        self.assertFalse(preview.results.winfo_manager())
        self.assertFalse(callback_errors, callback_errors)


class MemoryRetrievalUITest(unittest.TestCase):
    def test_native_retrieval_preview(self):
        with tempfile.TemporaryDirectory(prefix='ui-memory-retrieval-', dir=ROOT / 'development') as scratch:
            code = (
                'import sys, unittest; from pathlib import Path; '
                'sys.path.insert(0, str(Path("development/tests").resolve())); '
                'from test_memory_retrieval_ui import NativeRetrievalScenario; '
                'result = unittest.TextTestRunner(verbosity=2).run(NativeRetrievalScenario()); '
                'sys.exit(not result.wasSuccessful())'
            )
            result = subprocess.run(
                [str(BUNDLED_PYTHON if BUNDLED_PYTHON.is_file() else sys.executable), '-E', '-s', '-B', '-X', 'utf8', '-c', code],
                cwd=ROOT, env=dict(os.environ, AI_TRAINING_DATA=scratch), capture_output=True,
                text=True, encoding='utf-8', timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(MemoryRetrievalUITest)


if __name__ == '__main__':
    unittest.main()
