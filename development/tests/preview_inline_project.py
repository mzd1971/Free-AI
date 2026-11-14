"""Display a saved local-model experiment in the native client; never run its tasks."""
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
app.backend, app.selected_model = 'local', 'text-default'
app.load_conversation(report['session_id'])
app.refresh_capabilities()
app.input.insert('1.0', '继续检查这个项目的边界情况。')
app.update_placeholder()
root.title('Free AI · 项目对话预览')
if '--narrow' in sys.argv:
    root.geometry('940x690+32+28')
root.update_idletasks()
root.lift()
root.attributes('-topmost', True)
root.after(1500, lambda: root.attributes('-topmost', False))

def show():
    if '--approval' in sys.argv:
        import threading
        from project_chat import ActivityCard
        app.transcript.configure(state='normal')
        app.transcript.delete('1.0', 'end')
        app.transcript.configure(state='disabled')
        app.append_heading('你', 'user')
        app.write(app.transcript, '检查刚才修改的代码并运行测试。\n\n', True)
        app.append_heading('Free AI', 'assistant')
        card = ActivityCard(app.project, str(folder / 'project'))
        app.project.cards.append(card)
        app.project.active = card
        card.show_approval({'ready': threading.Event(), 'allowed': False, 'details': {
            'executable': str(ROOT / 'runtime/python/python.exe'), 'cwd': str(folder / 'project'),
            'argv': [str(ROOT / 'runtime/python/python.exe'), '-B', '-m', 'unittest', 'discover', '-v'],
            'timeout': 60}})
        app.status.set('命令确认布局预览：不会执行任何命令。')
        app.transcript.see('end')
    else:
        app.transcript.yview(app.transcript.search('补上刚才约定', '1.0') or '1.0')
        app.status.set('已恢复本地模型的两轮实测记录，可以在同一聊天中继续。')
    (ROOT / 'development/tmp/inline-preview-window.txt').write_text(str(root.winfo_id()))

root.after(500, show)
root.after(50000, app.close)
root.mainloop()
