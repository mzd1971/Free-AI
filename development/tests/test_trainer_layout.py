"""Exercise the three native pages against a temporary Free AI data folder."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCENARIO = r'''
import tkinter as tk
from tkinter import font as tkfont
from unittest.mock import patch

from development.tests.test_training_publish import fixture_run
from paths import ARTIFACTS, RUNS_ROOT
from trainer_app import TRAINER_CONTENT_WIDTH, TrainerApp, TrainingKnowledgePanel
from ui_kit import ui_font
from ui_preferences import load_text_size

fixture_run(RUNS_ROOT / 'qwen-lora-layout-fixture')
with patch.object(TrainingKnowledgePanel, 'refresh'):
    root = tk.Tk()
    app = TrainerApp(root)
    errors = []
    root.report_callback_exception = lambda kind, error, trace: errors.append(error)
    actions = (app.tools_button, app.model_button, app.use_model_button)
    root.geometry('1550x820+2500+1400')
    root.update()
    assert len(app.pages) == 3
    assert app.main.winfo_width() == TRAINER_CONTENT_WIDTH
    assert abs(2 * app.main.winfo_x() + app.main.winfo_width() - app.main.master.winfo_width()) <= 2
    assert len({button.winfo_y() for button in actions}) == 1
    app.latest_result()
    root.update()
    assert app.before_panel.grid_info()['row'] == app.after_panel.grid_info()['row']
    normal_row = app.run_tree.bbox(app.run_tree.get_children()[0])[3]
    app.select_page(2)
    root.update()
    panel = app.knowledge_panel
    assert panel.practice_records.grid_info()['column'] != panel.practice_form.grid_info()['column']
    panel.correction.insert('1.0', '尚未保存的纠错草稿')
    app.epochs.set('3')
    app.text_size_picker.current(2)
    app.text_size_picker.event_generate('<<ComboboxSelected>>')
    root.update()
    assert load_text_size() == 'extra_large'
    assert (ARTIFACTS / 'text-size.txt').read_text(encoding='utf-8').strip() == 'extra_large'
    assert ui_font(root).cget('size') == 16
    assert tkfont.nametofont(str(app.text_size_picker.cget('font')), root=root).cget('size') == 16
    root.geometry('760x590+2500+1400')
    root.update()
    assert panel.practice_records.grid_info()['row'] != panel.practice_form.grid_info()['row']
    assert panel.correction.get('1.0', 'end-1c') == '尚未保存的纠错草稿'
    assert app.epochs.get() == '3'
    assert app.text_size_picker.winfo_ismapped()

    app.select_page(0)
    app.toggle_parameters()
    root.update()
    assert len({button.winfo_y() for button in actions}) > 1
    for button in actions:
        assert button.winfo_x() + button.winfo_width() <= button.master.winfo_width()
        text_item = next(item for item in button.find_all() if button.type(item) == 'text')
        assert button.itemcget(text_item, 'text') == button.cget('text')
    assert app.log.cget('yscrollcommand')
    assert app.train_scroll.canvas.cget('yscrollcommand')
    assert app.train_scroll.canvas.yview()[1] < 1
    app.train_scroll.canvas.yview_moveto(1)
    root.update()
    assert app.train_scroll.canvas.yview()[1] == 1.0

    app.latest_result()
    root.update()
    assert app.before_panel.grid_info()['row'] != app.after_panel.grid_info()['row']
    assert app.run_tree.cget('yscrollcommand') and app.run_tree.cget('xscrollcommand')
    assert app.run_tree.bbox(app.run_tree.get_children()[0])[3] > normal_row
    assert not errors, errors
    app.close()

    reopened = tk.Tk()
    again = TrainerApp(reopened)
    assert again.text_size.get() == '特大'
    assert ui_font(reopened).cget('size') == 16
    reopened.update()
    again.close()
'''


class TrainerLayoutTests(unittest.TestCase):
    def test_workbench_reflows_without_losing_drafts_or_font_preference(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'development') as directory:
            result = subprocess.run(
                [sys.executable, '-B', '-X', 'utf8', '-c', SCENARIO], cwd=ROOT,
                env={**os.environ, 'AI_TRAINING_DATA': directory},
                capture_output=True, encoding='utf-8', timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
