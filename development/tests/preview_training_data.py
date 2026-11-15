"""Exercise the trainer's real HF import in isolated local data, without training."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
parser=argparse.ArgumentParser()
parser.add_argument('--mode', choices=('theme','dataset','links','selection','fuzzy'), default='theme')
parser.add_argument('--theme', default='cyberpunk')
parser.add_argument('--size', default='large')
parser.add_argument('--rows', type=int, default=30)
parser.add_argument('--query', default='alpca')
parser.add_argument('--target', default='yahma/alpaca-cleaned')
args=parser.parse_args()
folder=ROOT/'development/tmp'/('training-data-'+uuid4().hex)
folder.mkdir(parents=True)
os.environ['AI_TRAINING_DATA']=str(folder/'data')
sys.path.insert(0,str(ROOT))
import tkinter as tk
from app_state import Sessions
from paths import ARTIFACTS
from ui_preferences import save_text_size
from trainer_app import TrainerApp
from training_desktop import inspect_data

Sessions(ARTIFACTS).set('client_theme',args.theme)
save_text_size(args.size)
root=tk.Tk()
app=TrainerApp(root)
root.state('zoomed')
root.attributes('-topmost',True)
errors=[]
root.report_callback_exception=lambda kind,error,trace: errors.append(str(error))
started=time.monotonic()
phase='search'
result=None

def ready(extra=None):
    root.update_idletasks()
    record={'pid':os.getpid(),'handle':int(str(root.tk.call('wm','frame',root._w)),0),'mode':args.mode,
            'folder':str(folder),'theme':app.theme,'brand':app.brand_label.cget('text'),
            'brand_fits':app.brand_label.winfo_x()+app.brand_label.winfo_reqwidth()<=app.sidebar.winfo_width(),
            'font_choice':app.text_size_picker.get(),'elapsed':round(time.monotonic()-started,3),
            'errors':errors,**(extra or {})}
    text=json.dumps(record,ensure_ascii=False,indent=2,default=str)
    (folder/'report.json').write_text(text,encoding='utf-8')
    (ROOT/'development/tmp/training-data-window.json').write_text(text,encoding='utf-8')

def capture_import(value):
    global result
    app.imported_dataset(value)
    result=value

def progress():
    global phase
    panel=app.dataset_panel
    if time.monotonic()-started>35:
        ready({'success':False,'phase':phase,'status':panel.status.get(),'search':panel.search_message.get()})
        return
    if not app.busy and not panel._pending:
        target=args.target if args.mode=='fuzzy' else 'yahma/alpaca-cleaned'
        if phase=='search' and target in panel._repositories and panel.search_message.get().startswith('找到'):
            phase='preview'
            panel.repository_tree.selection_set(target)
            panel.repository_tree.event_generate('<<TreeviewSelect>>')
        elif phase=='preview' and panel._preview_source:
            if args.mode=='fuzzy':
                root.update_idletasks()
                height=app.train_scroll.body.winfo_reqheight()
                top=panel.winfo_rooty()-app.train_scroll.body.winfo_rooty()
                app.train_scroll.canvas.yview_moveto(max(0,top/height))
                ready({'success':True,'query':args.query,'target':target,
                       'results':list(panel._repositories),'preview_source':panel._preview_source,
                       'download_started':False,'training_started':False})
                return
            phase='import'
            panel.import_count.set(str(args.rows))
            panel.import_button.invoke()
        elif phase=='import' and result:
            counts=inspect_data(result['path'])
            root.update_idletasks()
            height=app.train_scroll.body.winfo_reqheight()
            top=panel.winfo_rooty()-app.train_scroll.body.winfo_rooty()
            app.train_scroll.canvas.yview_moveto(max(0,top/height))
            ready({'success':True,'result':result,'validated_counts':counts,
                   'source':panel._source['id'],
                   'selected_dataset':app.dataset.get(),'training_started':app.current_job=='training'})
            return
    root.after(100,progress)

def start():
    global phase
    if args.mode=='theme':
        ready()
        return
    app.toggle_online_data()
    if args.mode=='links':
        from unittest.mock import patch
        panel=app.dataset_panel
        root.update_idletasks()
        height=app.train_scroll.body.winfo_reqheight()
        top=panel.winfo_rooty()-app.train_scroll.body.winfo_rooty()
        app.train_scroll.canvas.yview_moveto(max(0,(top-12)/height))
        root.update()
        with patch('hf_dataset_panel.webbrowser.open') as opened:
            panel.website_button.invoke()
            opened.assert_called_once_with('https://huggingface.co/datasets')
        assert not app.busy
        ready({'recommendations':list(panel.query_entry.cget('values')),'network_task_started':False})
        return
    app.dataset_panel.on_import=capture_import
    if args.mode=='selection':
        phase='preview'
        app.dataset_panel.query_entry.current(0)
        app.dataset_panel.query_entry.event_generate('<<ComboboxSelected>>')
    else:
        app.dataset_panel.query.set(args.query if args.mode=='fuzzy' else 'alpaca-cleaned')
        app.dataset_panel.search()
    progress()

root.after(400,start)
root.after(55000,app.close)
root.mainloop()
