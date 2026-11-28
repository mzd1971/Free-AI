"""Regression checks for the native trainer's navigation and reflow."""

from functools import partial
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from app_state import Sessions
from development.tests.test_training_publish import fixture_run
from trainer_app import TrainerApp
from ui_controls import SwitchRow
from ui_preferences import load_text_size, save_text_size


class TrainerStudioTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / 'tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(prefix='trainer-studio-test-', dir=temporary_root)
        self.addCleanup(scratch.cleanup)
        self.folder = Path(scratch.name)
        self.outputs = self.folder / 'outputs'
        self.outputs.mkdir()

        from capability_manager import PackageManager
        from epistemology import KnowledgeStore
        from trainer_app import training_runs

        patches = (
            patch('trainer_app.ARTIFACTS', self.folder),
            patch('trainer_app.DATA_ROOT', self.folder / 'teaching'),
            patch('trainer_app.PackageManager', partial(PackageManager, self.folder / 'models')),
            patch('trainer_app.KnowledgeStore', partial(KnowledgeStore, self.folder / 'knowledge.sqlite3')),
            patch('trainer_app.load_text_size', partial(load_text_size, self.folder)),
            patch('trainer_app.save_text_size', partial(save_text_size, directory=self.folder)),
            patch('trainer_app.training_runs', partial(training_runs, self.outputs)),
            patch.object(TrainerApp, 'model_ready', return_value=False),
        )
        for replacement in patches:
            replacement.start()
            self.addCleanup(replacement.stop)
        guard = patch.object(TrainerApp, 'submit', side_effect=AssertionError('UI initiated a background job'))
        self.submit = guard.start()
        self.addCleanup(guard.stop)

    def open_studio(self):
        self.root = tk.Tk()
        try:
            self.app = TrainerApp(self.root)
        except Exception:
            self.root.destroy()
            raise
        self.addCleanup(self.app.close)
        self.errors = []
        self.root.report_callback_exception = lambda kind, error, trace: self.errors.append(error)
        self.resize(1550, 910)
        self.assertEqual(self.app.status.get(), '先选择训练模型，再检查练习题。')
        self.submit.assert_not_called()
        return self.app

    def resize(self, width, height):
        self.root.geometry(f'{width}x{height}+2500+1400')
        self.root.update()

    def assert_button_fits(self, button):
        self.assertTrue(button.winfo_ismapped(), button.cget('text'))
        self.assertLessEqual(button.winfo_x() + button.winfo_width(), button.master.winfo_width())
        caption = next(item for item in button.find_all() if button.type(item) == 'text')
        self.assertEqual(button.itemcget(caption, 'text'), button.cget('text'))
        left, top, right, bottom = button.bbox(caption)
        self.assertGreaterEqual(left, 0, button.cget('text'))
        self.assertGreaterEqual(top, 0, button.cget('text'))
        self.assertLessEqual(right, button.winfo_width(), button.cget('text'))
        self.assertLessEqual(bottom, button.winfo_height(), button.cget('text'))

    def test_training_reflows_and_keeps_edit_state(self):
        app = self.open_studio()
        self.assertEqual(app.studio_form.grid_info()['row'], app.studio_overview.grid_info()['row'])
        self.assertNotEqual(app.studio_form.grid_info()['column'], app.studio_overview.grid_info()['column'])

        app.dataset.set(str(self.folder / '草稿数据'))
        app.epochs.set('3')
        app.learning_rate.set('0.0002')
        app.toggle_parameters()
        self.root.update()
        self.assertTrue(app.parameters.winfo_ismapped())
        switch = app.agent_target_switch
        self.assertIsInstance(switch, SwitchRow)
        self.assertFalse(app.agent_target.get())
        switch.invoke()
        self.assertTrue(app.agent_target.get())

        self.resize(760, 650)
        app.text_size_picker.set('特大')
        app.text_size_picker.event_generate('<<ComboboxSelected>>')
        self.root.update()
        self.assertGreater(app.studio_overview.grid_info()['row'], app.studio_form.grid_info()['row'])
        self.assertEqual(load_text_size(self.folder), 'extra_large')
        self.assertEqual(app.dataset.get(), str(self.folder / '草稿数据'))
        self.assertEqual(app.epochs.get(), '3')
        self.assertEqual(app.learning_rate.get(), '0.0002')
        self.assertTrue(app.parameters.winfo_ismapped())
        self.assertTrue(switch.winfo_ismapped())
        self.assertEqual(switch.winfo_width(), app.parameters.winfo_width())
        self.assertTrue(app.agent_target.get())
        for button in (*app.nav, *app.studio_steps, app.model_button):
            self.assert_button_fits(button)
        app.studio_steps[2].invoke()
        self.root.update()
        self.assertEqual(app.studio_step_index, 2)
        canvas = app.train_scroll.canvas
        self.assertGreater(canvas.yview()[0], 0)
        offset = app.start_button.winfo_rooty() - app.train_scroll.body.winfo_rooty()
        canvas.yview_moveto(max(0, offset - 8) / app.train_scroll.body.winfo_height())
        self.root.update()
        self.assert_button_fits(app.start_button)
        self.assertGreaterEqual(app.start_button.winfo_rooty(), canvas.winfo_rooty())
        self.assertLessEqual(app.start_button.winfo_rooty() + app.start_button.winfo_height(),
                             canvas.winfo_rooty() + canvas.winfo_height())
        app.start_button.invoke()
        self.assertFalse(app.busy)
        self.submit.assert_not_called()
        self.assertEqual(self.errors, [])

    def test_switch_renders_in_dark_and_neon_themes(self):
        for theme in ('dark', 'cyberpunk'):
            with self.subTest(theme=theme):
                Sessions(self.folder).set('trainer_theme', theme)
                root = tk.Tk()
                try:
                    app = TrainerApp(root)
                except Exception:
                    root.destroy()
                    raise
                try:
                    root.geometry('760x650+2500+1400')
                    app.toggle_parameters()
                    root.update()
                    switch = app.agent_target_switch
                    self.assertEqual(app.theme, theme)
                    self.assertTrue(switch.winfo_ismapped())
                    self.assertEqual(switch.cget('bg'), app.colors['surface'])
                    self.assertEqual(len(app.stop_buttons), 1)
                    switch.invoke()
                    self.assertTrue(app.agent_target.get())
                    self.submit.assert_not_called()
                finally:
                    app.close()

    def test_navigation_records_and_online_panel_are_idle(self):
        completed = fixture_run(self.outputs / 'qwen-lora-studio-fixture')
        interrupted = self.outputs / 'qwen-lora-interrupted'
        interrupted.mkdir()
        app = self.open_studio()
        self.assertEqual(app.page_index, 0)
        app.online_data_button.invoke()
        self.root.update()
        self.assertTrue(app.dataset_panel.winfo_ismapped())
        app.online_data_button.invoke()
        self.root.update()
        self.assertFalse(app.dataset_panel.winfo_ismapped())

        app.nav[1].invoke()
        self.root.update()
        self.assertEqual(app.page_index, 1)
        self.assertTrue(app.records_scroll.winfo_ismapped())
        self.assertFalse(app.train_scroll.winfo_ismapped())
        app.latest_result()
        self.root.update()
        self.assertEqual(app.result['path'], completed)
        self.assertEqual(app.result['acceptance']['status'], 'passed')
        self.assertIn('验收通过', app.record_summary.get())
        self.assertEqual(app.publish_button.cget('state'), 'normal')
        self.assertIn('B', app.before_answer.get('1.0', 'end-1c'))
        self.assertIn('A', app.after_answer.get('1.0', 'end-1c'))

        unfinished_id = next(identity for identity, row in app.run_rows.items() if row['path'] == interrupted)
        app.run_tree.selection_set(unfinished_id)
        self.root.update()
        self.assertIsNone(app.result)
        self.assertIn('未完成', app.record_summary.get())
        self.assertEqual(app.publish_button.cget('state'), 'disabled')

        counts = {'all': '2', 'passed': '1', 'review': '0', 'failed': '1'}
        self.assertEqual({key: value.get() for key, value in app.record_totals.items()}, counts)
        app.record_filter_picker.set('验收通过')
        app.record_filter_picker.event_generate('<<ComboboxSelected>>')
        self.root.update()
        self.assertEqual([row['path'] for row in app.run_rows.values()], [completed])
        app.record_filter_picker.set('需要核实')
        app.record_filter_picker.event_generate('<<ComboboxSelected>>')
        self.root.update()
        self.assertFalse(app.run_tree.get_children())
        self.assertTrue(app.records_empty.winfo_ismapped())
        self.assertFalse(app.result_area.winfo_ismapped())
        app.clear_record_filter()
        self.root.update()
        self.assertEqual({row['path'] for row in app.run_rows.values()}, {completed, interrupted})
        self.assertEqual({key: value.get() for key, value in app.record_totals.items()}, counts)

        with patch.object(app.knowledge_panel, 'refresh') as refresh:
            app.nav[2].invoke()
            refresh.assert_called_once_with()
        self.root.update()
        self.assertTrue(app.knowledge_scroll.winfo_ismapped())
        self.assertFalse(app.records_scroll.winfo_ismapped())
        app.nav[0].invoke()
        self.root.update()
        self.assertTrue(app.train_scroll.winfo_ismapped())
        self.assertIsNone(app.current_job)
        self.submit.assert_not_called()
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
