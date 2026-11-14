"""Session storage regression checks for integrated Project mode."""

from pathlib import Path
import sqlite3
import tempfile
import unittest

from app_state import Sessions


DEVELOPMENT = Path(__file__).resolve().parents[1]


class ProjectSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_existing_database_migrates_without_rewriting_messages(self):
        database = self.root / 'conversations.sqlite3'
        db = sqlite3.connect(database)
        try:
            with db:
                db.executescript("""
                    CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, updated TEXT);
                    CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT);
                    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
                    INSERT INTO sessions VALUES('old', '旧对话', '2026-01-01');
                    INSERT INTO messages VALUES(5, 'old', 'user', '原始消息');
                    INSERT INTO settings VALUES('font', '"大"');
                """)
        finally:
            db.close()
        sessions = Sessions(self.root)
        self.assertEqual(sessions.messages('old'), [{'role': 'user', 'content': '原始消息'}])
        self.assertEqual(sessions.get('font'), '大')
        self.assertEqual(sessions.project_settings('old'), {})
        self.assertEqual(sessions.project_runs('old'), [])
        self.assertEqual(sessions.append('old', 'assistant', '回复'), 6)

    def test_project_preferences_are_per_session_and_preserve_existing_keys(self):
        sessions = Sessions(self.root)
        first, second = sessions.create(), sessions.create()
        sessions.set_project_settings(first, {'enabled': True, 'allow_write': True, 'workspace': 'D:/old'})
        sessions.set_project_settings(first, {'workspace': 'D:/new', 'use_memory': False})
        reopened = Sessions(self.root)
        self.assertEqual(reopened.project_settings(first), {
            'enabled': True, 'allow_write': True, 'workspace': 'D:/new', 'use_memory': False,
        })
        self.assertEqual(reopened.project_settings(second), {})
        for invalid in [{'allow_write': 1}, {'enabled': 'true'}, {'api_key': 'placeholder'}, []]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sessions.set_project_settings(first, invalid)
        with self.assertRaises(ValueError):
            sessions.set_project_settings('missing', {'enabled': True})

    def test_public_run_metadata_loads_at_assistant_message_index(self):
        sessions = Sessions(self.root)
        first, second = sessions.create(), sessions.create()
        sessions.append(first, 'user', '任务')
        sessions.append(second, 'assistant', '独立消息')
        sessions.append(first, 'assistant', '结果')
        public = {'status': 'completed', 'answer': '结果', 'run_dir': 'D:/runs/first',
                  'changes': [{'path': 'main.py'}], 'episode_id': None}
        sessions.save_project_run(
            first, 1, {**public, 'request_headers': {'Authorization': 'placeholder'}}, 'D:/project', '运行活动',
        )
        reopened = Sessions(self.root)
        self.assertEqual(reopened.project_runs(first), [{
            'assistant_index': 1, 'result': public, 'workspace': 'D:/project', 'activity': '运行活动',
        }])
        self.assertEqual(reopened.project_runs(second), [])
        for index in [0, 2, -1, True]:
            with self.subTest(index=index), self.assertRaises(ValueError):
                sessions.save_project_run(first, index, public, 'D:/project', '')
        with self.assertRaises(ValueError):
            sessions.save_project_run(first, 1, public, 'D:/project', '广' * (512 * 1024 // 3))
        with self.assertRaises(ValueError):
            sessions.save_project_run('missing', 1, public, 'D:/project', '')


if __name__ == '__main__':
    unittest.main()
