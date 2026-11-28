from datetime import datetime, timedelta, timezone
import json
import unittest

from fsrs import Card
from memory_retention import new_state, priority, reinforce, retention, should_archive


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


class MemoryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.state = new_state(START)
        self.record = {
            'status': 'active',
            'source': 'agent',
            'pinned': False,
            'verified': False,
            'importance': 0.5,
            'created': START.isoformat(),
            'updated': START.isoformat(),
            'retention_state': self.state,
        }

    def test_fsrs_scores_decay_and_explicit_review_reinforces(self):
        self.assertEqual(Card.from_json(self.state).last_review, START)
        self.assertEqual(retention(self.state, START), 1.0)
        reviewed_at = START + timedelta(days=60)
        later = START + timedelta(days=120)
        self.assertGreater(retention(self.state, reviewed_at), retention(self.state, later))
        reinforced = reinforce(self.state, reviewed_at)
        self.assertEqual(Card.from_json(reinforced).last_review, reviewed_at)
        self.assertGreater(retention(reinforced, later), retention(self.state, later))
        self.assertEqual(reinforce(reinforced, reviewed_at), reinforced)
        self.assertEqual(reinforce(reinforced, START), reinforced)

    def test_importance_changes_archive_threshold_and_priority(self):
        important = dict(self.record, importance=0.7)
        self.assertFalse(should_archive(self.record, START + timedelta(days=62)))
        self.assertTrue(should_archive(self.record, START + timedelta(days=63)))
        self.assertFalse(should_archive(important, START + timedelta(days=415)))
        self.assertTrue(should_archive(important, START + timedelta(days=416)))

        now = START + timedelta(days=63)
        self.assertGreater(priority(important, now), priority(self.record, now))
        self.assertGreater(priority(important, START), priority(important, now))

    def test_protected_or_inactive_memories_are_not_archived(self):
        now = START + timedelta(days=500)
        self.assertTrue(should_archive(self.record, now))
        for changes in (
            {'source': 'user'},
            {'verified': True},
            {'pinned': True},
            {'status': 'pending'},
            {'status': 'disabled'},
            {'status': 'archived'},
            {'updated': (now - timedelta(days=29)).isoformat()},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(should_archive(dict(self.record, **changes), now))
        updated = (now - timedelta(days=30)).isoformat()
        self.assertTrue(should_archive(dict(self.record, updated=updated), now))

    def test_invalid_state_or_timestamp_preserves_memory(self):
        now = START + timedelta(days=900)
        card = json.loads(self.state)
        for broken in (
            '',
            '{',
            '[]',
            json.dumps(dict(card, stability='NaN')),
            json.dumps(dict(card, stability=1e-250)),
            json.dumps(dict(card, difficulty=100)),
            json.dumps(dict(card, last_review='2026-01-01T00:00:00')),
        ):
            with self.subTest(state=broken):
                self.assertEqual(retention(broken, now), 1.0)
                self.assertEqual(reinforce(broken, now), broken)
                self.assertFalse(should_archive(dict(self.record, retention_state=broken), now))

        self.assertEqual(retention(None, now), 1.0)
        self.assertFalse(should_archive(dict(self.record, importance=float('nan')), now))
        self.assertFalse(should_archive(dict(self.record, updated='unknown'), now))
        self.assertFalse(should_archive(dict(self.record, updated=now.isoformat()), now))

    def test_normalizes_aware_clock_to_utc(self):
        local_time = START.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(Card.from_json(new_state(local_time)).last_review, START)
        with self.assertRaises(ValueError):
            new_state(START.replace(tzinfo=None))


if __name__ == '__main__':
    unittest.main()
