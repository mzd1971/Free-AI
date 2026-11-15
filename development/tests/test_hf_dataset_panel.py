"""Offline widget tests for the Hugging Face trainer dataset chooser."""
from pathlib import Path
from queue import Empty, Queue
from threading import Event
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import hf_datasets
from hf_dataset_panel import DATASET_HOME, RECOMMENDED_DATASETS, HuggingFaceDatasetPanel
from ui_kit import ScrollArea, set_text_size, setup_theme


REPOSITORY = {'id': 'example/questions', 'downloads': 32, 'likes': 1, 'license': 'apache-2.0'}
OTHER = {'id': 'example/other', 'downloads': 3, 'likes': 0, 'license': ''}
SPLITS = [
    {'config': 'translated', 'split': 'test'},
    {'config': 'standard', 'split': 'train'},
    {'config': 'standard', 'split': 'validation'},
]
PREVIEW = {
    'columns': ['question', 'answer', 'context', 'full_field_name_that_should_remain_visible'],
    'rows': [
        {'question': '什么是上下文？', 'answer': '辅助信息。', 'context': 'x' * 400},
        {'question': '你好？', 'answer': '你好。'},
    ],
    'truncated_cells': [[], ['answer']],
    'total': 300,
}
IMPORTED = {
    'path': Path('local_training_data'),
    'counts': {'train': 16, 'validation': 2, 'test': 2, 'general_test': 4},
    'skipped': {'invalid': 1},
    'report': Path('local_training_data', 'source.json'),
    'fetched': 21,
}


class TrainerFixture:
    def __init__(self, root):
        self.busy = False
        self.events = Queue()
        self.cancel_event = Event()
        self.status = tk.StringVar(root)
        self.tasks = []

    def submit(self, title, work, done, *, kind, cancellable):
        if self.busy:
            raise AssertionError('panel submitted during a trainer task')
        self.busy = True
        self.status.set(title)
        self.cancel_event.clear()
        self.tasks.append((work, done, kind, cancellable))

    def stop_training(self):
        self.cancel_event.set()

    def complete(self):
        work, done, kind, cancellable = self.tasks.pop(0)
        result = work()
        while True:
            try:
                action, callback, value = self.events.get_nowait()
            except Empty:
                break
            if action != 'update':
                raise AssertionError(action)
            callback(value)
        self.busy = False
        done(result)


class DatasetPanelTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk unavailable: {exc}')
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        setup_theme(self.root, 'FreeAI trainer test', theme='cyberpunk')
        self.app = TrainerFixture(self.root)
        self.imported = Mock()
        self.search_data = self._mock_backend('search_datasets', [REPOSITORY, OTHER])
        self.splits = self._mock_backend('dataset_splits', SPLITS)
        self.preview = self._mock_backend('preview_rows', PREVIEW)
        self.import_data = self._mock_backend('import_dataset', IMPORTED)
        self.callback_errors = []
        self.root.report_callback_exception = lambda kind, error, traceback: self.callback_errors.append(error)
        scroll = ScrollArea(self.root, scrollbar=True)
        scroll.pack(fill='both', expand=True)
        self.panel = HuggingFaceDatasetPanel(self.app, scroll.body, self.imported)
        self.panel.pack(fill='x')
        self.root.update()
        self.addCleanup(lambda: self.assertFalse(self.callback_errors, self.callback_errors))

    def _mock_backend(self, name, result):
        mock = patch.object(hf_datasets, name, return_value=result)
        self.addCleanup(mock.stop)
        return mock.start()

    def _choose_repo(self):
        self.panel.query.set('example')
        self.panel.search_button.invoke()
        self.app.complete()
        self.panel.repository_tree.selection_set(REPOSITORY['id'])
        self.root.update()
        self.app.complete()

    def _preview_train(self):
        self._choose_repo()
        self.app.complete()

    def test_search_auto_previews_selection_and_waits_for_explicit_download(self):
        self.assertEqual(self.app.tasks, [])
        self.assertEqual(self.panel.search_button.cget('state'), 'disabled')
        self._choose_repo()
        self.search_data.assert_called_once_with('example', cancel=self.app.cancel_event)
        self.splits.assert_called_once_with(REPOSITORY['id'], cancel=self.app.cancel_event)
        self.assertIn('apache-2.0', self.panel.source_description.get())
        self.assertEqual(self.panel.config_name.get(), 'standard')
        self.assertEqual(self.panel.split_name.get(), 'train')
        self.assertEqual(self.panel.import_button.cget('state'), 'disabled')
        self.preview.assert_not_called()
        self.assertEqual(len(self.app.tasks), 1)

        self.app.complete()
        self.preview.assert_called_once_with(REPOSITORY['id'], 'standard', 'train', cancel=self.app.cancel_event)
        text = self.panel.preview_text.get('1.0', 'end')
        self.assertIn('full_field_name_that_should_remain_visible', text)
        self.assertIn('什么是上下文', text)
        self.assertIn('你好。…', text)
        self.assertNotIn('x' * 321, text)
        self.assertEqual(self.panel.fields['prompt'].get(), 'question')
        self.assertEqual(self.panel.fields['response'].get(), 'answer')
        self.assertEqual(self.panel.fields['input'].get(), 'context')
        self.import_data.assert_not_called()
        self.imported.assert_not_called()

        self.panel.import_count.set('19')
        self.assertEqual(self.panel.import_button.cget('state'), 'disabled')
        self.panel.import_count.set('1001')
        self.assertEqual(self.panel.import_button.cget('state'), 'disabled')
        self.panel.import_count.set('21')
        self.panel.fields['response'].set('question')
        self.assertEqual(self.panel.import_button.cget('state'), 'disabled')
        self.panel.fields['response'].set('answer')
        self.panel.import_button.invoke()
        self.panel.import_button.invoke()
        self.assertEqual(len(self.app.tasks), 1)
        self.assertEqual(self.app.tasks[0][2:], ('hf-dataset', True))
        self.app.complete()
        args = self.import_data.call_args.args
        self.assertEqual(args, (REPOSITORY['id'], 'standard', 'train', 21,
                                {'prompt': 'question', 'input': 'context', 'response': 'answer'}))
        self.imported.assert_called_once_with(IMPORTED)
        self.assertIn('21 源行', self.panel.status.get())
        self.assertIn('跳过 1 行', self.panel.status.get())
        self.assertIn('本地基础检查 4', self.panel.status.get())

    def test_recommendation_uses_default_train_and_recovers_failed_preview(self):
        title, repo_id, _ = RECOMMENDED_DATASETS[0]
        self.assertEqual(self.panel.query_entry.cget('values'), tuple(f'{title} · {repo}' for title, repo, _ in RECOMMENDED_DATASETS))
        self.assertEqual(self.panel.import_count.get(), '100')
        self.splits.return_value = [*SPLITS, {'config': 'default', 'split': 'train'}]
        self.preview.side_effect = hf_datasets.DatasetError('预览暂时不可用')
        self.panel.query_entry.current(0)
        self.panel.query_entry.event_generate('<<ComboboxSelected>>')
        self.app.complete()
        self.assertIn(title, self.panel.search_message.get())
        self.assertIn(repo_id, self.panel.source_description.get())
        self.assertEqual(self.panel.config_name.get(), 'default')
        self.app.complete()
        self.preview.assert_called_once_with(repo_id, 'default', 'train', cancel=self.app.cancel_event)
        self.assertIn('暂时不可用', self.panel.status.get())
        self.assertEqual(self.panel.import_button.cget('state'), 'disabled')

        self.preview.side_effect = None
        self.panel.preview_button.invoke()
        self.app.complete()
        self.search_data.assert_not_called()
        self.import_data.assert_not_called()
        self.imported.assert_not_called()
        self.assertEqual(self.panel.import_button.cget('text'), '下载训练数据')
        with patch('hf_dataset_panel.webbrowser.open') as opened:
            self.panel.website_button.invoke()
            self.panel.open_button.invoke()
        self.assertEqual([call.args[0] for call in opened.call_args_list],
                         [DATASET_HOME, f'{DATASET_HOME}/{repo_id}'])
        self.panel.import_button.invoke()
        self.app.complete()
        self.assertEqual(self.import_data.call_args.args[:4], (repo_id, 'default', 'train', 100))
        self.imported.assert_called_once_with(IMPORTED)

    def test_recommendations_are_visible_and_typing_fuzzy_suggestions_stays_offline(self):
        tree = self.panel.repository_tree
        self.assertEqual(set(tree.get_children()), {repo for _, repo, _ in RECOMMENDED_DATASETS})
        for title, repo, _ in RECOMMENDED_DATASETS:
            self.assertIn(title, tree.item(repo, 'values')[0])
        self.assertFalse(tree.selection())
        self.panel.query.set('代码')
        self.assertTrue(any('CodeAlpaca' in item for item in self.panel.query_entry.cget('values')))
        self.panel.query.set('alpca')
        self.assertTrue(any('alpaca' in item.lower() for item in self.panel.query_entry.cget('values')))
        self.panel.query.set('中文')
        self.assertTrue(any('alpaca_gpt4_zh' in item for item in self.panel.query_entry.cget('values')))
        self.search_data.assert_not_called()
        self.splits.assert_not_called()
        self.preview.assert_not_called()
        self.import_data.assert_not_called()
        self.assertFalse(self.app.tasks)

    def test_nontraining_split_can_preview_but_cannot_import(self):
        self._preview_train()
        self.panel.config_name.set('translated')
        self.assertEqual(self.panel.split_name.get(), 'test')
        self.assertEqual(self.panel.import_button.cget('state'), 'disabled')
        self.panel.preview_button.invoke()
        self.app.complete()
        self.preview.assert_called_with(REPOSITORY['id'], 'translated', 'test', cancel=self.app.cancel_event)
        self.assertEqual(self.panel.import_button.cget('state'), 'disabled')
        self.assertIn('训练部分（train）', self.panel.status.get())
        self.panel.config_name.set('standard')
        self.assertEqual(self.panel.import_button.cget('state'), 'disabled')
        self.import_data.assert_not_called()

    def test_repreview_keeps_confirmed_field_choices(self):
        self._preview_train()
        self.panel.fields['prompt'].set('context')
        self.panel.fields['input'].set('')
        self.panel.preview_button.invoke()
        self.app.complete()
        self.assertEqual(self.panel.fields['prompt'].get(), 'context')
        self.assertEqual(self.panel.fields['input'].get(), '')
        self.assertEqual(self.panel.import_button.cget('state'), 'normal')

    def test_message_column_and_cancelled_import_can_retry(self):
        self.preview.return_value = {
            'columns': ['id', 'messages'],
            'rows': [{'id': 1, 'messages': [{'role': 'user', 'content': '你好'},
                                           {'role': 'assistant', 'content': '您好'}]}],
            'total': 60,
        }
        self._preview_train()
        self.assertEqual(self.panel.fields['messages'].get(), 'messages')
        self.assertEqual(str(self.panel.field_boxes['prompt'].cget('state')), 'disabled')

        def import_with_progress(*args, cancel, progress):
            self.assertEqual(args[-1], {'messages': 'messages'})
            if cancel.is_set():
                raise hf_datasets.DatasetCancelled('取消')
            progress(21, args[-2])
            return IMPORTED

        self.import_data.side_effect = import_with_progress
        self.panel.import_button.invoke()
        self.panel.stop_button.invoke()
        self.assertTrue(self.app.cancel_event.is_set())
        self.assertEqual(self.panel.stop_button.cget('state'), 'disabled')
        self.app.complete()
        self.imported.assert_not_called()
        self.assertIn('已停止', self.panel.status.get())

        self.panel.import_button.invoke()
        self.app.complete()
        self.imported.assert_called_once_with(IMPORTED)

    def test_failed_or_stale_search_keeps_existing_valid_source(self):
        self._preview_train()
        self.panel.query.set('previous')
        self.panel.search_button.invoke()
        self.panel.query.set('new')
        self.search_data.return_value = []
        self.app.complete()
        self.assertIn('关键词已改动', self.panel.search_message.get())
        self.assertEqual(self.panel.repository_tree.selection(), (REPOSITORY['id'],))
        self.assertEqual(self.panel.import_button.cget('state'), 'normal')

        self.search_data.side_effect = hf_datasets.DatasetError('网络连接超时')
        self.panel.search_button.invoke()
        self.app.complete()
        self.assertIn('网络连接超时', self.panel.search_message.get())
        self.assertEqual(self.panel.import_button.cget('state'), 'normal')

    def test_shared_busy_prevents_source_changes_and_submission(self):
        self._preview_train()
        self.app.busy = True
        self.panel.sync_busy()
        self.assertEqual(self.panel.import_button.cget('state'), 'disabled')
        self.assertEqual(self.panel.search_button.cget('state'), 'disabled')
        self.assertEqual(str(self.panel.config_box.cget('state')), 'disabled')
        self.panel.repository_tree.selection_set(OTHER['id'])
        self.root.update()
        self.assertEqual(self.panel.repository_tree.selection(), (REPOSITORY['id'],))
        self.assertFalse(self.app.tasks)
        self.app.busy = False
        self.panel.sync_busy()
        self.assertEqual(self.panel.import_button.cget('state'), 'normal')

    def test_split_error_can_be_retried(self):
        self.panel.query.set('example')
        self.panel.search_button.invoke()
        self.app.complete()
        self.splits.side_effect = hf_datasets.DatasetError('数据集预览服务暂时不可用')
        self.panel.repository_tree.selection_set(REPOSITORY['id'])
        self.root.update()
        self.app.complete()
        self.assertIn('暂时不可用', self.panel.status.get())
        self.assertEqual(self.panel.reload_button.winfo_manager(), 'pack')
        self.splits.side_effect = None
        self.panel.reload_button.invoke()
        self.app.complete()
        self.assertEqual(self.panel.reload_button.winfo_manager(), '')
        self.assertEqual(self.panel.preview_button.cget('state'), 'disabled')
        self.app.complete()
        self.assertEqual(self.panel.preview_button.cget('state'), 'normal')

    def test_controls_wrap_at_extra_large_text_size(self):
        self._preview_train()
        set_text_size(self.root, 'extra_large')
        self.root.minsize(320, 560)
        self.root.geometry('320x720+2500+1400')
        self.root.deiconify()
        self.root.update()
        self.assertNotEqual(self.panel.field_boxes['input'].master.grid_info()['row'],
                            self.panel.field_boxes['messages'].master.grid_info()['row'])
        for button in (self.panel.website_button, self.panel.preview_button, self.panel.import_button):
            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
            text_item = next(item for item in button.find_all() if button.type(item) == 'text')
            self.assertEqual(button.itemcget(text_item, 'text'), button.cget('text'))

        self.root.geometry('900x720+2500+1400')
        self.root.update()
        self.assertEqual(self.panel.field_boxes['input'].master.grid_info()['row'],
                         self.panel.field_boxes['messages'].master.grid_info()['row'])


if __name__ == '__main__':
    unittest.main()
