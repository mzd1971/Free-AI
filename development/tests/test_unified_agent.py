"""Integrated utility, scope, and recovery checks for the tool conversation loop."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_memory import AgentMemoryStore
from project_agent import ProjectAgent

from development.tests.test_project_agent import ScriptedModel


PROJECT_TOOLS = {'list_files', 'search_text', 'read_file', 'write_file', 'replace_text', 'run_command'}
UTILITIES = {'calculate', 'check_json', 'current_time'}


class UnifiedAgentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / 'internal-workspace'
        self.workspace.mkdir()
        self.runs = self.root / 'runs'
        self.events = []

    def agent(self, *turns, capability_mode='assistant', **options):
        model = ScriptedModel(*turns)
        model.expected_tools = {'update_plan'}
        if capability_mode == 'project':
            model.expected_tools.update(PROJECT_TOOLS)
        if capability_mode == 'assistant' or options.get('integrated_tools'):
            model.expected_tools.update(UTILITIES)
            if options.get('csv_path') is not None:
                model.expected_tools.add('summarize_csv')
        if options.get('allow_web'):
            model.expected_tools.update(('web_search', 'web_fetch'))
        if options.get('personal_memory') is not None and options.get('write_memory', True):
            model.expected_tools.add('remember_memory')
        self.model = model
        return ProjectAgent(self.workspace, model, runs_root=self.runs, on_event=self.events.append,
                            approve_command=lambda request: self.fail('Assistant attempted local execution'),
                            capability_mode=capability_mode, **options)

    def results(self, tool):
        return [event['result'] for event in self.events
                if event['type'] == 'tool_result' and event['tool'] == tool]

    def test_assistant_executes_utility_tools_and_checks_evidence(self):
        csv_path = self.root / 'metrics.csv'
        csv_path.write_text('name,value\nalpha,2\nbeta,4\ngamma,\n', encoding='utf-8')
        now = datetime(2026, 11, 22, 10, 11, 12, tzinfo=timezone(timedelta(hours=8)))
        agent = self.agent([
            ('calculate', {'expression': '(13 + 6) * 2'}),
            ('check_json', {'text': '{"value":38}'}),
            ('current_time', {}),
            ('summarize_csv', {}),
        ], '已计算、检查和统计。', csv_path=csv_path)
        with patch('clock_context.current_time', return_value=now):
            result = agent.run('请计算、检查 JSON、读取本机时间，并统计所选 CSV。')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['capability_mode'], 'assistant')
        self.assertEqual(result['changes'], [])
        observations = [json.loads(row['content']) for row in self.model.calls[-1] if row['role'] == 'tool']
        self.assertTrue(all(row['ok'] and row['tool_execution_verified'] for row in observations))
        calculator, check, clock, csv = [row['evidence'] for row in observations]
        self.assertEqual(calculator['value'], 38)
        self.assertEqual(json.loads(check['formatted_json']), {'value': 38})
        self.assertEqual(clock['clock_at'], now.isoformat())
        self.assertEqual(clock['weekday'], '星期日')
        self.assertEqual(csv['file_name'], 'metrics.csv')
        self.assertEqual(csv['data_rows'], 3)
        self.assertEqual(csv['columns'][1]['mean'], 3)
        self.assertEqual(csv['columns'][1]['missing_count'], 1)
        self.assertNotIn(str(csv_path.parent), str(observations))
        self.assertEqual(json.loads((Path(result['run_dir']) / 'result.json').read_text(encoding='utf-8')), result)

    def test_assistant_excludes_project_and_unselected_csv_capabilities(self):
        instructions = self.workspace / 'AGENTS.md'
        instructions.write_text('UNIQUE_PRIVATE_WORKSPACE_INSTRUCTIONS', encoding='utf-8')
        sample = self.workspace / 'private.txt'
        sample.write_text('UNIQUE_PRIVATE_WORKSPACE_CONTENT', encoding='utf-8')
        planned = [
            ('list_files', {}), ('search_text', {'query': 'UNIQUE'}), ('read_file', {'path': sample.name}),
            ('write_file', {'path': sample.name, 'content': 'changed'}),
            ('replace_text', {'path': sample.name, 'old': 'UNIQUE', 'new': 'changed'}),
            ('run_command', {'argv': ['unused'], 'purpose': 'check'}),
            ('summarize_csv', {}), ('web_search', {'query': 'unused'}),
        ]
        agent = self.agent(planned, '只回答普通问题。', permission_mode='automatic')
        result = agent.run('回答普通问题。', history=[{'role': 'user', 'content': '旧问题'},
                                                        {'role': 'assistant', 'content': '旧回答'}])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['task_state']['verification']['checks'], [])
        self.assertEqual(result['task_state']['failed_writes'], [])
        self.assertEqual(result['task_state']['failed_utilities'], [])
        self.assertEqual(sample.read_text(encoding='utf-8'), 'UNIQUE_PRIVATE_WORKSPACE_CONTENT')
        for name, _ in planned:
            self.assertNotIn(name, agent._tools)
            self.assertEqual(self.results(name)[0]['status'], 'unavailable')
        all_prompts = json.dumps(self.model.calls, ensure_ascii=False)
        self.assertNotIn('internal-workspace', all_prompts)
        self.assertNotIn('UNIQUE_PRIVATE_WORKSPACE_INSTRUCTIONS', all_prompts)
        self.assertNotIn('UNIQUE_PRIVATE_WORKSPACE_CONTENT', all_prompts)
        self.assertNotIn('旧问题', self.model.calls[0][0]['content'])
        self.assertIn('旧回答', all_prompts)

    def test_csv_path_override_fails_then_selected_file_can_be_used(self):
        selected = self.root / 'selected.csv'
        selected.write_text('value\n2\n4\n', encoding='utf-8')
        other = self.root / 'other.csv'
        other.write_text('value\n99\n', encoding='utf-8')
        agent = self.agent([('summarize_csv', {'path': str(other)})], '没有执行。',
                           [('summarize_csv', {})], '已统计所选 CSV。', csv_path=selected)
        result = agent.run('统计所选 CSV。')
        failed, summary = self.results('summarize_csv')
        self.assertFalse(failed['ok'])
        self.assertEqual(summary['evidence']['file_name'], selected.name)
        self.assertEqual(summary['evidence']['columns'][0]['mean'], 3)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['task_state']['failed_utilities'], [])

    def test_failure_is_checkpointed_and_blocks_premature_completion_after_resume(self):
        first = self.agent([('calculate', {'expression': '42 / 0'})], max_steps=1)
        paused = first.run('计算给定算式，并在必要时修正算式。')
        self.assertEqual(paused['status'], 'limit')
        self.assertEqual(paused['task_state']['failed_utilities'], ['calculate'])
        self.assertFalse(self.results('calculate')[0]['ok'])

        second = self.agent('已完成。', '已经完成。')
        blocked = second.run('继续任务。', resume_from=paused['run_dir'])
        self.assertEqual(blocked['status'], 'unfinished')
        self.assertIn('calculate', blocked['answer'])
        self.assertNotIn('已经完成', blocked['answer'])
        self.assertEqual(blocked['task_state']['failed_utilities'], ['calculate'])
        self.assertIn('calculate', self.model.calls[0][-1]['content'])

        third = self.agent([('calculate', {'expression': '42 / 2'})], '结果是 21。')
        finished = third.run('继续任务。', resume_from=blocked['run_dir'])
        self.assertEqual(finished['status'], 'completed')
        self.assertEqual(finished['task_state']['failed_utilities'], [])
        self.assertEqual(self.results('calculate')[-1]['evidence']['value'], 21)

    def test_batch_retains_invalid_json_when_another_input_passes(self):
        agent = self.agent([('check_json', {'text': '{invalid}'}),
                            ('check_json', {'text': '{}'})], '都通过了。', '都通过了。')
        result = agent.run('检查两段 JSON。')
        self.assertEqual(result['status'], 'unfinished')
        self.assertEqual(result['task_state']['failed_utilities'], ['check_json'])
        self.assertEqual(self.results('check_json')[0]['error']['code'], 'json_syntax')
        self.assertTrue(self.results('check_json')[1]['ok'])

    def test_malformed_utility_success_cannot_clear_failure(self):
        agent = self.agent([('calculate', {'expression': '21 * 2'})], '完成了。', '完成了。')
        with patch.dict(agent._tools, calculate=lambda expression: {'ok': True, 'evidence': {}}):
            result = agent.run('计算给定算式。')
        self.assertEqual(result['status'], 'unfinished')
        self.assertFalse(self.results('calculate')[0]['ok'])

    def test_assistant_memory_uses_no_workspace_and_filters_snapshot(self):
        store = AgentMemoryStore(self.root / 'memory')
        store.save_user('回答约定', 'PROJECT_SPECIFIC_MEMORY', scope='project', workspace=str(self.workspace))
        allowed = store.save_user('回答偏好', '优先使用常见词语。', scope='agent', kind='preference')
        agent = self.agent([
            ('remember_memory', {'title': '项目约定', 'content': '单独的项目内容。', 'scope': 'project'}),
            ('remember_memory', {'title': '当前日期', 'content': '2026-11-22T10:11:12+08:00'}),
            ('remember_memory', {'title': '临时提问', 'content': '现在几点？'}),
            ('remember_memory', {'title': '时间格式', 'content': '请始终使用 24 小时制。', 'kind': 'preference'}),
        ], '记住了你对时间格式的偏好。', personal_memory=store)
        with patch.object(store, 'before_call', wraps=store.before_call) as lookup, \
                patch.object(store, 'remember', wraps=store.remember) as write:
            result = agent.run('按我的回答偏好作答，并记住我喜欢 24 小时制。')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(lookup.call_args.kwargs['workspace'], '')
        self.assertEqual(write.call_count, 1)
        self.assertEqual(write.call_args.kwargs['workspace'], '')
        self.assertEqual(write.call_args.kwargs['scope'], 'agent')
        self.assertEqual([row['title'] for row in result['memory_updates']], ['时间格式'])
        self.assertIn(allowed['id'], result['memory_ids'])
        self.assertNotIn('PROJECT_SPECIFIC_MEMORY', self.model.calls[0][-1]['content'])
        self.assertEqual([row.get('status') for row in self.results('remember_memory')][1:3], ['skipped'] * 2)

    def test_web_sources_and_local_calculation_share_one_loop(self):
        search = {'results': [{'title': 'Example', 'url': 'https://example.com', 'snippet': 'Value: 21'}]}
        agent = self.agent([('web_search', {'query': 'example value'})],
                           [('calculate', {'expression': '21 * 2'})], '结果 42。', allow_web=True)
        with patch.object(agent.web, 'search', return_value=search):
            result = agent.run('查找数字并计算加倍后的结果。')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([row['url'] for row in result['web_sources']], ['https://example.com'])
        self.assertTrue(self.results('web_search')[0]['untrusted'])
        self.assertEqual(self.results('calculate')[0]['evidence']['value'], 42)

    def test_project_can_opt_in_without_losing_file_tools(self):
        (self.workspace / 'numbers.txt').write_text('21', encoding='utf-8')
        agent = self.agent([('read_file', {'path': 'numbers.txt'}),
                            ('calculate', {'expression': '21 * 2'})], '结果 42。',
                           capability_mode='project', integrated_tools=True)
        result = agent.run('读取项目数字并计算加倍后的结果。')
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('capability_mode', result)
        self.assertEqual(self.results('read_file')[0]['content'], '21')
        self.assertEqual(self.results('calculate')[0]['evidence']['value'], 42)


if __name__ == '__main__':
    unittest.main()
