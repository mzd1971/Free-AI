"""Opt-in native UI experiment with an installed local model, never scripted replies."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BROKEN = '''def clean_names(values):
    """Strip blanks; ignore empty names; deduplicate case-insensitively in input order."""
    return sorted(set(value.strip().lower() for value in values))
'''
TESTS = '''import unittest
from names import clean_names

class NamesTests(unittest.TestCase):
    def test_order_and_spelling(self):
        self.assertEqual(clean_names([' Bob ', 'Alice', 'BOB', 'alice', 'Carol']), ['Bob', 'Alice', 'Carol'])
    def test_blank_names(self):
        self.assertEqual(clean_names(['', '  ', ' Alice ', '']), ['Alice'])
    def test_empty(self):
        self.assertEqual(clean_names([]), [])
    def test_unicode(self):
        self.assertEqual(clean_names([' 小明 ', '小红', '小明']), ['小明', '小红'])
'''

FOLLOWUP_TESTS = '''
    def test_sensitive_keeps_case(self):
        self.assertEqual(clean_names([' Bob ', 'bob', 'Bob'], case_sensitive=True), ['Bob', 'bob'])
    def test_sensitive_removes_exact_duplicates(self):
        self.assertEqual(clean_names([' Alice ', 'Alice', 'alice', ''], case_sensitive=True), ['Alice', 'alice'])
    def test_explicit_false_keeps_original_behavior(self):
        self.assertEqual(clean_names([' Bob ', 'BOB', 'bob'], case_sensitive=False), ['Bob'])
'''


def wait_for_work(root, app, runtime, report, timeout):
    """Observe real UI approvals/results without supplying any model replies."""
    deadline = time.monotonic() + timeout
    last_state = idle_since = None
    while time.monotonic() < deadline:
        root.update()
        assert not report['ui_errors'], report['ui_errors']
        card = app.project.active
        if card and card.request:
            details = card.request.get('details', {})
            report['approvals'].append(details)
            card.decide(True)
        state = (len(app.project.cards), app.busy, app.status.get())
        if state != last_state:
            print(json.dumps({'cards': state[0], 'busy': state[1], 'status': state[2]}, ensure_ascii=False), flush=True)
            last_state = state
        if not app.busy:
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since > 1.5:
                return True
        else:
            idle_since = None
        time.sleep(.02)
    app.generation_cancel.set()
    runtime.shutdown()
    deadline = time.monotonic() + 10
    while app.busy and time.monotonic() < deadline:
        root.update()
        time.sleep(.05)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--models', type=Path, default=ROOT / 'models')
    parser.add_argument('--model', default='text-qwen3-0-6b')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--explicit-paths', action='store_true', help='Give file-tool path examples; record this extra guidance.')
    parser.add_argument('--follow-up', action='store_true', help='After success, request a new feature in the same reopened chat.')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / 'workspace'
    workspace.mkdir()
    source, tests = workspace / 'names.py', workspace / 'test_names.py'
    source.write_text(BROKEN, encoding='utf-8')
    tests.write_text(TESTS, encoding='utf-8')
    test_hash = hashlib.sha256(tests.read_bytes()).hexdigest()
    command = [sys.executable, '-E', '-s', '-B', '-X', 'utf8', '-m', 'unittest', '-v', 'test_names']
    (workspace / 'README.md').write_text(
        '这是一个名字清洗小项目。只修复 names.py，保留测试文件。\n'
        'clean_names 接收字符串列表：去掉首尾空白，忽略空名称，不分大小写去重，'
        '保持首次出现的拼写及顺序。输入为空时返回空列表。\n'
        '测试命令参数数组：\n' + json.dumps(command, ensure_ascii=False) + '\n', encoding='utf-8')
    os.environ['AI_TRAINING_DATA'] = str(output / 'data')
    os.environ['TEMP'] = os.environ['TMP'] = str(output / 'temp')
    (output / 'temp').mkdir()
    report = {'model': args.model, 'explicit_paths': args.explicit_paths,
              'scope': 'Actual local GGUF through native ClientApp. No scripted model replies.',
              'completion_criteria': 'Source changed, tests unchanged, independent checks pass, final task completed and all cards restored.',
              'verified': False, 'ui_errors': [], 'approvals': [], 'cards': []}

    def check():
        result = subprocess.run(command, cwd=workspace, capture_output=True, encoding='utf-8',
                                timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}

    report['before'] = check()
    assert report['before']['returncode'] != 0, 'Broken baseline must fail.'
    import tkinter as tk
    from unittest.mock import patch
    from client_app import ClientApp
    from capability_manager import PackageManager
    from local_runtime import LocalRuntime
    from project_checks import save_project_check
    import qwen_chat

    manager = PackageManager(root=args.models.resolve())
    assert manager.ready(args.model) and manager.ready('engine-cpu'), 'Install model and engine first.'
    runtime = LocalRuntime(manager=manager, request_timeout=120)
    qwen_chat._runtime = runtime
    root = tk.Tk()
    root.withdraw()
    app = None
    started = time.monotonic()
    try:
        root.report_callback_exception = lambda kind, error, tb: report['ui_errors'].append(
            ''.join(traceback.format_exception(kind, error, tb)))
        app = ClientApp(root)
        app.manager = manager
        app.backend, app.selected_model = 'local', args.model
        app.context.set('阅读长文 · 16K')
        app.open_project_assistant()
        with patch('project_chat.filedialog.askdirectory', return_value=str(workspace)):
            app.project.choose_workspace()
        save_project_check(app.sessions, workspace, json.dumps(command))
        prompt = ('请修复这个项目的 names.py，让 README.md 中 clean_names 的要求都满足。'
                  '先读取 README.md 和源代码，运行测试了解错误，再实际修改源代码并检查。'
                  '不要修改测试文件，不要只给建议。程序会按已保存的固定检查验证结果。')
        if args.explicit_paths:
            prompt += ('文件工具的 path 参数仅填相对文件名，例如 README.md、names.py、test_names.py。'
                       '不要填写工作区完整路径或盘符。请先调用 read_file(path="names.py")。')
        report['prompt'] = prompt
        app.input.insert('1.0', prompt)
        app.send_chat()
        # This explicitly invoked experiment authorizes commands in its disposable workspace.
        report['timeout'] = not wait_for_work(root, app, runtime, report, args.timeout)
        report['initial_elapsed_seconds'] = round(time.monotonic() - started, 2)
        report['engine'] = runtime.last_engine_notice
        report['cards'] = [{'result': card.result, 'activity': card.activity} for card in app.project.cards]
        report['after'] = check()
        report['tests_unchanged'] = hashlib.sha256(tests.read_bytes()).hexdigest() == test_hash
        report['source_changed'] = source.read_text(encoding='utf-8') != BROKEN
        report['source_after'] = source.read_text(encoding='utf-8')
        identity = app.session_id
        if not app.busy:
            app.new_conversation()
            app.load_conversation(identity)
            report['reopened_cards'] = len(app.project.cards)
        report['verified'] = bool(report['after']['returncode'] == 0 and report['tests_unchanged']
                                  and report['source_changed'] and not report.get('timeout')
                                  and report['cards'] and report['cards'][-1]['result'].get('fixed_check', {}).get('status') == 'passed'
                                  and report['cards'][-1]['result'].get('status') == 'completed'
                                  and report.get('reopened_cards') == len(report['cards']))
        if args.follow_up and report['verified']:
            # New acceptance tests are supplied by this experiment, not by the model.
            tests.write_text(TESTS + FOLLOWUP_TESTS, encoding='utf-8')
            followup_hash = hashlib.sha256(tests.read_bytes()).hexdigest()
            before_source = source.read_bytes()
            followup = {'before': check()}
            report['followup'] = followup
            assert followup['before']['returncode'] != 0, 'The new feature must be absent before this turn.'
            prompt = ('接着做这个项目：给 clean_names 新增可选参数 case_sensitive=False。'
                      '默认保持原来的不分大小写去重；为 True 时只合并拼写大小写完全相同的名字。'
                      '继续保持去首尾空白、忽略空名字和首次出现顺序。'
                      '我已在 test_names.py 添加了验收题，请先看实际报错，修改 names.py，再运行检查。'
                      '不要修改测试文件。')
            followup['prompt'] = prompt
            app.input.insert('1.0', prompt)
            app.send_chat()
            followup['timeout'] = not wait_for_work(root, app, runtime, report, args.timeout)
            followup['after'] = check()
            followup['tests_unchanged'] = hashlib.sha256(tests.read_bytes()).hexdigest() == followup_hash
            followup['source_changed'] = source.read_bytes() != before_source
            followup['source_after'] = source.read_text(encoding='utf-8')
            followup['cards'] = [{'result': card.result, 'activity': card.activity} for card in app.project.cards]
            if not app.busy:
                app.new_conversation()
                app.load_conversation(identity)
                followup['reopened_cards'] = len(app.project.cards)
            followup['verified'] = bool(not followup['timeout'] and followup['after']['returncode'] == 0
                and followup['tests_unchanged'] and followup['source_changed'] and followup['cards']
                and followup['cards'][-1]['result'].get('fixed_check', {}).get('status') == 'passed'
                and followup['cards'][-1]['result'].get('status') == 'completed'
                and followup.get('reopened_cards') == len(followup['cards']))
            report['verified'] = followup['verified']
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        if app:
            app.generation_cancel.set()
        runtime.shutdown()
        if app:
            try:
                root.after_cancel(app.poll_id)
            except (AttributeError, tk.TclError):
                pass
        root.destroy()
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'verified': report['verified'], 'report': str(output / 'report.json'),
                          'elapsed_seconds': report['elapsed_seconds'], 'error': report.get('error')}, ensure_ascii=False), flush=True)
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
