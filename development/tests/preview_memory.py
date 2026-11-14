"""Preview real memory experiment records beside the native conversation."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
folder = Path(sys.argv[1]).resolve()
folder.relative_to(ROOT / 'development/experiments')
report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
os.environ['AI_TRAINING_DATA'] = str(folder / 'data')
sys.path.insert(0, str(ROOT))
import tkinter as tk
from client_app import ClientApp
from capability_manager import PackageManager

root = tk.Tk()
app = ClientApp(root)
app.manager = PackageManager(root=ROOT / 'models')
app.load_conversation(report['session_ids'][0])
app.backend, app.selected_model = 'local', 'text-default'
app.refresh_model_state()
root.title('Free AI · 长期记忆预览')
root.geometry('1040x760+20+20' if '--narrow' in sys.argv else '1440x900+20+20')
root.update_idletasks()
app.open_memory()

def show():
    panel = app.memories
    for index, row in enumerate(panel.rows):
        if row['source'] == 'agent':
            panel.listbox.selection_clear(0, 'end')
            panel.listbox.selection_set(index)
            panel.select()
            break
    app.status.set('已打开本地模型实测记录；此预览不执行任务。')
    if '--home' in sys.argv:
        app.memories.toggle()
        app.new_conversation()
        app.status.set('边缘行者皮肤预览 · 黑底 / 电青 / 荧光绿')
    if '--settings' in sys.argv:
        app.select_page(2)
    app.transcript.see('end')
    root.lift()
    root.attributes('-topmost', True)
    root.after(1500, lambda: root.attributes('-topmost', False))
    (ROOT / 'development/tmp/memory-preview-window.txt').write_text(str(root.winfo_id()))

root.after(500, show)
root.after(55000, app.close)
root.mainloop()
