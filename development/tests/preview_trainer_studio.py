"""Open the real desktop trainer with isolated settings and synthetic run records."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--page', choices=('training', 'records', 'experience'), default='training')
    parser.add_argument('--width', type=int, default=1440)
    parser.add_argument('--height', type=int, default=880)
    parser.add_argument('--text-size', choices=('normal', 'large', 'extra_large'), default='normal')
    parser.add_argument('--theme', choices=('light', 'dark', 'cyberpunk'), default='light')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--seconds', type=int, default=55)
    args = parser.parse_args()

    if sys.platform == 'win32':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    import tkinter as tk

    temporary_root = REPOSITORY / 'development' / 'tmp'
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='trainer-studio-', dir=temporary_root) as folder:
        os.environ['AI_TRAINING_DATA'] = folder
        os.environ['HF_HUB_OFFLINE'] = '1'
        sys.path.insert(0, str(REPOSITORY))

        from app_state import Sessions
        from development.tests.test_training_publish import fixture_run
        from paths import ARTIFACTS, RUNS_ROOT
        from trainer_app import TrainerApp
        from ui_preferences import save_text_size

        Sessions(ARTIFACTS).set('trainer_theme', args.theme)
        save_text_size(args.text_size)
        if args.page == 'records':
            fixture_run(RUNS_ROOT / 'qwen-lora-客服问答练习')
            (RUNS_ROOT / 'qwen-lora-写作风格练习').mkdir()
        root = tk.Tk()
        app = TrainerApp(root)
        root.geometry(f'{args.width}x{args.height}+40+40')
        root.attributes('-topmost', True)
        errors = []
        original_callback_error = root.report_callback_exception

        def callback_error(kind, value, traceback):
            errors.append(f'{kind.__name__}: {value}')
            original_callback_error(kind, value, traceback)

        def prepare_page():
            if args.page == 'records':
                app.latest_result()
            elif args.page == 'experience':
                app.select_page(2)
            root.after(150, report_window)

        def report_window():
            root.update_idletasks()
            winfo_frame = str(root.tk.call('wm', 'frame', root._w))
            report = {
                'pid': os.getpid(),
                'handle': int(winfo_frame, 0),
                'geometry': root.geometry(),
                'page': args.page,
                'theme': app.theme,
                'tk_scaling': float(root.tk.call('tk', 'scaling')),
                'isolated_data': str(ARTIFACTS),
                'text_size': app.text_size.get(),
                'job': app.current_job,
                'errors': errors,
            }
            document = json.dumps(report, ensure_ascii=False, indent=2)
            if args.report:
                args.report.write_text(document, encoding='utf-8')
            print(document, flush=True)

        root.report_callback_exception = callback_error
        root.after(75, prepare_page)
        root.after(args.seconds * 1000, app.close)
        root.mainloop()
        return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
