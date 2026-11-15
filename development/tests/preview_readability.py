"""Isolated native layout and live model-list preview; never downloads weights."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument('--mode', choices=('chat', 'home', 'hf', 'trainer'), default='chat')
parser.add_argument('--size', default='large')
parser.add_argument('--geometry', default='1560x900+20+20')
parser.add_argument('--maximize', action='store_true')
args = parser.parse_args()
folder = ROOT/'development/tmp'/('readability-' + uuid.uuid4().hex)
folder.mkdir(parents=True)
os.environ['AI_TRAINING_DATA'] = str(folder/'data')
sys.path.insert(0, str(ROOT))
from ui_preferences import save_text_size
save_text_size(args.size)
import tkinter as tk

root = tk.Tk()
if args.mode == 'trainer':
    from trainer_app import TrainerApp
    app = TrainerApp(root)
else:
    from client_app import ClientApp
    app = ClientApp(root)
    if args.mode == 'chat':
        app.history = [{'role': 'user', 'content': '怎样开始修改一个项目？'},
                       {'role': 'assistant', 'content': '先选择项目文件夹，告诉我希望完成的目标。我会查看相关文件，修改后运行测试，再根据测试结果继续修复。\n\n### 你可以随时查看进展\n\n- 在同一段对话中查看文件修改和命令结果。\n- 遇到问题时停止，之后继续任务。\n- 在设置中选择只读、逐条确认或自动执行。\n\n**先从一个小目标开始**，例如修复一条失败的测试。'}]
        app.write(app.transcript, '')
        app.append_heading('你', 'user')
        app.write(app.transcript, app.history[0]['content'] + '\n\n', True)
        app.append_heading('Free AI', 'assistant')
        app.write(app.transcript, app.history[-1]['content'] + '\n', True)
        app.format_transcript()
        app.layout_chat()
root.geometry(args.geometry)
if args.maximize:
    root.state('zoomed')
root.attributes('-topmost', True)
started = time.monotonic()
phase = 'search'
errors = []
root.report_callback_exception = lambda kind, error, traceback: errors.append(str(error))

def ready(extra=None):
    root.update_idletasks()
    record = {'pid': os.getpid(), 'handle': int(str(root.tk.call('wm', 'frame', root._w)), 0),
              'folder': str(folder), 'mode': args.mode, 'text_size': args.size, 'errors': errors,
              'elapsed': round(time.monotonic()-started, 3), **(extra or {})}
    (ROOT/'development/tmp/readability-window.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    (folder/'report.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False), flush=True)

def check_search():
    global phase
    panel = app.hf_model_panel
    if time.monotonic()-started > 35:
        ready({'search_ok': False, 'message': panel.search_message.get(), 'files_message': panel.files_message.get()})
        return
    if not app.busy and not panel._pending:
        if phase == 'search' and panel._repositories:
            repository = next((repo for repo in panel._repositories if repo == 'Qwen/Qwen2.5-Coder-1.5B-GGUF'), next(iter(panel._repositories)))
            phase = 'files'
            panel.repository_tree.selection_set(repository)
            panel.repository_tree.event_generate('<<TreeviewSelect>>')
        elif phase == 'files' and panel._files:
            selected = next((key for key, row in panel._files.items() if row['available'] and 'q4_k_m' in row['filename'].lower()), next(iter(panel._files)))
            panel.file_tree.selection_set(selected)
            panel.file_tree.event_generate('<<TreeviewSelect>>')
            ready({'search_ok': True, 'repository': panel._repo_id, 'repositories': len(panel._repositories),
                   'files': list(panel._files.values()), 'download_started': bool(app.downloading),
                   'model_selected': app.selected_model})
            return
    root.after(100, check_search)

def begin():
    if args.mode == 'hf':
        app.show_capabilities('hf-models')
        app.hf_model_panel.query.set('Qwen2.5-Coder-1.5B-GGUF')
        app.hf_model_panel.search()
        check_search()
    else:
        ready()

root.after(400, begin)
root.after(45000, app.close)
root.mainloop()
