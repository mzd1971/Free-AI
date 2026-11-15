"""Preview settings with isolated data under FreeAI; no model or API starts."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument('--category', default='general')
parser.add_argument('--geometry', default='1120x820+20+20')
parser.add_argument('--theme', default='cyberpunk')
args = parser.parse_args()
folder = ROOT / 'development/tmp' / ('settings-preview-' + uuid.uuid4().hex)
folder.mkdir(parents=True)
os.environ['AI_TRAINING_DATA'] = str(folder / 'data')
sys.path.insert(0, str(ROOT))

import tkinter as tk
from app_state import Sessions
from client_app import ClientApp
from paths import ARTIFACTS

Sessions(ARTIFACTS).set('client_theme', args.theme)
root = tk.Tk()
app = ClientApp(root)
app.select_page(2)
app.settings_panel.select_category(args.category)
root.title('Free AI · 设置预览')
root.geometry(args.geometry)
root.attributes('-topmost', True)

def ready():
    root.update_idletasks()
    handle = int(str(root.tk.call('wm', 'frame', root._w)), 0)
    (ROOT / 'development/tmp/settings-preview-window.json').write_text(
        json.dumps({'handle': handle, 'pid': os.getpid(), 'folder': str(folder), 'category': args.category}), encoding='utf-8')
    print(handle, flush=True)

root.after(400, ready)
root.after(45000, app.close)
root.mainloop()
