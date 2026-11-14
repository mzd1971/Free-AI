"""Persistent conversations and preferences, independent of training artifacts."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

CONTEXTS = {'日常聊天 · 4K': 4096, '阅读长文 · 16K': 16384, '深入讨论 · 32K': 32768}

class Sessions:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'conversations.sqlite3'
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY, title TEXT, updated TEXT);
                CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);''')
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()
    def create(self):
        identity = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (identity, '新的对话', self.now()))
        return identity
    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()
    def recent(self):
        with self.connect() as db:
            return db.execute('SELECT id,title FROM sessions ORDER BY updated DESC LIMIT 50').fetchall()
    def messages(self, identity):
        with self.connect() as db:
            rows = db.execute('SELECT role,content FROM messages WHERE session_id=? ORDER BY id', (identity,)).fetchall()
        return [{'role': role, 'content': content} for role, content in rows]
    def append(self, identity, role, content):
        if role not in ('user', 'assistant') or not isinstance(content, str):
            raise ValueError('Invalid conversation message')
        with self.connect() as db:
            if not db.execute('SELECT id FROM sessions WHERE id=?', (identity,)).fetchone():
                raise ValueError('Conversation does not exist')
            db.execute('INSERT INTO messages(session_id,role,content) VALUES(?,?,?)', (identity, role, content))
            db.execute('UPDATE sessions SET updated=? WHERE id=?', (self.now(), identity))
            if role == 'user':
                db.execute("UPDATE sessions SET title=? WHERE id=? AND title='新的对话'", (content.replace('\n', ' ')[:22], identity))
    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))
