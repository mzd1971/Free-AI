"""Exercise the Hugging Face chooser without network or changes to local models."""
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from capability_manager import PackageError
from hf_model_panel import HuggingFacePanel
from ui_kit import setup_theme


REPOSITORY = {
    'repo_id': 'example/chat-GGUF',
    'url': 'https://huggingface.co/example/chat-GGUF',
}
AVAILABLE = {
    'repo_id': REPOSITORY['repo_id'],
    'revision': 'a' * 40,
    'filename': 'variants/model-Q4_K_M.gguf',
    'size': 2_123_456_789,
    'sha256': 'b' * 64,
    'available': True,
    'reason': '',
    'license': 'apache-2.0',
}
BLOCKED = dict(AVAILABLE, filename='model-00001-of-00002.gguf', size=None, available=False,
               sha256=None, reason='这个文件属于分卷，当前无法下载。')


class AppFixture:
    def __init__(self):
        self.manager = Mock()
        self.manager.ready.return_value = False
        self.busy = False
        self.downloading = None
        self.tasks = []
        self.start_download = Mock()

    def submit(self, title, work, done):
        if self.busy:
            raise AssertionError('panel submitted while the app was already working')
        self.busy = True
        self.tasks.append((work, done))

    def complete_task(self):
        work, done = self.tasks.pop(0)
        result = work()
        self.busy = False
        done(result)


class HuggingFacePanelTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk unavailable: {exc}')
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        setup_theme(self.root, 'Free AI test')
        self.app = AppFixture()
        search = patch('hf_model_panel.hf_models.search_models', return_value=[REPOSITORY])
        files = patch('hf_model_panel.hf_models.model_files', return_value=[AVAILABLE, BLOCKED])
        add = patch('hf_model_panel.hf_models.add_model', return_value='hf-example')
        self.search_models = search.start()
        self.model_files = files.start()
        self.add_model = add.start()
        for mock in (search, files, add):
            self.addCleanup(mock.stop)
        self.callback_errors = []
        self.root.report_callback_exception = lambda kind, error, traceback: self.callback_errors.append(error)
        self.panel = HuggingFacePanel(self.app, self.root)
        self.panel.pack(fill='x')
        self.root.update()
        self.addCleanup(lambda: self.assertFalse(self.callback_errors, self.callback_errors))

    def choose_repository(self):
        self.panel.query.set('chat')
        self.panel.search_button.invoke()
        self.app.complete_task()
        self.panel.repository_tree.selection_set(REPOSITORY['repo_id'])
        self.root.update()

    def choose_file(self, row):
        self.panel.file_tree.selection_set(row)
        self.root.update()

    def test_search_select_file_and_start_download_only_after_explicit_click(self):
        self.search_models.assert_not_called()
        self.model_files.assert_not_called()
        self.add_model.assert_not_called()
        self.assertFalse(self.app.tasks)
        self.assertEqual(self.panel.search_button.cget('state'), 'disabled')

        self.choose_repository()
        self.search_models.assert_called_once_with('chat')
        self.model_files.assert_not_called()
        self.app.complete_task()
        self.model_files.assert_called_once_with(REPOSITORY['repo_id'])
        self.assertEqual(self.panel.file_tree.item('0', 'values'),
                         (AVAILABLE['filename'], '2.12 GB', '可下载'))
        self.assertEqual(self.panel.file_tree.item('1', 'values'),
                         (BLOCKED['filename'], '未知', '不可下载'))

        self.choose_file('1')
        self.assertIn('分卷', self.panel.file_detail.get())
        self.assertEqual(self.panel.download_button.cget('state'), 'disabled')
        self.panel.download_button.invoke()
        self.assertFalse(self.app.tasks)

        self.choose_file('0')
        self.assertIn('apache-2.0', self.panel.file_detail.get())
        self.assertEqual(self.panel.download_button.cget('state'), 'normal')
        self.add_model.assert_not_called()
        self.app.start_download.assert_not_called()
        self.panel.download_button.invoke()
        self.panel.download_button.invoke()
        self.assertEqual(len(self.app.tasks), 1)
        self.add_model.assert_not_called()
        self.app.complete_task()
        self.add_model.assert_called_once_with(self.app.manager, AVAILABLE)
        self.app.start_download.assert_called_once_with('hf-example')
        self.assertIn('聊天模型', self.panel.download_message.get())

    def test_changed_query_ignores_stale_results_and_allows_retry(self):
        self.panel.query.set('older')
        self.panel.search_button.invoke()
        self.panel.query.set('newer')
        self.assertEqual(self.panel.search_button.cget('state'), 'disabled')
        self.app.complete_task()
        self.assertEqual(self.panel.repository_tree.get_children(), ())
        self.assertIn('关键词已改动', self.panel.search_message.get())

        self.search_models.side_effect = PackageError('无法连接 Hugging Face。')
        self.panel.search_button.invoke()
        self.app.complete_task()
        self.search_models.assert_called_with('newer')
        self.assertIn('无法连接', self.panel.search_message.get())
        self.assertEqual(self.panel.search_button.cget('state'), 'normal')

    def test_already_downloaded_file_can_be_used_from_existing_model_list(self):
        self.choose_repository()
        self.app.complete_task()
        self.choose_file('0')
        self.app.manager.ready.return_value = True
        self.panel.download_button.invoke()
        self.app.complete_task()
        self.app.start_download.assert_not_called()
        self.assertIn('文件已下载', self.panel.download_message.get())

    def test_file_listing_failure_can_be_retried(self):
        self.model_files.side_effect = PackageError('文件列表读取超时。')
        self.choose_repository()
        self.app.complete_task()
        self.assertIn('超时', self.panel.files_message.get())
        self.model_files.side_effect = None
        self.panel.reload_button.invoke()
        self.app.complete_task()
        self.assertEqual(self.panel.file_tree.get_children(), ('0', '1'))


if __name__ == '__main__':
    unittest.main()
