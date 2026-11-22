"""Regression coverage for the model reasoning panel and its separate storage."""

import sqlite3
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from app_state import PROJECT_RECORD_LIMIT, REASONING_LIMIT, Sessions
from reasoning_view import ReasoningPanel


class ReasoningStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_old_database_preserves_answer_and_uses_per_conversation_indexes(self):
        with sqlite3.connect(self.directory / 'conversations.sqlite3') as db:
            db.executescript('''
                CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, updated TEXT);
                CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT);
                CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO sessions VALUES('old', '历史', '2026-01-01');
                INSERT INTO messages VALUES(4, 'old', 'user', '问题');
                INSERT INTO messages VALUES(6, 'old', 'assistant', '历史答案');
            ''')
        db.close()
        sessions = Sessions(self.directory)
        self.assertEqual(sessions.reasoning('old'), {})
        other = sessions.create()
        sessions.append(other, 'assistant', '第二个会话的答案', reasoning='第二个会话的说明')
        sessions.append('old', 'assistant', '新答案', reasoning='新说明')
        reopened = Sessions(self.directory)
        self.assertEqual(reopened.reasoning('old'), {2: '新说明'})
        self.assertEqual(reopened.reasoning(other), {0: '第二个会话的说明'})
        self.assertEqual(reopened.messages('old'), [
            {'role': 'user', 'content': '问题'},
            {'role': 'assistant', 'content': '历史答案'},
            {'role': 'assistant', 'content': '新答案'},
        ])

    def test_append_rejects_unsupported_reasoning_and_bounds_text(self):
        sessions = Sessions(self.directory)
        identity = sessions.create()
        for role, reasoning in [('user', '用户文本'), ('assistant', {'text': '非字符串'})]:
            with self.subTest(role=role), self.assertRaises(ValueError):
                sessions.append(identity, role, '回复', reasoning=reasoning)
        self.assertEqual(sessions.messages(identity), [])
        sessions.append(identity, 'assistant', '空说明', reasoning=' \n\t')
        message_id = sessions.append(identity, 'assistant', '超长说明', reasoning='说' * (REASONING_LIMIT + 1))
        self.assertEqual(Sessions(self.directory).reasoning(identity), {1: '说' * REASONING_LIMIT})
        with sessions.connect() as db:
            db.execute('DELETE FROM messages WHERE id=?', (message_id,))
            self.assertEqual(db.execute('SELECT count(*) FROM messages_reasoning').fetchone(), (0,))
        self.assertEqual(sessions.reasoning(identity), {})

    def test_reasoning_insert_failure_rolls_back_message(self):
        sessions = Sessions(self.directory)
        identity = sessions.create()
        with sessions.connect() as db:
            db.execute('''CREATE TRIGGER refuse_reasoning BEFORE INSERT ON messages_reasoning BEGIN
                          SELECT RAISE(ABORT, 'test write failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            sessions.append(identity, 'assistant', '答案', reasoning='说明')
        self.assertEqual(sessions.messages(identity), [])
        self.assertEqual(sessions.reasoning(identity), {})

    def test_project_reasoning_is_only_in_card_metadata(self):
        sessions = Sessions(self.directory)
        identity = sessions.create()
        index = sessions.append_project_reply(identity, {'answer': '运行中'}, 'project', '')
        result = {'answer': '任务结果', 'reasoning': '想' * (REASONING_LIMIT + 1)}
        sessions.update_project_reply(identity, index, result, 'project', '工具记录')
        with self.assertRaises(ValueError):
            sessions.update_project_reply(identity, index, {**result, 'reasoning': 1}, 'project', '')
        reopened = Sessions(self.directory)
        self.assertEqual(reopened.messages(identity), [{'role': 'assistant', 'content': '任务结果'}])
        self.assertEqual(reopened.reasoning(identity), {})
        self.assertEqual(reopened.project_runs(identity)[0]['result'], {
            'answer': '任务结果', 'reasoning': '想' * REASONING_LIMIT,
        })

    def test_project_result_near_record_limit_preserves_answer_and_fitting_reasoning(self):
        sessions = Sessions(self.directory)
        identity = sessions.create()
        reasoning = '说明"\\\n' * 60
        result = {'answer': '任务结果', 'reasoning': reasoning}
        activity = '行' * ((PROJECT_RECORD_LIMIT - 146) // 3)
        sessions.append_project_reply(identity, result, 'project', activity)
        record = sessions.project_runs(identity)[0]
        self.assertEqual(record['result']['answer'], result['answer'])
        self.assertEqual(record['activity'], activity)
        saved = record['result']['reasoning']
        self.assertTrue(saved)
        self.assertLess(len(saved), len(reasoning))
        self.assertTrue(reasoning.startswith(saved))
        with sessions.connect() as db:
            encoded = db.execute('SELECT result_json FROM session_project_runs').fetchone()[0]
        size = sum(len(value.encode('utf-8')) for value in (encoded, record['activity'], record['workspace']))
        self.assertLessEqual(size, PROJECT_RECORD_LIMIT)


class ReasoningPanelTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.resize_count = 0
        self.panel = ReasoningPanel(self.root, on_resize=self._count_resize)

    def _count_resize(self):
        self.resize_count += 1

    def test_stream_coalesces_and_panel_can_be_collapsed_then_restored(self):
        self.panel.append('  ')
        self.assertEqual(self.panel.text, '')
        self.assertEqual(self.panel.winfo_manager(), '')
        self.panel.append('<b>原样显示</b>')
        idle_update = self.panel._pending
        self.panel.append('\n继续说明')
        self.assertEqual(self.panel._pending, idle_update)
        self.root.update_idletasks()
        self.assertEqual(self.panel.winfo_manager(), '')
        self.assertEqual(self.panel._display.get('1.0', 'end-1c'), '  <b>原样显示</b>\n继续说明')
        self.assertEqual(self.panel._display.cget('state'), 'disabled')
        self.assertIn('接收中', self.panel._header.cget('text'))
        self.assertEqual(self.resize_count, 1)

        self.panel._header.invoke()
        self.assertFalse(self.panel.expanded)
        self.assertEqual(self.panel._body.winfo_manager(), '')
        self.panel.append('\n流结束')
        self.panel.finish()
        self.assertIsNone(self.panel._pending)
        self.assertIn('已接收', self.panel._header.cget('text'))
        self.panel._header.invoke()
        self.assertTrue(self.panel.expanded)
        self.assertTrue(self.panel._display.get('1.0', 'end-1c').endswith('\n流结束'))
        self.assertEqual(self.resize_count, 3)

    def test_restored_text_is_bounded_and_destroy_cancels_pending_refresh(self):
        self.panel.set_text('思' * (REASONING_LIMIT + 1))
        self.panel.finish()
        self.assertEqual(len(self.panel.text), REASONING_LIMIT)
        self.assertIn('24,000', self.panel._hint.cget('text'))
        self.panel.set_text('新内容')
        idle_update = self.panel._pending
        self.panel.destroy()
        self.assertNotIn(idle_update, self.root.tk.call('after', 'info'))
        self.root.update_idletasks()


if __name__ == '__main__':
    unittest.main()
