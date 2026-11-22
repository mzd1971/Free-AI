"""Exercise assistant tools and durable recall with the installed local model."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PREFERENCE = '琥珀复盘731'
DOCS_URL = 'https://docs.python.org/3/builtins/functions.html'
RECALL = '我要求以后撰写每周复盘时使用哪个固定标题？只回答标题。'
TASK = (
    '帮我完成下面四件事，并按真实执行结果用中文汇报。'
    '一，用 web_search 搜索 Python 官方 abs 函数文档，再用 web_fetch 读取官方网页 '
    + DOCS_URL + '，说明 abs 的含义，并引用 web_fetch 返回的最终 url 字段。'
    '二，用 calculate 计算 (673 * 29) + 17，汇报结果。'
    '三，用 summarize_csv 统计我为本轮选好的 CSV，汇报数据行数、amount 列的均值和空白数。'
    '四，请今后每次替我撰写每周复盘时都使用固定标题“' + PREFERENCE + '”。'
    '这是我的长期写作偏好，请现在通过 remember_memory 以 preference、agent 范围保存，'
    '并在工具确认保存后告诉我。'
)
CSV_DATA = b'item,amount\nA,12\nB,33\nC,\n'
BASE_TOOLS = {'update_plan', 'remember_memory', 'calculate', 'check_json', 'current_time'}
RESEARCH_TOOLS = BASE_TOOLS | {'summarize_csv', 'web_search', 'web_fetch'}


def show_progress(event):
    if event['type'] in ('tool_result', 'finish', 'memory'):
        print(json.dumps({'type': event['type'], 'tool': event.get('tool'),
                          'ok': event.get('result', {}).get('ok'),
                          'message': event['message']}, ensure_ascii=False), flush=True)


def successful(round_data, name):
    return [event['result'] for event in round_data['events']
            if event['type'] == 'tool_result' and event['tool'] == name and event['result'].get('ok')]


def csv_matches(evidence):
    amount = next((column for column in evidence.get('columns', []) if column['name'] == 'amount'), {})
    return (evidence.get('file_name') == 'selected.csv' and evidence.get('data_rows') == 3
            and amount.get('mean') == 22.5 and amount.get('missing_count') == 1)


def completed(round_data, allowed):
    result = round_data['result']
    return (result['status'] == 'completed' and result.get('capability_mode') == 'assistant'
            and set(round_data['exposed_tools']) == allowed and result['changes'] == []
            and all(event.get('tool') in allowed for event in round_data['events']
                    if event['type'] in ('tool', 'tool_result')))


def verify_research(round_data, rows):
    result = round_data['result']
    answer = result['answer']
    fetched = [observation['url'] for observation in successful(round_data, 'web_fetch')
               if urlsplit(observation['url']).hostname == 'docs.python.org'
               and re.search(r'\babs\b', observation.get('text', ''))]
    observations = [row for row in result.get('web_sources', []) if row['kind'] == 'page']
    checks = {
        'assistant_completed_with_scoped_tools': completed(round_data, RESEARCH_TOOLS),
        'public_search_returned_sources': any(row.get('results') and row.get('fetched_at')
                                              for row in successful(round_data, 'web_search')),
        'official_page_fetched_and_cited': any(row['url'] in fetched and row['url'] in answer
                                               for row in observations)
                                          and bool(re.search(r'绝对值|\babsolute value\b', answer, re.I)),
        'calculator_evidence_and_answer': any(row.get('tool_execution_verified')
                                             and row['evidence'].get('value') == 19534
                                             for row in successful(round_data, 'calculate'))
                                          and '19534' in answer.replace(',', ''),
        'selected_csv_evidence_and_answer': any(row.get('tool_execution_verified')
                                               and csv_matches(row['evidence'])
                                               for row in successful(round_data, 'summarize_csv'))
                                             and '22.5' in answer,
    }
    update_ids = {row['id'] for row in result.get('memory_updates', [])}
    memory_results = successful(round_data, 'remember_memory')
    tool_memory_ids = {row.get('memory', {}).get('id') for row in memory_results}
    saved = [row for row in rows if row['id'] in update_ids & tool_memory_ids
             and PREFERENCE in re.sub(r'\s+', '', row['content'])
             and row['source'] == 'agent' and row['scope'] == 'agent' and not row['workspace']
             and row['kind'] == 'preference' and row['status'] == 'active' and not row['verified']]
    checks['preference_saved_active_unverified'] = bool(saved)
    return checks, saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='text-default')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ['AI_TRAINING_DATA'] = str(output / 'data')

    from agent_memory import AgentMemoryStore
    from capability_manager import PackageManager
    from local_project_model import LocalProjectModel
    from local_runtime import LocalRuntime
    from project_agent import ProjectAgent

    csv_path = output / 'selected.csv'
    csv_path.write_bytes(CSV_DATA)
    memory_root = output / 'data' / 'agent'
    workspace = output / 'workspace'
    fresh_workspace = output / 'fresh-workspace'
    workspace.mkdir()
    fresh_workspace.mkdir()
    manager = PackageManager(root=ROOT / 'models')
    runtime = LocalRuntime(manager=manager, request_timeout=120)
    started = time.monotonic()
    report = {'model': args.model, 'verified': False, 'rounds': {}, 'checks': {}}

    def run_round(name, task, current_workspace, store, *, web=False, selection=None):
        round_data = {'prompt': task, 'events': []}
        report['rounds'][name] = round_data

        def on_event(event):
            round_data['events'].append(event)
            show_progress(event)

        print(f'Running {name} with {args.model}', flush=True)
        model = LocalProjectModel(runtime=runtime, model_id=args.model)
        agent = ProjectAgent(current_workspace, model, runs_root=output / 'runs',
                             approve_command=lambda request: False, on_event=on_event, context=16384,
                             personal_memory=store, capability_mode='assistant', integrated_tools=True,
                             allow_web=web, csv_path=selection)
        round_data['exposed_tools'] = [tool['function']['name'] for tool in agent.tools]
        round_data['result'] = agent.run(task, history=[])
        return round_data

    try:
        store = AgentMemoryStore(memory_root)
        report['checks']['memory_initially_empty'] = not store.list()
        research = run_round('research_and_save', TASK, workspace, store, web=True, selection=csv_path)
        report['stored_memories'] = store.list()
        checks, saved = verify_research(research, report['stored_memories'])
        report['checks'].update(checks)
        del store
        fresh_store = AgentMemoryStore(memory_root)
        recall = run_round('fresh_recall', RECALL, fresh_workspace, fresh_store)
        recall_result = recall['result']
        memory_ids = set(recall_result.get('memory_ids', []))
        report['checks'].update({
            'fresh_assistant_completed_with_scoped_tools': completed(recall, BASE_TOOLS),
            'disk_memory_reopened_and_injected': bool(saved) and all(
                fresh_store.get(row['id']) == row and row['id'] in memory_ids for row in saved),
            'fresh_answer_recalled_preference': PREFERENCE not in RECALL
            and PREFERENCE in re.sub(r'\s+', '', recall_result['answer']),
            'recall_did_not_rewrite_preferences': fresh_store.list() == report['stored_memories'],
            'project_workspaces_and_selected_csv_untouched': not any(workspace.iterdir())
            and not any(fresh_workspace.iterdir()) and csv_path.read_bytes() == CSV_DATA,
        })
        report['verified'] = all(report['checks'].values())
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        report['engine'] = runtime.last_engine_notice
        runtime.shutdown()
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                                            encoding='utf-8')

    print(json.dumps({'verified': report['verified'], 'checks': report['checks'],
                      'error': report.get('error'), 'report': str(output / 'report.json')},
                     ensure_ascii=False), flush=True)
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
