"""Live local-model memory learning, fresh-chat recall and project memory tools."""
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
folder = ROOT / 'development/experiments' / ('memory-' + uuid.uuid4().hex)
folder.mkdir(parents=True)
os.environ['AI_TRAINING_DATA'] = str(folder / 'data')
sys.path.insert(0, str(ROOT))

import tkinter as tk
from agent_memory import AgentMemoryStore
from capability_manager import PackageManager
from client_app import ClientApp
from local_runtime import LocalRuntime
import qwen_chat


def main():
    root = tk.Tk()
    root.withdraw()
    errors = []
    app = ClientApp(root)
    root.report_callback_exception = lambda kind, error, trace: errors.append(str(error))
    app.manager = PackageManager(root=ROOT / 'models')
    app.backend, app.selected_model = 'local', 'text-default'
    app.context.set('阅读长文 · 16K')
    report = {'success': False, 'model': 'Qwen3.5 4B Q4_K_M', 'answers': [], 'checks': {}, 'session_ids': [],
              'extractions': []}
    def start_runtime():
        qwen_chat._runtime = LocalRuntime(manager=app.manager)
        original_generate = qwen_chat._runtime.generate

        def capture_generation(prompt, *args, **kwargs):
            answer = original_generate(prompt, *args, **kwargs)
            if 'evidence_quote' in prompt:
                report['extractions'].append(answer)
            return answer

        qwen_chat._runtime.generate = capture_generation

    start_runtime()
    started = time.monotonic()
    print('实验目录 ' + str(folder), flush=True)

    def send(message):
        app.input.delete('1.0', 'end')
        app.input.insert('1.0', message)
        app.send_chat()
        deadline = time.monotonic() + 180
        while app.busy and time.monotonic() < deadline:
            root.update()
            if errors:
                raise AssertionError(errors)
            if app.project.active and app.project.active.request:
                app.project.active.decide(False)
            time.sleep(.015)
        if app.busy:
            app.generation_cancel.set()
            raise TimeoutError('Local memory experiment exceeded three minutes')
        report['answers'].append(app.last_answer)
        report['session_ids'].append(app.session_id)
        print(app.last_answer[:400], flush=True)
        return app.last_answer

    try:
        manual = app.personal_memory.save_user('回答语言', '回答使用中文，先给结论，尽量简洁。', kind='preference')
        question = '我的演示项目代号是什么？只告诉我代号。'
        baseline = qwen_chat.chat(question, [], use_memory=False, context=16384, return_details=True)
        report['baseline_answer'] = baseline['answer']
        report['checks']['unknown_without_memory'] = '青石742' not in re.sub(r'\s+', '', baseline['answer'])
        send('请长期记住，我正在开发的演示项目代号是青石742，以后提到演示项目时使用这个代号。')
        first_session = app.session_id
        first_rows = app.personal_memory.list()
        learned = [row for row in first_rows if row['source'] == 'agent'
                   and '青石742' in re.sub(r'\s+', '', row['content'])]
        report['learned'] = learned
        report['checks']['chat_model_saved_memory'] = bool(learned)
        report['checks']['notice_in_chat'] = '[长期记忆] AI 已记下' in app.last_answer
        report['checks']['saved_active_unverified'] = bool(learned) and all(
            row['status'] == 'active' and not row['verified'] for row in learned)
        # Close the native app and its SQLite store, then construct a new app.
        # The normal close path also shuts down the model server and timers.
        app.close()
        root = tk.Tk()
        root.withdraw()
        root.report_callback_exception = lambda kind, error, trace: errors.append(str(error))
        app = ClientApp(root)
        app.manager = PackageManager(root=ROOT / 'models')
        app.backend, app.selected_model = 'local', 'text-default'
        app.context.set('阅读长文 · 16K')
        start_runtime()
        report['checks']['persisted_after_app_reopen'] = bool(learned) and all(
            app.personal_memory.get(row['id'])['content'] == row['content'] for row in learned)
        app.new_conversation()
        report['checks']['fresh_chat_no_history'] = not app.history
        recalled = send(question)
        # Assess recall independently from the small model's spacing fidelity.
        # Preserve the exact answer and the strict result; never rewrite model output.
        report['verbatim_recall'] = '青石742' in recalled
        report['checks']['recall_in_new_conversation'] = first_session != app.session_id and '青石742' in re.sub(r'\s+', '', recalled)
        report['checks']['recall_notice'] = '条记忆作为参考' in recalled
        report['checks']['manual_entry_preserved'] = app.personal_memory.get(manual['id'])['source'] == 'user'
        if learned:
            identity = learned[0]['id']
            app.personal_memory.set_pinned(identity, True)
            found = app.personal_memory.before_call('设计一个欢迎页面')
            report['checks']['pinned_external_context'] = identity in found['memory_ids']
            verified = app.personal_memory.verify(identity)
            protected, changed = app.personal_memory.remember(verified['title'], '改用错误代号',
                kind=verified['kind'], return_changed=True)
            report['checks']['verify_retains_origin_and_protects_content'] = (
                verified['verified'] and verified['source'] == 'agent' and not changed
                and protected['content'] == verified['content'])
        workspace = folder / 'project'
        workspace.mkdir()
        app.new_conversation()
        app.project.set_mode(True)
        app.project.workspace.set(str(workspace))
        app.project.allow_write.set(False)
        app.project.refresh()
        send('这个项目的长期约定是：输出文件名统一使用小写英文。请用记忆工具保存为当前项目的做法，之后简短回答即可，不创建文件、不运行命令。')
        card = app.project.cards[-1]
        rows = app.personal_memory.list(workspace=str(workspace))
        saved = [row for row in rows if row['source'] == 'agent' and row['scope'] == 'project']
        report['project_memories'] = saved
        report['project_run'] = card.result
        report['checks']['project_tool_saved_memory'] = bool(saved) and bool(card.result.get('memory_updates'))
        report['checks']['project_memory_isolated'] = not any(row['id'] in {item['id'] for item in saved}
                                                          for row in app.personal_memory.list(workspace=str(folder / 'other')))
        report['checks']['project_files_unchanged'] = not list(workspace.iterdir()) and not card.result.get('changes')
        for row in learned:
            app.personal_memory.delete(row['id'])
        app.new_conversation()
        app.project.set_mode(False)
        forgotten = send(question)
        report['checks']['deleted_not_recalled'] = '青石742' not in re.sub(r'\s+', '', forgotten)
        report['checks']['callbacks_clean'] = not errors
        report['success'] = all(report['checks'].values())
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        app.generation_cancel.set()
        deadline = time.monotonic() + 8
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.02)
        app.close()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'success': report['success'], 'elapsed_seconds': report['elapsed_seconds'],
                          'checks': report['checks'], 'error': report.get('error'),
                          'report': str(folder / 'report.json')}, ensure_ascii=False), flush=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
