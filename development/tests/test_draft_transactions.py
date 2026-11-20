"""Exercise draft consumption across two clients sharing one SQLite database."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app_state import ACTIVE_DRAFT_SESSION, Sessions


class DraftTransactions(unittest.TestCase):
    def setUp(self):
        data = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(data.cleanup)
        self.first = Sessions(data.name)
        self.second = Sessions(data.name)
        self.chat = self.first.create()

    def send(self, owner, text='sent'):
        return self.first.append(self.chat, 'user', text, draft_from=owner)

    def test_later_draft_survives_send_for_new_and_existing_chat(self):
        for owner in (None, self.chat):
            with self.subTest(owner=owner):
                self.first.save_draft(owner, 'first')
                self.second.save_draft(owner, 'second')
                self.send(owner)
                self.assertEqual(self.second.draft(owner), ('second', None, None, None))

    def test_later_attachment_survives_even_if_text_matches(self):
        for changed in (('new.png', None, None), (None, 'a.csv', 'csv'), (None, 'b.csv', 'summary')):
            with self.subTest(attachments=changed):
                self.second.save_draft(self.chat, 'same', None, 'b.csv', 'csv')
                self.first.draft(self.chat)
                self.second.save_draft(self.chat, 'same', *changed)
                self.send(self.chat)
                self.assertEqual(self.second.draft(self.chat), ('same', *changed))

    def test_unknown_and_seen_empty_drafts_do_not_consume_another_windows_text(self):
        self.second.save_draft(None, 'unknown')
        self.send(None)
        self.assertEqual(self.second.draft(None)[0], 'unknown')
        self.assertEqual(self.first.draft(self.chat), ('', None, None, None))
        self.second.save_draft(self.chat, 'new')
        self.send(self.chat)
        self.assertEqual(self.second.draft(self.chat)[0], 'new')

    def test_send_consumes_known_draft_even_after_last_minute_edit(self):
        self.first.save_draft(None, 'saved', 'image.png')
        self.send(None, 'sent after edit')
        self.assertEqual(self.second.draft(None), ('', None, None, None))
        self.second.save_draft(self.chat, 'restored')
        self.first.draft(self.chat)
        self.send(self.chat, 'restored')
        self.assertEqual(self.second.draft(self.chat), ('', None, None, None))
        self.assertEqual([row['content'] for row in self.second.messages(self.chat)], ['sent after edit', 'restored'])
        self.assertEqual(self.second.get(ACTIVE_DRAFT_SESSION), self.chat)

    def test_rollback_preserves_message_transaction_and_allows_retry(self):
        self.first.save_draft(None, 'saved')
        with self.second.connect() as db:
            db.execute("""CREATE TRIGGER refuse_send BEFORE INSERT ON settings BEGIN
                          SELECT RAISE(ABORT, 'test write failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.send(None)
        self.assertEqual(self.second.messages(self.chat), [])
        self.assertIsNone(self.second.get(ACTIVE_DRAFT_SESSION))
        self.assertEqual(self.second.draft(None)[0], 'saved')
        with self.second.connect() as db:
            db.execute('DROP TRIGGER refuse_send')
        self.send(None)
        self.assertEqual(self.second.draft(None), ('', None, None, None))

    def test_failed_draft_save_preserves_previously_known_snapshot(self):
        self.first.save_draft(self.chat, 'saved')
        with self.second.connect() as db:
            db.execute("""CREATE TRIGGER refuse_save BEFORE INSERT ON conversation_drafts BEGIN
                          SELECT RAISE(ABORT, 'test write failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.first.save_draft(self.chat, 'new text')
        self.send(self.chat, 'new text')
        self.assertEqual(self.second.draft(self.chat), ('', None, None, None))
