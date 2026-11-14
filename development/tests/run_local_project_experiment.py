"""Run the project agent against an installed local model and a disposable fixture."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
PYTHON = (ROOT / 'runtime' / 'python' / 'python.exe').resolve()
MODEL_ID = 'text-default'
CONTEXT = 16384
MAX_STEPS = 16
TEST_ARGS = ('-B', '-m', 'unittest', 'discover', '-s', '.', '-v')

PRICING = '''"""Pricing helper."""

def total_price(price, quantity):
    return price + quantity
'''
TESTS = '''import unittest

from pricing import total_price


class PricingTests(unittest.TestCase):
    def test_multiplies_price_and_quantity(self):
        self.assertEqual(total_price(7, 3), 21)

    def test_supports_fractional_unit_price(self):
        self.assertEqual(total_price(2.5, 3), 7.5)

    def test_zero_quantity_returns_zero(self):
        self.assertEqual(total_price(12, 0), 0)

    def test_rejects_negative_price(self):
        with self.assertRaises(ValueError):
            total_price(-2, 3)

    def test_rejects_negative_quantity(self):
        with self.assertRaises(ValueError):
            total_price(2, -3)
'''
README = '''# Pricing

Run the tests from this folder with the FreeAI bundled Python executable.

    python.exe -B -m unittest discover -s . -v
'''
FIXTURE = {'pricing.py': PRICING, 'test_pricing.py': TESTS, 'README.md': README}
TASK = (
    '这是一个小型 Python 项目。先按 README 的命令运行测试，查看真实失败。'
    '仅修改 pricing.py 来修复测试所揭示的问题，保留测试和 README 原样，不创建其他文件。'
    '修改后用同一条命令再次运行测试，最后概括实际修改和测试结果。'
    f'每次运行测试均使用工作区根目录作为 cwd，并使用这些 argv 项：{[str(PYTHON), *TEST_ARGS]!r}。'
)
SCOPE = '结论仅适用于本次单一小项目，不能由此推断模型的一般项目处理能力。'


def decode(data):
    return data.decode('utf-8', errors='replace') if data is not None else ''


def run_tests(project):
    command = [str(PYTHON), *TEST_ARGS]
    try:
        result = subprocess.run(command, cwd=project, capture_output=True, timeout=30, check=False)
        return {'argv': command, 'cwd': str(project), 'status': 'exited',
                'returncode': result.returncode, 'stdout': decode(result.stdout),
                'stderr': decode(result.stderr)}
    except subprocess.TimeoutExpired as exc:
        return {'argv': command, 'cwd': str(project), 'status': 'timeout',
                'returncode': None, 'stdout': decode(exc.stdout), 'stderr': decode(exc.stderr)}
    except OSError as exc:
        return {'argv': command, 'cwd': str(project), 'status': 'start_failed',
                'returncode': None, 'stdout': '', 'stderr': str(exc)}


def hashes(project):
    result = {}
    for name in FIXTURE:
        path = project / name
        if path.is_file() and not path.is_symlink():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def verify_test_output(result, *, failed):
    output = result['stdout'] + '\n' + result['stderr']
    if result['status'] != 'exited' or 'Ran 5 tests' not in output:
        return False
    if failed:
        return result['returncode'] == 1 and 'FAILED (failures=5)' in output
    return result['returncode'] == 0 and output.rstrip().endswith('\nOK')


class Experiment:
    def __init__(self, folder):
        self.folder = folder
        self.project = folder / 'project'
        self.tool_counts = Counter()
        self.command_attempts = []
        self.test_results = []
        self.changed = []
        self.trace_index = 0

    def unexpected_files(self):
        return sorted(
            str(path.relative_to(self.project)) for path in self.project.iterdir()
            if path.name not in FIXTURE or not path.is_file() or path.is_symlink()
        )

    def approve_command(self, request):
        argv = request.get('argv', [])
        try:
            approved = (
                Path(request['executable']).resolve(strict=True) == PYTHON
                and Path(request['cwd']).resolve(strict=True) == self.project
                and tuple(argv[1:]) == TEST_ARGS
                and not self.unexpected_files()
            )
        except (KeyError, OSError, TypeError, ValueError):
            approved = False
        self.command_attempts.append({'approved': approved})
        if approved:
            print('批准运行原始测试命令。', flush=True)
        else:
            print('命令已拒绝。任务中给出了可批准的完整测试参数。', flush=True)
        return approved

    def on_event(self, event):
        kind = event.get('type')
        if kind != 'tool_result':
            if kind in ('start', 'finish', 'plan'):
                print(event['message'], flush=True)
            return
        self.trace_index += 1
        tool = event['tool']
        result = event['result']
        self.tool_counts[tool] += 1
        summary = '已返回' if result['ok'] else '未成功'
        if tool == 'run_command':
            command = {key: result[key] for key in ('status', 'returncode', 'stdout', 'stderr')
                       if key in result}
            command['status'] = command.get('status', 'rejected')
            command['event_index'] = self.trace_index
            self.test_results.append(command)
            summary = command['status'] + f'，退出码 {command.get("returncode")}'
        elif tool in ('write_file', 'replace_text') and result.get('changed'):
            self.changed.append({'tool': tool, 'path': result['path'], 'event_index': self.trace_index})
            summary = f'已修改 {result["path"]}'
        print(f'[{self.trace_index}] {tool} · {summary}', flush=True)

    def test_sequence_passes(self):
        observed = [item for item in self.test_results if item['status'] == 'exited']
        if not self.changed or not observed:
            return False
        first_edit = min(item['event_index'] for item in self.changed)
        last_edit = max(item['event_index'] for item in self.changed)
        return (any(item['event_index'] < first_edit and verify_test_output(item, failed=True)
                    for item in observed)
                and any(item['event_index'] > last_edit and verify_test_output(item, failed=False)
                        for item in observed))

    def evaluate(self, report):
        final_hashes = hashes(self.project)
        before = report['original_hashes']
        report['final_hashes'] = final_hashes
        report['extra_entries'] = self.unexpected_files()
        changes = report['project_changes']
        report['checks'] = {
            'baseline_fails': bool(report.get('baseline')) and verify_test_output(report['baseline'], failed=True),
            'final_passes': verify_test_output(report['final'], failed=False),
            'pricing_changed': ('pricing.py' in final_hashes
                                and final_hashes['pricing.py'] != before['pricing.py']),
            'test_and_readme_unchanged': all(final_hashes.get(name) == before[name]
                                             for name in ('test_pricing.py', 'README.md')),
            'no_extra_entries': not report['extra_entries'],
            'tracked_pricing_edit_only': bool(self.changed) and bool(changes)
            and {item['path'] for item in [*self.changed, *changes]} == {'pricing.py'},
            'agent_ran_tests_before_and_after_edit': self.test_sequence_passes(),
            'agent_finished': report['agent_status'] == 'completed',
        }
        report['success'] = all(report['checks'].values())


def engine_info(runtime):
    if runtime is None:
        return {}
    signature = getattr(runtime, '_signature', None)
    catalog = getattr(runtime.manager, 'catalog', {})
    spec = catalog.get(MODEL_ID, {})
    return {'backend': 'llama.cpp', 'model_id': MODEL_ID,
            'model': spec.get('short_name') or spec.get('title'),
            'model_file': Path(signature[3]).name if signature else spec.get('entrypoint'),
            'engine_package': signature[2] if signature else None,
            'engine_notice': runtime.last_engine_notice, 'context': CONTEXT}


def save_report(folder, report):
    (folder / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
    )
    status = '通过' if report['success'] else '未通过'
    checks = '\n'.join(f'- {name} · {"通过" if passed else "未通过"}'
                       for name, passed in report.get('checks', {}).items())
    model = report.get('model_engine', {})
    summary = [
        '# 本地项目助手实验', '',
        f'结果为**{status}**，耗时 {report["elapsed_seconds"]:.1f} 秒。',
        f'模型为 {model.get("model") or MODEL_ID}，执行组件为 {model.get("engine_package") or "未启动"}。',
        f'项目助手状态为 {report["agent_status"]}。',
        f'运行记录位于 `{report["run_dir"] or "未生成"}`。',
    ]
    if report.get('error'):
        summary.append(f'执行异常类型为 `{report["error"]}`。')
    summary += ['', f'工具调用次数为 {sum(report["tool_counts"].values())}。']
    for tool, count in report['tool_counts'].items():
        summary.append(f'- `{tool}` {count} 次')
    if checks:
        summary += ['', checks]
    summary += ['', SCOPE, '']
    for label, key in [('修改前独立测试', 'baseline'), ('修改后独立测试', 'final')]:
        result = report.get(key)
        if result is None:
            continue
        summary += [
            f'## {label}', '', f'退出码为 `{result["returncode"]}`。', '',
            '标准输出', '', '```text', result['stdout'].rstrip(), '```', '',
            '标准错误', '', '```text', result['stderr'].rstrip(), '```', '',
        ]
    (folder / 'report.md').write_text('\n'.join(summary), encoding='utf-8')


def main():
    folder = ROOT / 'development' / 'experiments' / f'local-project-{uuid.uuid4().hex}'
    experiment = Experiment(folder)
    experiment.project.mkdir(parents=True)
    print(f'实验目录 {folder}', flush=True)
    for name, contents in FIXTURE.items():
        (experiment.project / name).write_text(contents, encoding='utf-8')
    report = {
        'success': False, 'scope': SCOPE, 'agent_status': 'not_started', 'run_dir': '',
        'project_changes': [], 'original_hashes': hashes(experiment.project),
    }
    runtime = None
    agent = None
    shutdown = None
    started = time.monotonic()
    try:
        report['baseline'] = run_tests(experiment.project)
        print(f'原始测试退出码 {report["baseline"]["returncode"]}', flush=True)
        if verify_test_output(report['baseline'], failed=True):
            from local_project_model import LocalProjectModel
            from project_agent import ProjectAgent
            from qwen_chat import runtime as shared_runtime, shutdown

            runtime = shared_runtime()
            model = LocalProjectModel(model_id=MODEL_ID, max_output_tokens=2048)
            agent = ProjectAgent(
                experiment.project, model, runs_root=folder / 'runs',
                approve_command=experiment.approve_command, on_event=experiment.on_event,
                context=CONTEXT, max_steps=MAX_STEPS,
            )
            result = agent.run(TASK)
            report['agent_status'] = result['status']
            report['agent_answer'] = result['answer']
            report['run_dir'] = result['run_dir']
            report['project_changes'] = result['changes']
    except (Exception, KeyboardInterrupt) as exc:
        report['error'] = type(exc).__name__
        report['agent_status'] = 'failed'
        if agent is not None and agent.run_dir is not None:
            report['run_dir'] = str(agent.run_dir)
            report['project_changes'] = agent.changes
    finally:
        try:
            report['model_engine'] = engine_info(runtime)
        except Exception as exc:
            report['engine_info_error'] = type(exc).__name__
        if shutdown is not None:
            try:
                shutdown()
            except Exception as exc:
                report['shutdown_error'] = type(exc).__name__
        report['tool_counts'] = dict(sorted(experiment.tool_counts.items()))
        report['agent_test_commands'] = experiment.test_results
        report['approval_decisions'] = experiment.command_attempts
        report['edit_events'] = experiment.changed
        final_source = experiment.project / 'pricing.py'
        verified = folder / 'verification'
        verified.mkdir()
        (verified / 'test_pricing.py').write_text(TESTS, encoding='utf-8')
        if final_source.is_file() and not final_source.is_symlink():
            (verified / 'pricing.py').write_bytes(final_source.read_bytes())
        report['final'] = run_tests(verified)
        experiment.evaluate(report)
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        save_report(folder, report)
    print(f'实验{"通过" if report["success"] else "未通过"}，报告 {folder / "report.json"}', flush=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
