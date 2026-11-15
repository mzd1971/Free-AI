"""Checks for scoped project reads on a real filesystem."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import agent_workspace
from agent_workspace import read_instructions, search_text
from project_agent import FILE_LIMIT, ProjectAgent, ToolError


DEVELOPMENT = Path(__file__).resolve().parents[1]


class AgentWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'project'
        self.root.mkdir()
        self.agent = ProjectAgent(
            self.root, type('Model', (), {'_api_key': 'known-agent-key'})(),
            runs_root=self.root / 'runs', approve_command=None,
        )

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode('utf-8') if isinstance(content, str) else content)
        return path

    def test_literal_lines_recursion_and_case(self):
        self.write('src/main.txt', 'nothing\r\n已完成 [a+b] Straße\r\n同一行 [a+b] [a+b]\n')
        self.write('src/nested/second.md', '已完成 [a+b]')
        self.write('out.txt', '[a+b]')

        result = search_text(self.agent, '[a+b]', path='src')
        self.assertEqual(result, {'ok': True, 'truncated': False, 'scanned': 2, 'matches': [
            {'path': 'src/main.txt', 'line': 2, 'text': '已完成 [a+b] Straße'},
            {'path': 'src/main.txt', 'line': 3, 'text': '同一行 [a+b] [a+b]'},
            {'path': 'src/nested/second.md', 'line': 1, 'text': '已完成 [a+b]'},
        ]})
        self.assertEqual(len(search_text(self.agent, 'STRASSE', path='src\\main.txt')['matches']), 1)
        self.assertEqual(search_text(self.agent, 'STRASSE', case_sensitive=True)['matches'], [])
        self.assertEqual(len(search_text(self.agent, '已完成')['matches']), 2)

    def test_match_count_and_output_limits_keep_line_context(self):
        self.write('many.txt', ('ß' * 250 + ' 定位此处 ' + '甲' * 250 + '\n') * 30)
        single = search_text(self.agent, '定位此处', limit=1)
        self.assertTrue(single['truncated'])
        self.assertEqual(len(single['matches']), 1)
        self.assertIn('定位此处', single['matches'][0]['text'])
        self.assertTrue(single['matches'][0]['text'].startswith('…'))
        result = search_text(self.agent, '定位此处', limit=100)
        self.assertTrue(result['truncated'])
        self.assertLess(len(result['matches']), 30)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')),
                             agent_workspace.SEARCH_OUTPUT)

    def test_entry_file_and_total_byte_budgets(self):
        self.write('a.txt', 'same')
        self.write('b.txt', 'same')
        self.write('c.txt', 'same')
        for limit, value in (('SEARCH_ENTRIES', 1), ('SEARCH_FILES', 1),
                             ('SEARCH_BYTES', FILE_LIMIT)):
            with self.subTest(limit=limit), patch.object(agent_workspace, limit, value):
                result = search_text(self.agent, 'same')
                self.assertTrue(result['truncated'])
                self.assertEqual(result['scanned'], 1)
                self.assertEqual(len(result['matches']), 1)

    def test_cancellation_stops_directory_scan_and_line_matching(self):
        self.write('a.txt', 'same\n' * 100)
        self.agent.cancel.set()
        stopped = search_text(self.agent, 'same')
        self.assertEqual(stopped, {'ok': False, 'status': 'cancelled', 'truncated': True,
                                   'matches': [], 'scanned': 0})
        self.agent.cancel.clear()
        snippet = agent_workspace._snippet

        def cancel_on_match(*args):
            self.agent.cancel.set()
            return snippet(*args)

        with patch.object(agent_workspace, '_snippet', side_effect=cancel_on_match):
            stopped = search_text(self.agent, 'same')
        self.assertEqual(stopped['status'], 'cancelled')
        self.assertEqual(len(stopped['matches']), 1)

    def test_excludes_private_ignored_binary_and_hardlinks(self):
        for relative, text in (
            ('allowed.txt', 'match'), ('.env.local', 'match'), ('secret.pem', 'match'),
            ('credentials/config.txt', 'match'), ('.git/config', 'match'),
            ('node_modules/pkg.txt', 'match'), ('runs/current/log.txt', 'match'),
            ('.freeai-edit', 'match'), ('image.png', 'match'), ('null.txt', b'\x00match'),
            ('non-utf8.txt', b'\xffmatch'), ('over-limit.txt', b'match' + b'x' * FILE_LIMIT),
            ('api.txt', 'match known-agent-key'),
            ('key.txt', '-----BEGIN OPENSSH PRIVATE KEY-----\nmatch'),
        ):
            self.write(relative, text)
        outside = self.base / 'secret.txt'
        outside.write_text('match', encoding='utf-8')
        os.link(outside, self.root / 'linked.txt')
        self.assertEqual(search_text(self.agent, 'match')['matches'],
                         [{'path': 'allowed.txt', 'line': 1, 'text': 'match'}])
        for path in ('.env.local', 'runs', 'linked.txt', 'node_modules', 'missing.txt'):
            with self.subTest(path=path), self.assertRaises(ToolError):
                search_text(self.agent, 'match', path=path)

    def test_junction_or_symbolic_link_is_not_traversed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'AGENTS.md').write_text('outside instruction', encoding='utf-8')
        (outside / 'source.txt').write_text('needle', encoding='utf-8')
        link = self.root / 'linked'
        if os.name == 'nt':
            result = subprocess.run(
                ['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                cwd=self.base, capture_output=True,
            )
            if result.returncode:
                self.skipTest('Windows junction unavailable')
            self.addCleanup(link.rmdir)
        else:
            link.symlink_to(outside, target_is_directory=True)
            self.addCleanup(link.unlink)
        self.assertEqual(search_text(self.agent, 'needle')['matches'], [])
        with self.assertRaises(ToolError):
            search_text(self.agent, 'needle', path='linked/source.txt')
        with self.assertRaises(ToolError):
            read_instructions(self.agent, 'linked/source.txt')

    def test_instruction_scope_is_root_to_nearest_ancestor(self):
        (self.base / 'AGENTS.md').write_text('个人目录守则', encoding='utf-8')
        self.write('AGENTS.md', '\ufeff项目级约定，使用 A')
        self.write('src/AGENTS.md', '子目录使用 B')
        self.write('src/task/AGENTS.md', '此子目录使用 C')
        self.write('src/sibling/AGENTS.md', '同级目录的约定')
        self.write('src/task/example.py', '')

        scoped = read_instructions(self.agent, 'src/task/example.py')
        self.assertEqual(scoped['files'], ['AGENTS.md', 'src/AGENTS.md', 'src/task/AGENTS.md'])
        self.assertEqual(scoped['context'], '### AGENTS.md\n项目级约定，使用 A\n\n'
                         '### src/AGENTS.md\n子目录使用 B\n\n'
                         '### src/task/AGENTS.md\n此子目录使用 C')
        self.assertEqual(scoped['warnings'], [])
        self.assertEqual(scoped, read_instructions(self.agent, 'src/task'))
        self.assertEqual(scoped, read_instructions(self.agent, 'src/task/new.py'))
        self.assertEqual(read_instructions(self.agent)['files'], ['AGENTS.md'])

    def test_instruction_errors_and_utf8_budget(self):
        self.write('AGENTS.md', '根规则' * 120)
        self.write('src/AGENTS.md', '子规则' * 120)
        self.write('src/task/AGENTS.md', b'\xff\xff')
        self.write('src/task/actual.py', '')

        result = read_instructions(self.agent, 'src/task/actual.py', budget=130)
        self.assertEqual(result['files'], ['AGENTS.md', 'src/AGENTS.md'])
        self.assertLessEqual(len(result['context'].encode('utf-8')), 130)
        self.assertIn('根规则', result['context'])
        self.assertIn('子规则', result['context'])
        self.assertEqual(len(result['warnings']), 2)
        no_budget = read_instructions(self.agent, 'src/task/actual.py', budget=0)
        self.assertEqual(no_budget, {'context': '', 'files': [], 'warnings': []})

    def test_instruction_hardlink_and_key_are_rejected(self):
        outside = self.base / 'AGENTS.md'
        outside.write_text('attached instruction', encoding='utf-8')
        os.link(outside, self.root / 'AGENTS.md')
        self.write('src/AGENTS.md', 'known-agent-key')
        result = read_instructions(self.agent, 'src/new.txt')
        self.assertEqual(result['files'], [])
        self.assertEqual(result['context'], '')
        self.assertEqual(len(result['warnings']), 1)
        self.assertIn('2', result['warnings'][0])

    def test_invalid_arguments(self):
        search_arguments = [
            {'query': value} for value in ('', None, 'x' * 161, 'x\ny', 'x\x00y', '\ud800')
        ] + [
            {'query': 'x', 'path': value} for value in ('../outside.txt', None, '/abs', 'src/./txt')
        ] + [
            {'query': 'x', 'limit': True}, {'query': 'x', 'limit': 101},
            {'query': 'x', 'case_sensitive': 'false'},
        ]
        for arguments in search_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                search_text(self.agent, **arguments)
        for arguments in ({'target': '../AGENTS.md'}, {'target': 'secrets/file.txt'},
                          {'budget': -1}, {'budget': True}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                read_instructions(self.agent, **arguments)
