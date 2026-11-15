"""Exercise concurrent file edits with the installed local project model."""

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
PYTHON = (ROOT / 'runtime' / 'python' / 'python.exe').resolve()
COMMAND = [str(PYTHON), '-B', '-m', 'unittest', '-v', 'test_tags']
SOURCE = 'tags.py'
EXTERNAL = '\n# External editor: keep this note for the next review.\n'
EXPRESSION = 'value.strip().replace(" ", "-").lower()'
BROKEN = f'''"""Normalize display tags."""


def slugify_tag(value):
    """Convert a display tag to lowercase words joined by hyphens."""
    return {EXPRESSION}
'''
WITH_EXTERNAL = BROKEN + EXTERNAL
REPAIRS = {
    WITH_EXTERNAL.replace(EXPRESSION, expression.replace('"', quote))
    for expression in (
        '"-".join(value.split()).lower()',
        '"-".join(value.lower().split())',
        '"-".join(value.strip().lower().split())',
        '"-".join(value.strip().split()).lower()',
    )
    for quote in ('"', "'")
}
# Also accept the exact regular-expression solution reviewed after the first trial.
# The allowlist still rejects arbitrary executable model output.
REPAIRS.add(WITH_EXTERNAL.replace('"""Normalize display tags."""\n',
                                  '"""Normalize display tags."""\nimport re\n').replace(
    EXPRESSION, "re.sub(r'\\s+', '-', value.strip()).lower()"))
REPAIRS.add(WITH_EXTERNAL.replace('"""Normalize display tags."""\n',
                                  '"""Normalize display tags."""\nimport re\n').replace(
    EXPRESSION, "re.sub(r'\\s+', '-', re.sub(r'\\s+', ' ', value.strip())).lower()"))
PERMITTED_SOURCE = {version.encode('utf-8') for version in {BROKEN, WITH_EXTERNAL, *REPAIRS}}
FIXTURE = {
    'AGENTS.md': '# 项目约定\n保留函数签名、文档、注释和测试。只局部替换实现中需要修复的表达式。\n',
    SOURCE: BROKEN,
    'test_tags.py': '''import unittest

from tags import slugify_tag


class TagTests(unittest.TestCase):
    def test_collapses_repeated_spaces(self):
        self.assertEqual(slugify_tag("  Red    Leaf  "), "red-leaf")

    def test_splits_tabs(self):
        self.assertEqual(slugify_tag("New\tTag"), "new-tag")

    def test_splits_unicode_whitespace(self):
        self.assertEqual(slugify_tag("中文\u3000标题"), "中文-标题")
''',
}
TASK = (
    '修复 slugify_tag，使它转为小写并把连续的空白字符合并成一个连字号，保留测试和现有注释。'
    '先运行一次测试并查看真实失败，再用 search_text 找到实现，read_file 读取后用 replace_text 局部修复。'
    '如果工具报告文件已变化，请重新读取，保留文件里的改动，然后再提交修复。'
    '修复后用同一条测试命令重新验证，准确报告发生过的变化和验证结果。'
    f'测试在 cwd="." 下以 purpose="check" 运行，下列 argv 是允许的完整命令：{json.dumps(COMMAND, ensure_ascii=False)}。'
)


def snapshot(project):
    """Accept only the trusted fixture contents and a small set of repaired expressions."""
    from project_agent import _pinned, _read_bytes, _target

    with _pinned(project, directory=True):
        if {item.name for item in project.iterdir()} != FIXTURE.keys():
            raise ValueError('实验项目的文件列表已变化。')
        contents = {name: _read_bytes(_target(project, (name,)), limit=4096) for name in FIXTURE}
        for name, value in contents.items():
            allowed = PERMITTED_SOURCE if name == SOURCE else {FIXTURE[name].encode('utf-8')}
            if value not in allowed:
                raise ValueError(f'实验文件越出允许范围 {name}。')
        return {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}


class Experiment:
    def __init__(self, folder, previous=None):
        self.project = folder / 'project'
        if previous is None:
            self.project.mkdir(parents=True)
            for name, content in FIXTURE.items():
                (self.project / name).write_bytes(content.encode('utf-8'))
        self.calls = list(previous['tool_results']) if previous else []
        self.approvals = list(previous['approvals']) if previous else []
        self.injection = previous['external_edit_event'] if previous else None

    def approve(self, request):
        try:
            allowed = (request['argv'] == COMMAND and request['purpose'] == 'check'
                       and Path(request['executable']).resolve(strict=True) == PYTHON
                       and Path(request['cwd']).resolve(strict=True) == self.project)
            if allowed:
                snapshot(self.project)
        except (KeyError, OSError, ValueError, TypeError):
            allowed = False
        self.approvals.append({'allowed': allowed, 'request': request})
        print(f'检查命令{"已批准" if allowed else "已拒绝"}', flush=True)
        return allowed

    def observe(self, event):
        if event.get('type') != 'tool_result':
            return
        tool, result = event['tool'], event['result']
        self.calls.append({'tool': tool, 'result': result})
        print(f'{tool} {result.get("status", result["ok"])}', flush=True)
        if (self.injection is None and tool == 'read_file' and result['ok']
                and result.get('path') == SOURCE):
            file = self.project / SOURCE
            if file.read_bytes() == BROKEN.encode('utf-8'):
                with file.open('ab') as stream:
                    stream.write(EXTERNAL.encode('utf-8'))
                self.injection = len(self.calls) - 1
                print('外部编辑器追加注释。', flush=True)

    def evidence(self, result):
        from run_agent_resume_experiment import tests_ran

        def find(tool, condition):
            return next((index for index, call in enumerate(self.calls)
                         if call['tool'] == tool and condition(call['result'])), None)
        failure = find('run_command', lambda data: tests_ran(data, 1))
        search = find('search_text', lambda data: any(hit['path'] == SOURCE
                      for hit in data.get('matches', [])))
        stale = find('replace_text', lambda data: not data['ok'] and '已变化' in data.get('message', ''))
        reread = find('read_file', lambda data: data.get('path') == SOURCE and
                      EXTERNAL.strip() in data.get('content', ''))
        repair = find('replace_text', lambda data: data.get('changed') and data.get('path') == SOURCE)
        passed = find('run_command', lambda data: tests_ran(data, 0))
        sequence = [failure, search, self.injection, stale, reread, repair, passed]
        final_source = (self.project / SOURCE).read_bytes().decode('utf-8')
        return {
            'repair_followed_conflict_and_reread': all(index is not None for index in sequence)
                and sequence == sorted(set(sequence)),
            'external_edit_preserved': final_source in REPAIRS,
            'only_implementation_changed': {call['result']['path'] for call in self.calls
                                            if call['result'].get('changed')} == {SOURCE},
            'completed_with_passing_checks': result['status'] == 'completed'
                and result['task_state']['verification']['status'] == 'passed',
        }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume-report', type=Path)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    previous = None
    if args.resume_report:
        original_report = args.resume_report.resolve(strict=True)
        original_report.relative_to(ROOT / 'development' / 'experiments')
        previous = json.loads(original_report.read_text(encoding='utf-8'))
        folder = original_report.parent
    else:
        folder = ROOT / 'development' / 'experiments' / f'edit-conflict-{uuid.uuid4().hex}'
    if args.verify_only and previous is None:
        parser.error('--verify-only requires a prior report')
    output = folder / ('report-verified.json' if args.verify_only else
                       ('report-resumed.json' if previous else 'report.json'))
    if output.exists():
        parser.error('The output report already exists; retain prior evidence unchanged.')
    os.environ['AI_TRAINING_DATA'] = str(folder / 'data')

    from capability_manager import PackageManager
    from local_project_model import LocalProjectModel
    from local_runtime import LocalRuntime
    from project_agent import ProjectAgent
    from run_local_project_experiment import CONTEXT, MODEL_ID, SCOPE, engine_info

    experiment = Experiment(folder, previous)
    snapshot(experiment.project)
    print(f'实验目录 {folder}', flush=True)
    started = time.monotonic()
    report = {'success': False, 'scope': SCOPE,
              'original_hashes': previous['original_hashes'] if previous else snapshot(experiment.project)}
    if previous:
        report['previous_report'] = str(original_report)
        report['previous_status'] = previous['result']['status']
        report['harness_adjustment'] = 'Reviewed and allowed the exact regex implementation; original report retained.'
        report['verification_only'] = args.verify_only
    service = None
    try:
        service = LocalRuntime(manager=PackageManager(root=ROOT / 'models'))
        model = LocalProjectModel(service, model_id=MODEL_ID)
        agent = ProjectAgent(experiment.project, model, runs_root=folder / 'runs',
                             approve_command=experiment.approve, on_event=experiment.observe,
                             context=CONTEXT, max_steps=20, allow_write=not args.verify_only)
        if previous:
            result = agent.run(('当前只读验收阶段：不要再修改任何文件。原有实现已通过实验允许范围审核，请直接补跑原定测试。'
                                if args.verify_only else
                                '继续原任务。先读取当前实现，保留已有代码和注释；若修复正确，只补做原定测试。')
                               + f'使用这个 argv，以 purpose="check" 执行：{json.dumps(COMMAND, ensure_ascii=False)}。',
                               resume_from=previous['result']['run_dir'])
        else:
            result = agent.run(TASK)
        report['result'] = result
        final = snapshot(experiment.project)
        report['final_hashes'] = final
        report['checks'] = experiment.evidence(result)
        report['checks']['fixture_guards_unchanged'] = all(
            value == report['original_hashes'][name] for name, value in final.items() if name != SOURCE)
        report['success'] = all(report['checks'].values())
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
        report.update(tool_results=experiment.calls, approvals=experiment.approvals,
                      external_edit_event=experiment.injection, elapsed_seconds=round(time.monotonic() - started, 3))
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'实验{"通过" if report["success"] else "未通过"}，报告 {output}', flush=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
