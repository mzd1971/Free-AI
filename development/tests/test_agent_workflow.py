"""End-to-end checks for project tools, verification, and resumption."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from project_agent import ProjectAgent
from test_project_agent import ScriptedModel


DEVELOPMENT = Path(__file__).resolve().parents[1]
CHECK = {'argv': [sys.executable, '-B', '-c',
                  "from pathlib import Path; assert Path('status.txt').read_text() == 'ready'"],
         'purpose': 'check'}
OTHER_CHECK = {'argv': [sys.executable, '-B', '-c', "print('unrelated successful check')"],
               'purpose': 'check'}
REPAIR = {'path': 'status.txt', 'old': 'broken', 'new': 'ready'}
GOAL = '修复状态，并用指定命令验证。'


class AgentWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        self.project = folder / 'project'
        self.project.mkdir()
        self.runs = folder / 'runs'
        self.events = []
        self.approvals = []

    def write(self, relative, content):
        file = self.project / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding='utf-8')
        return file

    def agent(self, *turns, decisions=(), **kwargs):
        choices = iter(decisions)
        self.model = ScriptedModel(*turns)

        def approve(request):
            self.approvals.append(request)
            return (request['argv'] in (CHECK['argv'], OTHER_CHECK['argv'])
                    and request['purpose'] == 'check' and next(choices, False))

        return ProjectAgent(self.project, self.model, runs_root=self.runs, approve_command=approve,
                            on_event=self.events.append, **kwargs)

    def results(self, name):
        return [event['result'] for event in self.events if event['type'] == 'tool_result'
                and event['tool'] == name]

    def test_search_then_rules_are_visible_before_nested_edit(self):
        self.write('AGENTS.md', 'ROOT_RULE 根目录标记')
        self.write('src/AGENTS.md', 'LOCAL_RULE 实现目录标记')
        self.write('other/AGENTS.md', 'SIBLING_RULE 其他目录标记')
        source = self.write('src/code.py', 'value = "测试命中"\n')
        change = {'path': 'src/code.py', 'old': '测试命中', 'new': '已修复'}
        agent = self.agent(
            [('search_text', {'query': '测试命中'})],
            [('read_file', {'path': 'src/code.py'}), ('replace_text', change), ('replace_text', change)],
            [('replace_text', change)], '编辑完成。',
        )
        result = agent.run('定位文本，然后完成修复。')
        self.assertEqual(result['status'], 'completed', result)
        self.assertIn('ROOT_RULE', self.model.calls[0][-1]['content'])
        self.assertNotIn('LOCAL_RULE', self.model.calls[0][-1]['content'])
        self.assertEqual(self.results('search_text')[0]['matches'], [
            {'path': 'src/code.py', 'line': 1, 'text': 'value = "测试命中"'},
        ])
        content, = self.results('read_file')
        self.assertEqual(content['instruction_files'], ['AGENTS.md', 'src/AGENTS.md'])
        self.assertIn('LOCAL_RULE', content['project_instructions'])
        self.assertNotIn('SIBLING_RULE', content['project_instructions'])
        blocked_one, blocked_two, edited = self.results('replace_text')
        for blocked in (blocked_one, blocked_two):
            self.assertEqual(blocked['status'], 'instructions_required')
            self.assertFalse(blocked['ok'])
        self.assertTrue(edited['changed'])
        self.assertEqual(source.read_text(encoding='utf-8'), 'value = "已修复"\n')

    def test_failed_explicit_check_cannot_finish_on_model_claim(self):
        self.write('status.txt', 'broken')
        agent = self.agent([('run_command', CHECK)], '检查完成。', '这里已经完成。',
                           decisions=(True,))
        result = agent.run(GOAL)
        self.assertEqual(result['status'], 'unfinished', result)
        self.assertTrue(result['continuation_available'])
        self.assertIn('重新验证', self.model.calls[-1][-1]['content'])
        verification = result['task_state']['verification']
        self.assertEqual(verification['status'], 'failed')
        self.assertEqual([check['returncode'] for check in verification['checks']], [1])
        checkpoint = Path(result['run_dir']) / 'task.json'
        self.assertEqual(json.loads(checkpoint.read_text(encoding='utf-8'))['status'], 'unfinished')

    def test_unrelated_check_cannot_discharge_a_stale_failure(self):
        self.write('status.txt', 'broken')
        agent = self.agent(
            [('read_file', {'path': 'status.txt'}), ('run_command', CHECK)],
            [('replace_text', REPAIR)], [('run_command', OTHER_CHECK)],
            '另一项检查已通过。', '尚未重跑原验证。', decisions=(True, True),
        )
        result = agent.run(GOAL)
        self.assertEqual(result['status'], 'unfinished', result)
        self.assertEqual(result['task_state']['verification']['status'], 'stale')
        self.assertEqual([row['returncode'] for row in self.results('run_command')], [1, 0])

    def test_resume_is_new_run_and_asks_again_without_replaying_edit(self):
        source = self.write('status.txt', 'broken')
        first = self.agent([('read_file', {'path': 'status.txt'}), ('run_command', CHECK)], [('replace_text', REPAIR)],
                           decisions=(True,), max_steps=2).run(GOAL)
        self.assertEqual(first['status'], 'limit', first)
        self.assertEqual(first['task_state']['verification']['status'], 'stale')
        run = Path(first['run_dir'])
        original_artifacts = {file.name: file.read_bytes() for file in run.iterdir()}
        self.events.clear()
        self.approvals.clear()
        resumed = self.agent(
            [('read_file', {'path': 'status.txt'})], [('run_command', CHECK)], [('run_command', CHECK)],
            '已通过指定验证。', decisions=(False, True),
        ).run('继续验证之前的修复。', resume_from=first['run_dir'])
        self.assertEqual(resumed['status'], 'completed', resumed)
        self.assertNotEqual(resumed['run_dir'], first['run_dir'])
        self.assertEqual(resumed['task_state']['parent_run'], run.name)
        self.assertEqual(resumed['task_state']['task'], GOAL)
        self.assertEqual(resumed['task_state']['verification']['status'], 'passed')
        self.assertEqual(resumed['changes'], [])
        self.assertEqual(self.results('replace_text'), [])
        self.assertEqual(source.read_text(encoding='utf-8'), 'ready')
        self.assertEqual([v['status'] for v in self.results('run_command')], ['denied', 'exited'])
        self.assertEqual([v['argv'] for v in self.approvals], [CHECK['argv'], CHECK['argv']])
        self.assertIn('status.txt', self.model.calls[0][-1]['content'])
        self.assertEqual({file.name: file.read_bytes() for file in run.iterdir()}, original_artifacts)

    def test_compaction_retains_goal_plan_and_early_edit(self):
        for number in range(7):
            self.write(f'fixture-{number}.txt', 'details=' + str(number) * 3000)
        turns = [
            [('update_plan', {'steps': [{'text': '检查七项输入', 'status': 'in_progress'}]}),
             ('write_file', {'path': 'early-edit.txt', 'content': '已编辑'})],
        ]
        turns += [[('read_file', {'path': f'fixture-{number}.txt', 'limit': 2800})]
                  for number in range(7)]
        agent = self.agent(*turns, '目标已处理。', context=10240)
        goal = '写入初始记录并检查七项输入。'
        result = agent.run(goal)
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(any(event['type'] == 'context' for event in self.events))
        compacted = self.model.calls[-1]
        early_edit_call_id = self.model.calls[1][-3]['tool_calls'][1]['id']
        self.assertFalse(any(row.get('tool_call_id') == early_edit_call_id for row in compacted))
        prompt = compacted[1]['content']
        self.assertIn(goal, prompt)
        self.assertIn('检查七项输入', prompt)
        self.assertIn('已改动 early-edit.txt', prompt)
        history = json.loads((Path(result['run_dir']) / 'history.json').read_text(encoding='utf-8'))
        self.assertTrue(any(row.get('tool_call_id') == early_edit_call_id for row in history))

    def test_crash_during_tool_stays_uncertain_until_fresh_check(self):
        self.write('status.txt', 'ready')
        first_agent = self.agent([('run_command', CHECK)])
        with patch.object(first_agent, '_execute', side_effect=RuntimeError('unexpected shutdown')):
            first = first_agent.run(GOAL)
        self.assertEqual(first['status'], 'failed')
        self.assertEqual(first['task_state']['phase'], 'executing')
        resumed = self.agent('已经完成。', '没有其他操作。').run('继续。', resume_from=first['run_dir'])
        self.assertEqual(resumed['status'], 'unfinished', resumed)
        self.assertTrue(resumed['task_state']['uncertain'])
        self.assertIn('尚未验证完成', resumed['answer'])
        checked = self.agent([('run_command', CHECK)], '本次检查已通过。', decisions=(True,)).run(
            '核实当前文件。', resume_from=resumed['run_dir'])
        self.assertEqual(checked['status'], 'completed', checked)
        self.assertFalse(checked['task_state']['uncertain'])
        self.assertEqual(checked['task_state']['verification']['status'], 'passed')
