"""Real local model, native conversation, inline approvals and a second turn."""
import json
import os
from pathlib import Path
import sys
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from run_local_project_experiment import (
    Experiment, FIXTURE, TASK, SCOPE, hashes, run_tests, verify_test_output, engine_info)


def main():
    folder = ROOT / 'development/experiments' / ('inline-project-' + uuid.uuid4().hex)
    experiment = Experiment(folder)
    experiment.project.mkdir(parents=True)
    os.environ['AI_TRAINING_DATA'] = str(folder / 'data')
    for name, content in FIXTURE.items():
        (experiment.project / name).write_text(content, encoding='utf-8')
    import tkinter as tk
    from client_app import ClientApp
    from capability_manager import PackageManager
    from local_runtime import LocalRuntime
    from project_chat import ActivityCard
    import qwen_chat

    print('实验目录 ' + str(folder), flush=True)
    report = {'success': False, 'scope': SCOPE, 'original_hashes': hashes(experiment.project),
              'baseline': run_tests(experiment.project), 'rounds': []}
    assert verify_test_output(report['baseline'], failed=True)
    callback_errors = []
    root = tk.Tk()
    root.withdraw()
    root.report_callback_exception = lambda kind, error, trace: callback_errors.append(str(error))
    app = None
    started = time.monotonic()
    original_event = ActivityCard.event

    def trace(card, event):
        experiment.on_event(event)
        original_event(card, event)

    def send(text):
        app.input.insert('1.0', text)
        app.send_chat()
        deadline = time.monotonic() + 240
        while app.busy and time.monotonic() < deadline:
            root.update()
            if callback_errors:
                raise AssertionError(callback_errors)
            card = app.project.active
            if card and card.request:
                details = card.request['details']
                protected = all(hashes(experiment.project).get(name) == report['original_hashes'][name]
                                for name in ('README.md', 'test_pricing.py'))
                card.decide(protected and experiment.approve_command(details))
            time.sleep(.015)
        if app.busy:
            app.generation_cancel.set()
            raise TimeoutError('Local model did not finish within four minutes')
        card = app.project.cards[-1]
        report['rounds'].append(card.result)
        print('本轮状态 ' + card.result['status'], flush=True)
        assert card.result['status'] == 'completed', card.result
        return card

    try:
        with patch.object(tk, 'Toplevel', side_effect=AssertionError('Unexpected extra window')), \
             patch.object(ActivityCard, 'event', trace):
            app = ClientApp(root)
            app.manager = PackageManager(root=ROOT / 'models')
            app.backend, app.selected_model = 'local', 'text-default'
            qwen_chat._runtime = LocalRuntime(manager=app.manager)
            app.project.set_mode(True)
            app.project.workspace.set(str(experiment.project))
            app.project.refresh()
            first = send(TASK + '另外约定下一轮如果我说“补上说明”，请给这个函数添加英文文档字符串 '
                         'Return the validated order total. 本轮先不要添加该说明。')
            report['first_final'] = run_tests(experiment.project)
            assert verify_test_output(report['first_final'], failed=False)
            first_identity = app.session_id
            first_history = list(app.history)
            second = send('补上刚才约定的说明，然后运行和刚才相同的测试。仍然只修改原来的实现文件。')
            report['final'] = run_tests(experiment.project)
            report['final_hashes'] = hashes(experiment.project)
            messages = json.loads((Path(second.result['run_dir']) / 'history.json').read_text(encoding='utf-8'))
            report['checks'] = {
                'first_tests_pass': verify_test_output(report['first_final'], failed=False),
                'second_tests_pass': verify_test_output(report['final'], failed=False),
                'same_session': app.session_id == first_identity,
                'four_chat_messages': len(app.history) == 4,
                'two_inline_cards': len(app.project.cards) == 2,
                'history_reaches_second_model_call': all(
                    any(message['role'] == row['role'] and row['content'] in message.get('content', '')
                        for message in messages) for row in first_history),
                'agreed_docstring_added': 'Return the validated order total.' in
                                         (experiment.project / 'pricing.py').read_text(encoding='utf-8'),
                'tests_and_readme_unchanged': all(report['final_hashes'][name] == report['original_hashes'][name]
                                                for name in ('README.md', 'test_pricing.py')),
                'two_persisted_cards': len(app.sessions.project_runs(first_identity)) == 2,
                'no_callback_errors': not callback_errors,
                'observed_failure_then_repair': experiment.test_sequence_passes(),
                'no_extra_files': not experiment.unexpected_files(),
            }
            app.load_conversation(first_identity)
            report['checks']['restored_without_running'] = not app.busy and len(app.project.cards) == 2
            report['success'] = all(report['checks'].values())
            report['session_id'] = first_identity
            report['model_engine'] = engine_info(qwen_chat._runtime)
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
        print(report['error'], flush=True)
    finally:
        if app:
            app.generation_cancel.set()
            until = time.monotonic() + 8
            while app.busy and time.monotonic() < until:
                root.update()
                time.sleep(.02)
        qwen_chat.shutdown()
        root.destroy()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['tool_counts'] = dict(experiment.tool_counts)
        (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'success': report['success'], 'elapsed_seconds': report['elapsed_seconds'],
                          'report': str(folder / 'report.json'), 'checks': report.get('checks', {})},
                         ensure_ascii=False), flush=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
