"""Open a read-only native preview of a recorded agent experiment."""

import argparse
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event


ROOT = Path(__file__).resolve().parents[2]


def read_events(result, root):
    run = Path(result['run_dir']).resolve()
    if run.parent != root:
        raise ValueError('运行记录必须位于实验的 runs 文件夹中。')
    return [json.loads(line) for line in (run / 'events.jsonl').read_text(encoding='utf-8').splitlines()]


def preview(args, data_dir):
    os.environ['AI_TRAINING_DATA'] = data_dir
    sys.path.insert(0, str(ROOT))
    import tkinter as tk
    from capability_manager import PackageManager
    from client_app import ClientApp
    from project_chat import ActivityCard

    report_path = args.report.resolve(strict=True)
    report = json.loads(report_path.read_text(encoding='utf-8'))
    workspace = str((report_path.parent / 'project').resolve(strict=True))
    runs = (report_path.parent / 'runs').resolve(strict=True)
    first = report['first']
    resumed = report.get('resumed')
    first_events = read_events(first, runs)
    resumed_events = read_events(resumed, runs) if resumed and args.stage != 'first' else []
    if args.stage != 'first' and not resumed_events:
        raise ValueError('实验报告缺少继续任务的运行记录。')
    results = [(first, first_events)]
    if args.stage == 'resumed':
        results.append((resumed, resumed_events))

    root = tk.Tk()
    app = ClientApp(root)
    app.manager = PackageManager(root=ROOT / 'models')
    if app.manager.ready('text-default'):
        app.selected_model = 'text-default'
        app.refresh_capabilities()
    identity = app.sessions.create()
    app.sessions.set_project_settings(identity, {'enabled': True, 'workspace': workspace})
    for index, (result, _) in enumerate(results):
        task = result['task_state']['task'] if index == 0 else '继续任务'
        app.sessions.append(identity, 'user', task)
        app.sessions.append_project_reply(identity, result, workspace, '')
    app.load_conversation(identity)

    # Render recorded events through the same methods the active cards use.
    for card, (result, events) in zip(app.project.cards, results):
        app.project.active = card
        for event in events:
            card.event(event)
        card.finish(result)
        app.project.save_card(card)
    app.project.active = None
    app.load_conversation(identity)

    def preview_only(*args, **kwargs):
        app.status.set('这是只读预览，已保留这次实测的运行记录。')

    app.project.resume = app.project.undo = app.project.send = preview_only
    app.send_chat = preview_only
    app.send_button.configure(command=preview_only)
    if args.stage == 'approval':
        app.sidebar.pack_forget()
        app.show_project_details = False
        app.layout_chat()
        app.append_heading('你', 'user')
        app.write(app.transcript, '继续任务\n\n', True)
        app.append_heading('Free AI', 'assistant')
        card = ActivityCard(app.project, workspace)
        card.result = {'status': 'running', 'answer': '', 'changes': []}
        app.project.cards.append(card)
        app.project.active = card
        for event in resumed_events:
            card.event(event)
            request = event.get('request')
            if event.get('tool') == 'run_command' and request and request.get('purpose') == 'check':
                card.show_approval({'details': request, 'ready': Event(), 'allowed': False})
                break
        if not card.request:
            raise ValueError('继续任务记录没有验证命令的确认事件。')

    root.title('Free AI · 项目续跑预览')
    root.geometry(args.geometry)
    root.attributes('-topmost', True)
    preview_only()

    def ready():
        root.update_idletasks()
        frame = str(app.project.cards[-1].frame)
        for _, name, position in app.transcript.dump('1.0', 'end', window=True):
            if name == frame:
                app.transcript.yview(position)
                break
        args.handle_file.parent.mkdir(parents=True, exist_ok=True)
        # Tk returns the client HWND. The frame command returns the outer window HWND.
        handle = int(str(root.tk.call('wm', 'frame', root._w)), 0)
        args.handle_file.write_text(str(handle), encoding='ascii')
        print(handle, flush=True)

    root.after(350, ready)
    root.after(args.seconds * 1000, root.destroy)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--stage', choices=('first', 'resumed', 'approval'), default='resumed')
    parser.add_argument('--geometry', default='940x640+32+28')
    parser.add_argument('--handle-file', type=Path, default=ROOT / 'development/tmp/agent-workflow-window.txt')
    parser.add_argument('--seconds', type=int, default=55)
    args = parser.parse_args()
    temporary_root = ROOT / 'development/tmp'
    temporary_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='agent-preview-', dir=temporary_root) as data_dir:
        preview(args, data_dir)


if __name__ == '__main__':
    main()
