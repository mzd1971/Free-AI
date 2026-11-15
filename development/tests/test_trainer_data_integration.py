"""Verify trainer branding, persisted theme and importing a ready dataset without training."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = r'''
import json
from pathlib import Path
import tkinter as tk
from unittest.mock import patch
from paths import ARTIFACTS
from trainer_app import TrainerApp
from ui_kit import ui_font
from training_desktop import FILES

root = tk.Tk()
root.withdraw()
root.tk.call('tk', 'scaling', 2)
with patch('urllib.request.urlopen', side_effect=AssertionError('Startup network')), patch('socket.create_connection', side_effect=AssertionError('Startup network')):
    app = TrainerApp(root)
errors=[]
root.report_callback_exception=lambda kind,error,trace: errors.append(str(error))
root.geometry('1536x900+2500+1400')
root.deiconify()
root.update()
assert app.theme == 'cyberpunk'
assert app.brand_label.cget('text') == 'Free AI'
for name in ('标准','特大'):
    app.text_size.set(name)
    app.change_text_size()
    root.update()
    assert app.text_size_picker.get() == name
    assert app.brand_label.winfo_width() >= ui_font(root,25,bold=True).measure('Free AI')
    assert app.brand_label.winfo_x()+app.brand_label.winfo_width() <= app.sidebar.winfo_width()
    glyph=app.text_size_picker.bbox(0)
    assert glyph and glyph[2]>0 and glyph[3]>0, glyph
assert not app.dataset_panel.winfo_manager()
app.toggle_online_data()
root.update()
assert app.dataset_panel.winfo_manager()
app.toggle_online_data()
assert not app.dataset_panel.winfo_manager()
app.epochs.set('3')
app.theme_choice.set('经典深色')
app.change_theme()
assert app.settings.get('trainer_theme') == 'dark'
assert app.epochs.get() == '3'
assert app.theme == 'cyberpunk', 'theme save must not rebuild and discard work'
target=ARTIFACTS/'datasets'/'fixture'
target.mkdir(parents=True)
for index,name in enumerate(FILES):
    (target/name).write_text(json.dumps({'id':str(index),'prompt':f'问题{index}','response':'回答'},ensure_ascii=False)+'\n',encoding='utf-8')
with patch.object(app,'finetune',side_effect=AssertionError('Import started training')):
    app.imported_dataset({'path':target,'report':target/'source.json'})
assert Path(app.dataset.get())==target
assert '已导入并检查' in app.data_notice.get()
assert len(app.pages)==3
assert not errors, errors
app.close()
second=tk.Tk()
again=TrainerApp(second)
assert again.theme=='dark'
assert again.text_size_picker.get()=='特大'
assert Path(again.dataset.get()) == target
again.close()
'''


class TrainerDataIntegrationTests(unittest.TestCase):
    def test_native_brand_theme_and_data_handoff(self):
        with tempfile.TemporaryDirectory(prefix='trainer-data-', dir=ROOT/'development') as scratch:
            result=subprocess.run([str(ROOT/'runtime/python/python.exe'),'-E','-s','-B','-X','utf8','-c',SCENARIO],
                cwd=ROOT,env=dict(os.environ,AI_TRAINING_DATA=scratch),capture_output=True,text=True,encoding='utf-8',
                timeout=40,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
