"""Live local model + public web + disposable project; no scripted model replies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='text-default')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / 'workspace'
    workspace.mkdir()
    os.environ['AI_TRAINING_DATA'] = str(output / 'data')
    source = workspace / 'numbers.py'
    source.write_text('def magnitude(value):\n    return value\n', encoding='utf-8')
    tests = workspace / 'test_numbers.py'
    tests.write_text('''import unittest
from numbers import magnitude
class Tests(unittest.TestCase):
    def test_negative(self): self.assertEqual(magnitude(-7), 7)
    def test_positive(self): self.assertEqual(magnitude(5), 5)
    def test_zero(self): self.assertEqual(magnitude(0), 0)
''', encoding='utf-8')
    tests_hash = hashlib.sha256(tests.read_bytes()).hexdigest()
    command = [sys.executable, '-E', '-s', '-B', '-m', 'unittest', '-v', 'test_numbers']

    def check():
        run = subprocess.run(command, cwd=workspace, capture_output=True, text=True,
                             encoding='utf-8', timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return {'returncode': run.returncode, 'stdout': run.stdout, 'stderr': run.stderr}

    from capability_manager import PackageManager
    from local_runtime import LocalRuntime
    from local_project_model import LocalProjectModel
    from project_agent import ProjectAgent

    events = []
    def event(row):
        events.append(row)
        if row['type'] in ('tool_result', 'finish'):
            print(json.dumps({'type': row['type'], 'tool': row.get('tool'),
                              'ok': row.get('result', {}).get('ok'), 'message': row.get('message')}, ensure_ascii=False), flush=True)
    manager = PackageManager(root=ROOT / 'models')
    runtime = LocalRuntime(manager=manager, request_timeout=120)
    start = time.monotonic()
    report = {'model': args.model, 'before': check(), 'verified': False,
              'scope': 'Real local GGUF, real public network and real isolated test process.'}
    try:
        agent = ProjectAgent(workspace, LocalProjectModel(runtime=runtime, model_id=args.model),
                             runs_root=output / 'runs', approve_command=lambda request: True,
                             permission_mode='automatic', allow_web=True, integrated_tools=True,
                             on_event=event, context=16384)
        task = ('先联网搜索 Python 官方 abs 函数文档（web_search），再读取官方网页 '
                'https://docs.python.org/3/library/functions.html （web_fetch）。'
                '阅读项目 numbers.py，修复 magnitude 让它返回绝对值，负数、正数、0 均正确。'
                '不要改 test_numbers.py。创建 SOURCES.md 用中文简述查到的依据并列出实际来源URL。'
                '最后运行检查命令（purpose=check）' + json.dumps(command, ensure_ascii=False)
                + '。必须真正调用联网工具，不要假称查过。')
        report['result'] = agent.run(task)
        report['after'] = check()
        web = [row for row in events if row['type'] == 'tool_result' and row.get('tool') in ('web_search', 'web_fetch')]
        report['web_results'] = web
        report['tests_unchanged'] = hashlib.sha256(tests.read_bytes()).hexdigest() == tests_hash
        sources = workspace / 'SOURCES.md'
        report['sources_written'] = sources.is_file() and 'https://' in sources.read_text(encoding='utf-8')
        report['verified'] = (report['before']['returncode'] != 0 and report['after']['returncode'] == 0
            and report['tests_unchanged'] and report['sources_written']
            and report['result']['status'] == 'completed'
            and all(any(row.get('tool') == tool and row['result'].get('ok') for row in web)
                    for tool in ('web_search', 'web_fetch')))
    finally:
        runtime.shutdown()
        report['elapsed_seconds'] = round(time.monotonic() - start, 2)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'verified': report['verified'], 'report': str(output / 'report.json')}, ensure_ascii=False))
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
