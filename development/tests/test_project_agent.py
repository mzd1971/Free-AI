"""Project-agent unit and subprocess integration checks."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import project_agent
from project_agent import FILE_LIMIT, OUTPUT_LIMIT, ProjectAgent, ToolError, rollback_run
from remote_api import APIError


DEVELOPMENT = Path(__file__).resolve().parents[1]


class ScriptedModel:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []
        self._api_key = 'project-test-model-key'
        self.expected_tools = {'list_files', 'search_text', 'read_file', 'write_file', 'replace_text', 'run_command', 'update_plan'}

    def complete_tools(self, messages, tools, *, cancel_event, context):
        self.calls.append(messages[:])
        pending = set()
        for row in messages:
            if row['role'] == 'assistant':
                assert not pending
                pending = {call['id'] for call in row.get('tool_calls', [])}
            elif row['role'] == 'tool':
                pending.remove(row['tool_call_id'])
        assert not pending
        assert {entry['function']['name'] for entry in tools} == self.expected_tools
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, str):
            return {'role': 'assistant', 'content': turn}
        tool_calls = []
        for index, (name, arguments) in enumerate(turn):
            tool_calls.append({
                'id': f'call_{len(self.calls)}_{index}', 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)},
            })
        return {'role': 'assistant', 'content': None, 'tool_calls': tool_calls}


class ProjectAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.runs = self.root / 'runs'
        self.events = []
        self.requests = []

    def start(self, *turns, **kwargs):
        self.model = ScriptedModel(*turns)
        if kwargs.get('personal_memory') is not None and kwargs.get('write_memory', True):
            self.model.expected_tools.add('remember_memory')
        history = kwargs.pop('history', None)

        def approve(request):
            self.requests.append(request)
            return True

        self.agent = ProjectAgent(
            self.project, self.model, runs_root=self.runs, on_event=self.events.append,
            approve_command=kwargs.pop('approve_command', approve), **kwargs,
        )
        return self.agent.run('完成并核验项目任务。', history=history)

    def results(self, tool):
        return [event['result'] for event in self.events
                if event['type'] == 'tool_result' and event['tool'] == tool]

    def test_personal_memory_writes_and_reads_across_runs(self):
        from agent_memory import AgentMemoryStore

        database = self.root / 'agent-memory.sqlite3'
        store = AgentMemoryStore(database)
        item = {'title': '项目任务', 'content': '项目任务使用贴近现有代码的最小修改。',
                'kind': 'note', 'scope': 'project'}
        result = self.start([('remember_memory', item)], '已保存记忆。',
                            personal_memory=store, agent_id='author', allow_write=False)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['changes'], [])
        entry, = result['memory_updates']
        self.assertEqual(entry['title'], item['title'])
        self.assertEqual(entry['status'], 'active')
        self.assertEqual(set(entry), {'id', 'title', 'status'})
        self.assertFalse(self.requests)
        sibling = self.root / 'other-project'
        sibling.mkdir()
        other = store.before_call('项目任务', agent_id='author', workspace=str(sibling))
        self.assertNotIn(entry['id'], other['memory_ids'])
        result = self.start('已读取记忆。', personal_memory=AgentMemoryStore(database),
                            agent_id='author', write_memory=False)
        prompt = self.model.calls[0][-1]['content']
        self.assertIn('长期记忆资料', prompt)
        self.assertIn(item['content'], prompt)
        self.assertTrue(any(event.get('memory_ids') == [entry['id']] for event in self.events))
        self.assertEqual(result['memory_ids'], [entry['id']])
        persisted = json.loads((Path(result['run_dir']) / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(persisted['memory_ids'], [entry['id']])
        self.assertNotIn('memory_updates', result)

        store.save_user('项目任务', '用户确认的项目约定。', scope='project',
                        agent_id='author', workspace=str(self.project))
        result = self.start([('remember_memory', item)], '已核对现有记忆。',
                            personal_memory=store, agent_id='author', read_memory=False)
        self.assertNotIn('memory_updates', result)
        self.assertEqual(self.results('remember_memory')[-1]['status'], 'skipped')
        kept = store.before_call('项目任务', agent_id='author', workspace=str(self.project))
        self.assertIn('用户确认的项目约定。', kept['context'])
        self.assertNotIn(item['content'], kept['context'])

    def test_personal_memory_ids_exclude_context_dropped_before_generation(self):
        from agent_memory import AgentMemoryStore

        store = AgentMemoryStore(self.root / 'memory')
        for index in range(4):
            store.save_user(f'输出格式 {index}', '用于检测上下文预算的条目。' * 45, kind='preference')
        self.start('普通作答。', personal_memory=store, write_memory=False)
        first = self.model.calls[0]
        prefix = first[-1]['content'].split('\n长期记忆资料：')[0]
        budget = len(project_agent._json([first[0], dict(first[-1], content=prefix)]).encode()) + 256
        self.events.clear()
        with patch.object(ProjectAgent, '_history_budget', return_value=budget):
            result = self.start('普通作答。', personal_memory=store, write_memory=False)
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('长期记忆资料：', self.model.calls[0][-1]['content'])
        self.assertNotIn('memory_ids', result)
        self.assertFalse(any(event.get('memory_ids') for event in self.events))

    def test_duplicate_and_protected_memory_do_not_announce_writes(self):
        from agent_memory import AgentMemoryStore

        store = AgentMemoryStore(self.root / 'memory')
        item = {'title': '部署约定', 'content': '先运行测试。', 'kind': 'procedure'}
        original = store.remember(**item)
        result = self.start([('remember_memory', item), ('remember_memory', {**item, 'title': '发布方法'})],
                            '已核对。', personal_memory=store, read_memory=False)
        self.assertNotIn('memory_updates', result)
        self.assertEqual([row['status'] for row in self.results('remember_memory')], ['already_saved'] * 2)
        verified = store.verify(original['id'])
        result = self.start([('remember_memory', {**item, 'content': '执行其他步骤。'})], '已核对。',
                            personal_memory=store, read_memory=False)
        self.assertNotIn('memory_updates', result)
        self.assertEqual(self.results('remember_memory')[-1]['status'], 'skipped')
        self.assertEqual(store.get(original['id']), verified)

    def test_personal_memory_permissions_validation_and_write_limit(self):
        class Store:
            def __init__(self):
                self.writes = []
                self.reads = 0

            def before_call(self, *args, **kwargs):
                self.reads += 1
                return {'context': '不应显示', 'memory_ids': ['unexpected']}

            def remember(self, title, content, **fields):
                if fields['kind'] == 'invalid':
                    raise ValueError('invalid fixture kind')
                self.writes.append(fields)
                return {'id': str(len(self.writes)), 'title': title, 'status': 'pending', 'source': 'agent'}, True

        store = Store()
        item = {'title': '条目', 'content': '记忆内容', 'scope': 'project'}
        result = self.start([
            ('remember_memory', {**item, 'source': 'manual'}),
            ('remember_memory', {**item, 'agent_id': 'other'}),
            ('remember_memory', {**item, 'workspace': str(self.root)}),
            ('remember_memory', {**item, 'scope': 'global'}),
            ('remember_memory', {**item, 'kind': 'invalid'}),
            *[('remember_memory', {**item, 'title': str(i)}) for i in range(4)],
        ], '仍可完成。', personal_memory=store, agent_id='owner', read_memory=False, memory_pending=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['changes'], [])
        self.assertEqual(len(result['memory_updates']), 3)
        self.assertEqual([item['ok'] for item in self.results('remember_memory')],
                         [False] * 5 + [True] * 3 + [False])
        self.assertTrue(all(fields == {'agent_id': 'owner', 'scope': 'project', 'kind': 'note',
                                      'workspace': str(self.project.resolve()), 'pending': True, 'return_changed': True,
                                      'evidence': '项目运行 ' + Path(result['run_dir']).name}
                            for fields in store.writes))
        self.assertTrue(all('长期记忆资料' not in call[0]['content'] for call in self.model.calls))
        self.assertEqual(len(store.writes), 3)
        self.assertEqual(store.reads, 0)

    def test_project_loop_edit_verify_and_rollback(self):
        source = self.project / 'hello.py'
        source.write_bytes(b"print('old')\r\n")
        result = self.start(
            [('update_plan', {'steps': ['读取', '修改和验证']}),
             ('list_files', {}), ('read_file', {'path': 'hello.py'})],
            [('replace_text', {'path': 'hello.py', 'old': "print('old')", 'new': "print('middle')"}),
             ('replace_text', {'path': 'hello.py', 'old': "print('middle')\n", 'new': "print('new')\n"}),
             ('write_file', {'path': 'sub/new.txt', 'content': '新文件'})],
            [('run_command', {'argv': [sys.executable, 'hello.py'], 'purpose': 'check'}),
             ('update_plan', {'steps': [{'text': step, 'status': 'completed'}
                                        for step in ('读取', '修改和验证')]})],
            '已修改并执行脚本。',
        )
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(source.read_bytes(), b"print('new')\r\n")
        self.assertEqual(self.requests[0]['cwd'], str(self.project))
        self.assertEqual(self.results('run_command')[0]['stdout'].strip(), 'new')
        self.assertEqual(len(result['changes']), 3)
        self.assertEqual(len(self.model.calls), 4)
        run_dir = Path(result['run_dir'])
        self.assertEqual(json.loads((run_dir / 'result.json').read_text(encoding='utf-8')), result)
        self.assertGreater(len((run_dir / 'events.jsonl').read_text(encoding='utf-8')), 0)
        rollback = rollback_run(run_dir, self.project)
        self.assertCountEqual(rollback['restored'], ['hello.py', 'sub/new.txt'])
        self.assertEqual(rollback['conflicts'], [])
        self.assertEqual(source.read_bytes(), b"print('old')\r\n")
        self.assertFalse((self.project / 'sub').exists())

    def test_command_denied_then_approved_separately(self):
        decisions = iter([False, True])

        def approve(request):
            self.requests.append(request)
            return next(decisions)

        script = 'from pathlib import Path; Path("created.txt").write_text("safe")'
        args = {'argv': [sys.executable, '-c', script]}
        self.start([('run_command', args)], [('run_command', args)], '结束。', approve_command=approve)
        results = self.results('run_command')
        self.assertEqual(results[0]['status'], 'denied')
        self.assertEqual(results[1]['status'], 'exited')
        self.assertEqual(len(self.requests), 2)
        self.assertEqual((self.project / 'created.txt').read_text(), 'safe')

    def test_command_absolute_directory_is_scoped(self):
        child = self.project / 'in project'
        child.mkdir()
        argv = [sys.executable, '-c', 'import os;print(os.getcwd())']
        self.start(
            [('run_command', {'argv': argv, 'cwd': str(self.project)}),
             ('run_command', {'argv': argv, 'cwd': str(child)}),
             ('run_command', {'argv': argv, 'cwd': str(self.root)}),
             ('run_command', {'argv': argv, 'cwd': str(child / '..')})],
            '结束。',
        )
        observed = self.results('run_command')
        self.assertEqual([request['cwd'] for request in self.requests], [str(self.project), str(child)])
        self.assertEqual([entry.get('status') for entry in observed[:2]], ['exited', 'exited'])
        self.assertIn('工作区外', observed[2]['message'])
        self.assertIn('不能包含 ..', observed[3]['message'])

    def test_file_confinement(self):
        (self.root / 'outside.txt').write_text('outside')
        cases = ['../outside.txt', str(self.root / 'outside.txt'), 'C:outside.txt',
                 'outside.txt:alternate', 'SECRE~1/config', '.git/config', '.env.local', 'secrets/api.txt',
                 'nested/id_ed25519', 'nested/example.pem']
        (self.project / 'source.txt').write_text('okay')
        (self.project / '.env.local').write_text('token')
        (self.project / 'data').mkdir()
        (self.project / 'data' / 'source.txt').write_text('inside')
        (self.project / 'binary').write_bytes(b'\x00\x01')
        (self.project / 'large.txt').write_bytes(b'A' * (FILE_LIMIT + 1))
        (self.project / 'node_modules').mkdir()
        (self.project / 'node_modules' / 'ignored.txt').write_text('ignored')
        (self.project / 'private.txt').write_text('-----BEGIN OPENSSH PRIVATE KEY-----')
        result = self.start(
            [('read_file', {'path': name}) for name in cases + ['large.txt', 'binary', 'private.txt']]
            + [('list_files', {}), ('unknown', {})],
            [('write_file', {'path': '../outside.txt', 'content': 'oops'}),
             ('run_command', {'argv': [sys.executable, '-V'], 'cwd': '..'})],
            '结束。',
        )
        self.assertEqual(result['changes'], [])
        self.assertTrue(all(not entry['ok'] for entry in self.results('read_file')))
        self.assertEqual(self.results('list_files')[0]['files'], ['source.txt', 'data/source.txt'])
        self.assertFalse(self.results('unknown')[0]['ok'])
        self.assertFalse(self.requests)
        self.assertEqual((self.root / 'outside.txt').read_text(), 'outside')

    def test_link_and_linked_parent_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('outside')
        link = self.project / 'linked'
        if os.name == 'nt':
            created = subprocess.run(
                ['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                cwd=self.root, capture_output=True,
            )
            if created.returncode != 0:
                self.skipTest('Windows junction unavailable')
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            outcome = self.start(
                [('read_file', {'path': 'linked/secret.txt'}),
                 ('write_file', {'path': 'linked/new.txt', 'content': 'should fail'}),
                 ('run_command', {'argv': [sys.executable, '-V'], 'cwd': 'linked'}),
                 ('run_command', {'argv': [sys.executable, '-V'], 'cwd': str(link)}),
                 ('list_files', {})],
                '结束。',
            )
            self.assertEqual(outcome['changes'], [])
            self.assertTrue(all(not self.results(tool)[0]['ok']
                                for tool in ('read_file', 'write_file', 'run_command')))
            self.assertEqual(self.results('list_files')[0]['files'], [])
            self.assertEqual(len(self.results('run_command')), 2)
            self.assertTrue(all(not item['ok'] for item in self.results('run_command')))
            self.assertFalse(self.requests)
            self.assertFalse((outside / 'new.txt').exists())
        finally:
            link.rmdir() if os.name == 'nt' else link.unlink()

    def test_read_paging_on_utf8_boundaries(self):
        contents = ('甲"\\\n' * 1500).encode('utf-8')
        (self.project / 'large.txt').write_bytes(contents)
        self.start([('read_file', {'path': 'large.txt', 'offset': 0, 'limit': 8192}),
                    ('read_file', {'path': 'large.txt', 'offset': 1, 'limit': 8192})], '结束。')
        result = self.results('read_file')[0]
        model_view = json.loads(self.model.calls[-1][-2]['content'])
        self.assertEqual(result['content'].encode('utf-8'), contents[:result['next_offset']])
        self.assertEqual(model_view['content'].encode('utf-8'), contents[:model_view['next_offset']])
        self.assertFalse(self.results('read_file')[1]['ok'])

    def test_rollback_conflicts_and_tampered_manifest(self):
        source = self.project / 'file.txt'
        source.write_text('original')
        result = self.start([('read_file', {'path': 'file.txt'})],
                            [('write_file', {'path': 'file.txt', 'content': 'edited'})], '结束。')
        run_dir = Path(result['run_dir'])
        source.write_text('new user edit')
        self.assertEqual(rollback_run(run_dir, self.project), {'restored': [], 'conflicts': ['file.txt']})
        self.assertEqual(source.read_text(), 'new user edit')
        with self.assertRaises(ToolError):
            rollback_run(run_dir, self.root)
        record = run_dir / 'changes.json'
        manifest = json.loads(record.read_text(encoding='utf-8'))
        manifest['changes'][0]['backup'] = '../user-file.txt'
        record.write_text(json.dumps(manifest), encoding='utf-8')
        source.write_text('edited')
        with self.assertRaises(ToolError):
            rollback_run(run_dir, self.project)
        self.assertEqual(source.read_text(), 'edited')

    def test_command_cancel_kills_descendants(self):
        cancellation = threading.Event()
        child = "import time; from pathlib import Path; time.sleep(2); Path('child-survived').touch()"
        script = (
            "import subprocess,sys,time; from pathlib import Path; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
            "Path('child-spawned').touch(); print('ready',flush=True); time.sleep(30)"
        )

        def cancel_after_spawn():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (self.project / 'child-spawned').exists():
                time.sleep(0.02)
            cancellation.set()

        watcher = threading.Thread(target=cancel_after_spawn, daemon=True)
        watcher.start()
        start = time.monotonic()
        result = self.start([('run_command', {'argv': [sys.executable, '-c', script]})],
                            cancel_event=cancellation)
        elapsed = time.monotonic() - start
        watcher.join(timeout=1)
        self.assertTrue((self.project / 'child-spawned').exists())
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(self.results('run_command')[0]['status'], 'cancelled')
        self.assertLess(elapsed, 4)
        time.sleep(2.1)
        self.assertFalse((self.project / 'child-survived').exists())

    def test_large_stdout_stderr_timeout_and_sanitized_environment(self):
        argv = [sys.executable, '-c', (
            'import os,sys,time; '
            'assert "OPENAI_API_KEY" not in os.environ; '
            f'sys.stdout.buffer.write(b"a" * {OUTPUT_LIMIT * 3}); sys.stdout.flush(); '
            f'sys.stderr.buffer.write(b"b" * {OUTPUT_LIMIT * 3}); sys.stderr.flush(); '
            'time.sleep(20)'
        )]
        previous = os.environ.get('OPENAI_API_KEY')
        os.environ['OPENAI_API_KEY'] = 'fake-environment-key'
        self.addCleanup(os.environ.pop if previous is None else os.environ.__setitem__,
                        'OPENAI_API_KEY', None if previous is None else previous)
        start = time.monotonic()
        self.start([('run_command', {'argv': argv, 'timeout': 1})], '超时。')
        self.assertLess(time.monotonic() - start, 4)
        observed = self.results('run_command')[0]
        self.assertEqual(observed['status'], 'timeout')
        self.assertEqual(len(observed['stdout']), OUTPUT_LIMIT)
        self.assertEqual(len(observed['stderr']), OUTPUT_LIMIT)
        self.assertTrue(observed['stdout_truncated'])
        self.assertTrue(observed['stderr_truncated'])

    def test_step_budget_and_no_progress(self):
        repeated = [('read_file', {'path': 'missing.txt'})]
        result = self.start(repeated, max_steps=1)
        self.assertEqual(result['status'], 'limit')
        self.assertEqual(len(self.model.calls), 1)
        self.events.clear()
        result = self.start(repeated, repeated, repeated)
        self.assertEqual(result['status'], 'limit')
        self.assertEqual(len(self.model.calls), 3)

    def test_api_failure_and_write_disabled(self):
        result = self.start(
            [('write_file', {'path': 'disabled.txt', 'content': 'hi'}),
             ('write_file', {'path': 'credential.txt', 'content': 'project-test-model-key'})],
            APIError('API 服务不可用。'), allow_write=False,
        )
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['answer'], 'API 服务不可用。')
        self.assertEqual(result['changes'], [])
        for file in Path(result['run_dir']).glob('*.json*'):
            self.assertNotIn('project-test-model-key', file.read_text(encoding='utf-8'))


    def test_context_budget_rejects_batch_before_writing(self):
        result = self.start(
            [('write_file', {'path': 'small.txt', 'content': 'small'}),
             ('write_file', {'path': 'large.txt', 'content': 'z' * 9000})],
            context=8192,
        )
        self.assertEqual(result['status'], 'limit')
        self.assertFalse(list(self.project.iterdir()))
        self.assertEqual(len(self.results('write_file')), 2)

    def test_context_budget_shares_space_across_batch_and_retains_full_results(self):
        (self.project / 'sample.txt').write_text('abcd' * 2000)
        self.start([('read_file', {'path': 'sample.txt'})] * 16, '结束。', context=16384)
        self.assertEqual(len(self.model.calls), 2)
        history = self.model.calls[-1]
        self.assertLessEqual(len(project_agent._json(history).encode('utf-8')), self.agent._history_budget())
        first_result = json.loads(history[-16]['content'])
        self.assertEqual(first_result['next_offset'], len(first_result['content']))
        self.assertEqual(len(self.results('read_file')[0]['content']), 2600)

    def test_previous_chat_passes_only_complete_turns(self):
        history = [
            {'role': 'system', 'content': '跳过系统信息'},
            {'role': 'assistant', 'content': '跳过孤立回答'},
            {'role': 'user', 'content': '之前的完整问题'},
            {'role': 'assistant', 'content': '之前的完整回答', 'reasoning_content': '跳过推理'},
            {'role': 'user', 'content': '跳过中断问题'},
            {'role': 'tool', 'content': '跳过工具结果'},
            {'role': 'assistant', 'content': '跳过中断回答'},
            {'role': 'user', 'content': '跳过尾部问题'},
        ]
        result = self.start([('list_files', {})], '继续。', history=history)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.model.calls[0][1:], [
            {'role': 'user', 'content': '之前的完整问题'},
            {'role': 'assistant', 'content': '之前的完整回答'},
            {'role': 'user', 'content': '完成并核验项目任务。'},
        ])
        self.assertEqual(self.model.calls[1][1:4], self.model.calls[0][1:])
        self.assertEqual(self.model.calls[1][-1]['role'], 'tool')

    def test_old_history_crops_and_new_chat_yields_to_latest_tool_batch(self):
        history = [
            {'role': 'user', 'content': '旧问题' + 'x' * 6000},
            {'role': 'assistant', 'content': '旧回答'},
            {'role': 'user', 'content': '新问题' + 'y' * 3000},
            {'role': 'assistant', 'content': '新回答'},
        ]
        result = self.start(
            [('write_file', {'path': 'new.txt', 'content': 'x' * 2100})],
            '完成。', history=history, context=10240,
        )
        self.assertEqual(result['status'], 'completed')
        first, second = self.model.calls
        self.assertEqual(first[1:3], history[-2:])
        self.assertEqual(first[-1], {'role': 'user', 'content': '完成并核验项目任务。'})
        self.assertEqual(second[1], first[-1])
        self.assertEqual(second[-1]['role'], 'tool')

    def test_memory_recorded_without_retrieval(self):
        class Knowledge:
            def __init__(self):
                self.retrieved = False
                self.observation = None

            def before_call(self, goal):
                self.retrieved = True
                return {'context': 'OLD', 'episode_ids': ['old']}

            def record(self, goal, answer, **fields):
                self.observation = fields
                return 'episode-id'

        store = Knowledge()
        result = self.start('模型声称已经验证。', knowledge=store, use_memory=False)
        self.assertFalse(store.retrieved)
        self.assertEqual(result['episode_id'], 'episode-id')
        self.assertEqual(store.observation['outcome'], 'pending')
        self.assertEqual(store.observation['verification'], 'unverified')

    def test_final_logging_failure_preserves_run_and_changes(self):
        original = project_agent._atomic_json

        def fail_on_result(path, value):
            if path.name == 'result.json':
                raise OSError('simulated full disk')
            return original(path, value)

        with patch.object(project_agent, '_atomic_json', side_effect=fail_on_result):
            result = self.start([('write_file', {'path': 'file.txt', 'content': 'content'})], '完成。')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['changes']), 1)
        self.assertTrue(Path(result['run_dir']).is_dir())
        self.assertEqual(rollback_run(result['run_dir'], self.project)['restored'], ['file.txt'])

    @unittest.skipUnless(os.name == 'nt', 'Windows directory handle sharing')
    def test_file_write_pins_project_directories_against_rename(self):
        parent = self.project / 'child'
        parent.mkdir()
        actual_atomic = project_agent._atomic_bytes

        def try_relocate(path, data, **kwargs):
            if path == parent / 'file.txt':
                with self.assertRaises(OSError) as error:
                    parent.rename(self.root / 'moved')
                self.assertIn(error.exception.winerror, (5, 32))
                with self.assertRaises(OSError) as error:
                    self.project.rename(self.root / 'moved_project')
                self.assertIn(error.exception.winerror, (5, 32))
            return actual_atomic(path, data, **kwargs)

        with patch.object(project_agent, '_atomic_bytes', side_effect=try_relocate):
            result = self.start([('write_file', {'path': 'child/file.txt', 'content': 'content'})], '完成。')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((parent / 'file.txt').read_text(), 'content')


if __name__ == '__main__':
    unittest.main()
