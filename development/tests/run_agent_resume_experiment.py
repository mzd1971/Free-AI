"""Run a local-model project repair, stop after the edit, and verify in a new run."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
PYTHON = (ROOT / 'runtime' / 'python' / 'python.exe').resolve()
TEST_ARGS = ['-B', '-m', 'unittest', '-v', 'test_resume_fixture']
COMMAND = [str(PYTHON), *TEST_ARGS]
SOURCE = 'src/charge.py'
BROKEN = b'"""Compute charges for a group of items."""\n\ndef invoice_total(unit_price, units):\n    return unit_price + units\n'
REPAIRED = BROKEN.replace(b'unit_price + units', b'unit_price * units')
FIXTURE = {
    'AGENTS.md': (
        '# 项目约定\n只修改实现文件。用目的为 check 的测试命令记录检查；保持测试和目录约定原样。\n'
    ).encode('utf-8'),
    'src/AGENTS.md': (
        '# src 目录约定\n保留函数签名、文档字符串和空行，只替换导致测试失败的表达式。\n'
    ).encode('utf-8'),
    SOURCE: BROKEN,
    'test_resume_fixture.py': b'''import unittest

from src.charge import invoice_total


class ChargeTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(invoice_total(7, 3), 21)

    def test_fractional_price(self):
        self.assertEqual(invoice_total(2.5, 3), 7.5)

    def test_zero_units(self):
        self.assertEqual(invoice_total(12, 0), 0)
''',
}
TASK = (
    '修复 invoice_total 的金额计算。先用 search_text 找到该函数的实现，再用下面的测试命令记录一次失败。'
    '读取实现和目录规范，只修改导致失败的表达式，保留测试和 AGENTS.md 原样。修复后必须运行同一测试命令验证。'
    '如果这是继续运行，先读取修改过的实现并核对上次进度，然后只补做尚未完成的验证，不要重复已确认的修改。'
    f'测试命令只允许在 cwd="." 下以 purpose="check" 使用这个 argv：{json.dumps(COMMAND, ensure_ascii=False)}。'
)


def fixture_snapshot(project):
    """Compare every fixture file and directory before allowing a test import."""
    from project_agent import _checked_stat, _pinned, _read_bytes, _target

    with _pinned(project, directory=True):
        if {item.name for item in project.iterdir()} != {'AGENTS.md', 'src', 'test_resume_fixture.py'}:
            raise ValueError('项目根目录内容已变化。')
    with _pinned(project, ('src',), directory=True):
        src = _target(project, ('src',), directory=True)
        info = _checked_stat(src)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise ValueError('src 目录已变化。')
        if {item.name for item in src.iterdir()} != {'AGENTS.md', 'charge.py'}:
            raise ValueError('src 目录内容已变化。')
    result = {}
    for relative, original in FIXTURE.items():
        parts = tuple(relative.split('/'))
        with _pinned(project, parts):
            actual = _read_bytes(_target(project, parts), limit=4096)
        accepted = (original, REPAIRED) if relative == SOURCE else (original,)
        if actual not in accepted:
            raise ValueError(f'实验文件内容越出允许范围 {relative}。')
        result[relative] = hashlib.sha256(actual).hexdigest()
    return result


def tests_ran(result, returncode):
    output = result.get('stderr', '') + '\n' + result.get('stdout', '')
    return (result.get('status') == 'exited' and result.get('returncode') == returncode
            and 'Ran 3 tests' in output and
            ('FAILED (failures=3)' in output if returncode else output.rstrip().endswith('OK')))


class Experiment:
    def __init__(self, folder):
        self.folder = folder
        self.project = folder / 'project'
        self.cancel_first = threading.Event()
        self.stage = 'first'
        self.approvals = []
        self.calls = []

    def approve(self, request):
        try:
            allowed = (request['argv'] == COMMAND and request['purpose'] == 'check'
                       and Path(request['executable']).resolve(strict=True) == PYTHON
                       and Path(request['cwd']).resolve(strict=True) == self.project)
            if allowed:
                fixture_snapshot(self.project)
        except (KeyError, OSError, ValueError, TypeError):
            allowed = False
        self.approvals.append({'stage': self.stage, 'allowed': allowed, 'request': request})
        print(f'{self.stage} 检查命令{"已批准" if allowed else "已拒绝"}', flush=True)
        return allowed

    def observe(self, event):
        if event.get('type') != 'tool_result':
            return
        tool, result = event['tool'], event['result']
        self.calls.append({'stage': self.stage, 'tool': tool, 'result': result})
        print(f'{self.stage} {tool} {result.get("status", result["ok"])}', flush=True)
        if self.stage == 'first' and result.get('changed') and result.get('path') == SOURCE:
            self.cancel_first.set()

    def prepare(self):
        for relative, body in FIXTURE.items():
            file = self.project / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(body)

    def evaluate(self, report):
        first, resumed = report['first'], report['resumed']
        final = fixture_snapshot(self.project)
        calls_first = [call for call in self.calls if call['stage'] == 'first']
        calls_resumed = [call for call in self.calls if call['stage'] == 'resumed']
        writes = [index for index, call in enumerate(calls_first) if call['result'].get('changed')]
        before = calls_first[:min(writes, default=0)]
        first_hashes = report['first_run_hashes']
        final_first_hashes = self.run_hashes(Path(first['run_dir']))
        before_hashes = report['original_hashes']
        report['final_hashes'] = final
        report['checks'] = {
            'search_found_implementation_before_edit': any(
                call['tool'] == 'search_text' and any(hit['path'] == SOURCE
                    and 'invoice_total' in hit['text'] for hit in call['result'].get('matches', []))
                for call in before),
            'scoped_guidance_visible_before_edit': any(
                'src/AGENTS.md' in call['result'].get('instruction_files', []) for call in before),
            'baseline_failed_before_edit': any(
                call['tool'] == 'run_command' and tests_ran(call['result'], 1) for call in before),
            'stopped_after_repair': first['status'] == 'stopped' and len(writes) == 1
                and first['continuation_available'] and final[SOURCE] == hashlib.sha256(REPAIRED).hexdigest(),
            'fresh_run_and_parent_checkpoint': first['run_dir'] != resumed['run_dir']
                and resumed['task_state']['parent_run'] == Path(first['run_dir']).name
                and final_first_hashes == first_hashes,
            'resume_did_not_replay_the_edit': resumed['changes'] == []
                and not any(call['result'].get('changed') for call in calls_resumed),
            'freshly_approved_check_passed_in_resume': any(
                row['stage'] == 'resumed' and row['allowed'] for row in self.approvals)
                and any(call['tool'] == 'run_command' and tests_ran(call['result'], 0)
                        for call in calls_resumed),
            'resume_completed_with_verified_state': resumed['status'] == 'completed'
                and resumed['task_state']['verification']['status'] == 'passed',
            'fixture_guards_unchanged': all(final[name] == before_hashes[name]
                                            for name in FIXTURE if name != SOURCE),
        }
        report['success'] = all(report['checks'].values())

    @staticmethod
    def run_hashes(directory):
        return {file.name: hashlib.sha256(file.read_bytes()).hexdigest()
                for file in directory.iterdir() if file.is_file()}


def main():
    folder = ROOT / 'development' / 'experiments' / f'agent-resume-{uuid.uuid4().hex}'
    os.environ['AI_TRAINING_DATA'] = str(folder / 'data')

    from capability_manager import PackageManager
    from local_project_model import LocalProjectModel
    from local_runtime import LocalRuntime
    from project_agent import ProjectAgent
    from run_local_project_experiment import CONTEXT, MODEL_ID, SCOPE, engine_info

    experiment = Experiment(folder)
    experiment.prepare()
    print(f'实验目录 {folder}', flush=True)
    start = time.monotonic()
    report = {'success': False, 'scope': SCOPE, 'original_hashes': fixture_snapshot(experiment.project)}
    service = None
    try:
        service = LocalRuntime(manager=PackageManager(root=ROOT / 'models'))
        common = dict(runs_root=folder / 'runs', approve_command=experiment.approve,
                      on_event=experiment.observe, context=CONTEXT, max_steps=16)
        first = ProjectAgent(experiment.project, LocalProjectModel(service, model_id=MODEL_ID),
                             cancel_event=experiment.cancel_first, **common).run(TASK)
        report['first'] = first
        report['first_run_hashes'] = experiment.run_hashes(Path(first['run_dir']))
        if first['status'] == 'stopped' and experiment.cancel_first.is_set():
            experiment.stage = 'resumed'
            report['resumed'] = ProjectAgent(experiment.project, LocalProjectModel(service, model_id=MODEL_ID),
                                               **common).run(TASK, resume_from=first['run_dir'])
            experiment.evaluate(report)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            report['model_engine'] = engine_info(service)
        except (AttributeError, IndexError, TypeError) as exc:
            report['engine_info_error'] = type(exc).__name__
        try:
            if service is not None:
                service.shutdown()
        except (OSError, RuntimeError) as exc:
            report['shutdown_error'] = type(exc).__name__
        report.update(approvals=experiment.approvals, tool_results=experiment.calls,
                      elapsed_seconds=round(time.monotonic() - start, 3))
        (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                                            encoding='utf-8')
    print(f'实验{"通过" if report["success"] else "未通过"}，报告 {folder / "report.json"}', flush=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
