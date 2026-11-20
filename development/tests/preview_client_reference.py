"""Isolated native client preview; never reads the user's conversation database."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', choices=('home', 'chat', 'compact'), default='home')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--capture-tool', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(dir=ROOT / 'development', prefix='client-preview-') as scratch:
        os.environ['AI_TRAINING_DATA'] = scratch
        sys.path.insert(0, str(ROOT))
        import tkinter as tk
        from client_app import ClientApp
        root = tk.Tk()
        app = ClientApp(root)
        root.title('Free AI · 布局预览')
        if args.state == 'compact':
            root.geometry('940x740+20+20')
            app.text_size_choice.set('特大')
            app.change_text_size()
        else:
            root.state('zoomed')
        root.attributes('-topmost', True)
        if args.state != 'home':
            identity = app.sessions.create()
            app.sessions.append(identity, 'user', '帮我整理下一步的项目计划。')
            app.sessions.append(identity, 'assistant', '可以从一个小目标开始，把每一步的结果保存下来。\n\n'
                '## 这次先做好客户端\n\n'
                '1. **清楚的布局**：左侧找到项目和对话，中间专心查看内容。\n'
                '2. **顺手的输入框**：选好工作区，再描述想完成的事情。\n'
                '3. **能查看的记忆**：常用约定单独保存，之后可以核实和修改。\n\n'
                '做好后，使用真实任务检查效果，再决定下一步。')
            app.load_conversation(identity)
            app.input.insert('1.0', '接下来，帮我检查项目的测试结果。')
            app.project.set_mode(True)

        def ready():
            app.layout_chat()
            root.update_idletasks()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            def rect(widget):
                return [widget.winfo_rootx(), widget.winfo_rooty(), widget.winfo_width(), widget.winfo_height()]
            data = {'hwnd': int(str(root.tk.call('wm', 'frame', root._w)), 0),
                    'root': rect(root), 'sidebar': rect(app.sidebar), 'page': rect(app.pages[0]),
                    'stack': rect(app.chat_stack), 'hero': rect(app.hero), 'composer': rect(app.composer),
                    'context': rect(app.context_bar), 'model': rect(app.model_button), 'send': rect(app.send_button)}
            args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(data), flush=True)
            if args.capture_tool:
                root.update()
                tool = str(args.capture_tool.resolve()).replace("'", "''")
                command = ('Add-Type -TypeDefinition \'using System.Runtime.InteropServices; public static class CaptureDpi { '
                    '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }\'; '
                    '[CaptureDpi]::SetProcessDPIAware() | Out-Null; '
                    f"& '{tool}' -WindowHandle {data['hwnd']} -Mode temp")
                result = subprocess.run(['powershell', '-NoProfile', '-Command', command],
                    capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                print(result.stdout.strip() or result.stderr.strip(), flush=True)
                root.after(200, app.close)
        root.after(700, ready)
        root.after(55000, app.close)
        root.mainloop()


if __name__ == '__main__':
    main()
