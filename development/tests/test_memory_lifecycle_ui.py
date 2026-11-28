"""Exercise the native memory lifecycle controls with a temporary database and clock."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BUNDLED_PYTHON = ROOT / 'runtime' / 'python' / 'python.exe'


class NativeLifecycleScenario(unittest.TestCase):
    def runTest(self):
        import tkinter as tk

        from agent_memory import AgentMemoryStore
        from client_app import ClientApp
        from paths import ARTIFACTS
        from ui_kit import set_text_size

        scratch = Path(os.environ['AI_TRAINING_DATA'])
        self.assertEqual(ARTIFACTS, scratch)
        now = datetime(2020, 1, 1, tzinfo=timezone.utc)
        store = AgentMemoryStore(scratch / 'lifecycle', clock=lambda: now)
        original = store.remember('构建命令', '在仓库根目录执行构建。', evidence='用户原话：从仓库根目录构建。')
        identity = original['id']
        other = store.remember('项目规范', '项目日志采用结构化记录。',
                               scope='project', workspace=scratch / 'other')
        root = tk.Tk()
        root.geometry('+9000+9000')
        callback_errors = []
        root.report_callback_exception = lambda kind, error, traceback: callback_errors.append(error)
        app = ClientApp(root)
        self.addCleanup(app.close)
        app.personal_memory = store
        set_text_size(root, 'extra_large')
        root.geometry(f'{app.px(940)}x{app.px(640)}+9000+9000')
        root.update()
        app.open_memory()
        root.update()
        panel = app.memories
        self.assertTrue(app._memory_drawer)
        self.assertIn('自动归档', panel.retention_note.cget('text'))
        self.assertIn('原文会保留', panel.retention_note.cget('text'))

        panel.listbox.selection_set(0)
        panel.select()
        self.assertEqual(panel.identity, identity)
        self.assertEqual(panel.importance.get(), '普通')
        self.assertEqual(panel.restore_button.winfo_manager(), '')
        panel.importance_input.current(1)
        panel.importance_input.event_generate('<<ComboboxSelected>>')
        self.assertGreater(store.get(identity)['importance'], original['importance'])
        self.assertEqual(store.get(identity)['source'], 'agent')
        self.assertFalse(store.get(identity)['pinned'])
        panel.importance_input.current(0)
        panel.importance_input.event_generate('<<ComboboxSelected>>')
        self.assertEqual(store.get(identity)['importance'], original['importance'])

        now += timedelta(days=3650)
        panel.refresh()
        self.assertTrue(store.get(identity)['archived'])
        self.assertFalse(store.get(other['id'])['archived'])
        self.assertIn('已归档', panel.listbox.get(0))
        self.assertNotIn(identity, store.before_call('构建命令')['memory_ids'])
        self.assertIn('已归档', panel.provenance.cget('text'))
        self.assertEqual(panel.restore_button.winfo_manager(), 'pack')
        self.assertEqual(panel.restore_button.cget('state'), 'normal')
        panel.source_button.invoke()
        self.assertEqual(panel.source_text.get('1.0', 'end-1c'), original['evidence'])

        app.busy = True
        panel.sync_busy()
        self.assertTrue(panel.importance_input.instate(['disabled']))
        self.assertEqual(panel.restore_button.cget('state'), 'disabled')
        panel.restore_button.invoke()
        self.assertTrue(store.get(identity)['archived'])
        app.busy = False
        panel.sync_busy()

        panel.status_button.invoke()
        self.assertEqual(store.get(identity)['status'], 'disabled')
        panel.status_button.invoke()
        self.assertEqual(store.get(identity)['status'], 'active')
        self.assertTrue(store.get(identity)['archived'])
        self.assertIn('恢复这条记忆后', panel.notice.get())

        root.update()
        for widget in (panel.importance_input, panel.restore_button):
            with self.subTest(widget=widget):
                self.assertEqual(widget.winfo_width(), widget.winfo_reqwidth())
        panel.restore_button.invoke()
        self.assertEqual(panel.restore_button.winfo_manager(), '')
        self.assertNotIn('已归档', panel.listbox.get(0))
        self.assertIn('已恢复', panel.notice.get())
        self.assertIn(identity, store.before_call('构建命令')['memory_ids'])
        restored = store.get(identity)
        for field in ('content', 'evidence', 'source', 'verified', 'pinned', 'status'):
            self.assertEqual(restored[field], original[field])

        panel.new()
        self.assertEqual(panel.importance.get(), '普通')
        panel.importance_input.current(1)
        panel.importance_input.event_generate('<<ComboboxSelected>>')
        panel.title.set('答复习惯')
        panel.content.insert('1.0', '先给结论，再解释原因。')
        panel.save_button.invoke()
        created = store.get(panel.identity)
        self.assertEqual(created['source'], 'user')
        self.assertGreater(created['importance'], original['importance'])
        self.assertFalse(callback_errors, callback_errors)


class MemoryLifecycleUITest(unittest.TestCase):
    def test_native_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix='ui-memory-lifecycle-', dir=ROOT / 'development') as scratch:
            code = (
                'import sys, unittest; from pathlib import Path; '
                'sys.path.insert(0, str(Path("development/tests").resolve())); '
                'from test_memory_lifecycle_ui import NativeLifecycleScenario; '
                'result = unittest.TextTestRunner(verbosity=2).run(NativeLifecycleScenario()); '
                'sys.exit(not result.wasSuccessful())'
            )
            result = subprocess.run(
                [str(BUNDLED_PYTHON if BUNDLED_PYTHON.is_file() else sys.executable), '-E', '-s', '-B', '-X', 'utf8', '-c', code],
                cwd=ROOT, env=dict(os.environ, AI_TRAINING_DATA=scratch), capture_output=True,
                text=True, encoding='utf-8', timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(MemoryLifecycleUITest)


if __name__ == '__main__':
    unittest.main()
