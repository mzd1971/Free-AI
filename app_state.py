"""Persistent conversations and preferences, independent of training artifacts."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

PROJECT_PERMISSION_MODES = frozenset({'standard', 'readonly', 'automatic'})
PROJECT_SETTING_TYPES = {'enabled': bool, 'workspace': str, 'allow_write': bool, 'use_memory': bool,
                         'permission_mode': str}
PROJECT_RESULT_FIELDS = ('status', 'answer', 'run_dir', 'changes', 'episode_id', 'memory_updates', 'engine',
                         'task_state', 'continuation_available', 'permission_mode')
PROJECT_RECORD_LIMIT = 512 * 1024
PROJECT_STATE_FIELDS = ('task', 'status', 'phase', 'parent_run', 'uncertain')

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
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS session_project(
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    settings_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session_project_runs(
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    assistant_index INTEGER NOT NULL, result_json TEXT NOT NULL,
                    workspace TEXT NOT NULL, activity TEXT NOT NULL,
                    PRIMARY KEY(session_id, assistant_index));''')
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA foreign_keys=ON')
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
            message = db.execute('INSERT INTO messages(session_id,role,content) VALUES(?,?,?)',
                                 (identity, role, content))
            db.execute('UPDATE sessions SET updated=? WHERE id=?', (self.now(), identity))
            if role == 'user':
                db.execute("UPDATE sessions SET title=? WHERE id=? AND title='新的对话'", (content.replace('\n', ' ')[:22], identity))
            return message.lastrowid

    def project_settings(self, session_id):
        with self.connect() as db:
            row = db.execute('SELECT settings_json FROM session_project WHERE session_id=?',
                             (session_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_project_settings(self, session_id, settings):
        if (not isinstance(settings, dict) or any(
                key not in PROJECT_SETTING_TYPES or type(value) is not PROJECT_SETTING_TYPES[key]
                for key, value in settings.items()
        ) or ('permission_mode' in settings and settings['permission_mode'] not in PROJECT_PERMISSION_MODES)):
            raise ValueError('项目会话设置无效。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM sessions WHERE id=?', (session_id,)).fetchone():
                raise ValueError('会话不存在。')
            row = db.execute('SELECT settings_json FROM session_project WHERE session_id=?',
                             (session_id,)).fetchone()
            merged = json.loads(row[0]) if row else {}
            if ('workspace' in settings and 'permission_mode' in merged and 'permission_mode' not in settings
                    and Path(settings['workspace']) != Path(merged.get('workspace', ''))):
                merged.update(permission_mode='standard', allow_write=True)
            merged.update(settings)
            encoded = json.dumps(merged, ensure_ascii=False)
            if len(encoded.encode('utf-8')) > PROJECT_RECORD_LIMIT:
                raise ValueError('项目会话设置超过 512 KiB。')
            db.execute('INSERT OR REPLACE INTO session_project VALUES(?,?)', (session_id, encoded))

    def save_project_run(self, session_id, assistant_index, result, workspace, activity):
        if (type(assistant_index) is not int or assistant_index < 0 or not isinstance(result, dict)
                or not isinstance(workspace, str) or not isinstance(activity, str)):
            raise ValueError('项目运行记录无效。')
        encoded = self._encode_project_run(result, workspace, activity)
        with self.connect() as db:
            row = db.execute('SELECT role FROM messages WHERE session_id=? ORDER BY id LIMIT 1 OFFSET ?',
                             (session_id, assistant_index)).fetchone()
            if row is None or row[0] != 'assistant':
                raise ValueError('项目运行记录必须关联本会话的助手消息。')
            self._write_project_run(db, session_id, assistant_index, encoded, workspace, activity)

    @staticmethod
    def _encode_project_run(result, workspace, activity):
        public = {field: result[field] for field in PROJECT_RESULT_FIELDS if field in result}
        state = public.get('task_state')
        if isinstance(state, dict):
            structured = {field: state[field] for field in PROJECT_STATE_FIELDS if field in state}
            if 'failed_writes' in state:
                structured['failed_writes'] = [path for path in state['failed_writes'] if isinstance(path, str)]
            for group, fields in (('plan', ('text', 'status')), ('edits', ('path', 'sha256')),
                                  ('observations', ('tool', 'subject', 'status', 'detail')),
                                  ('unresolved', ('id', 'tool', 'subject'))):
                if group in state:
                    structured[group] = [{field: row[field] for field in fields if field in row}
                                         for row in state[group]]
            if 'verification' in state:
                verification = state['verification']
                check_fields = ('command', 'cwd', 'status', 'returncode', 'detail', 'current')
                structured['verification'] = {
                    'status': verification['status'],
                    'checks': [{field: row[field] for field in check_fields if field in row}
                               for row in verification.get('checks', [])],
                }
            public['task_state'] = structured
        try:
            encoded = json.dumps(public, ensure_ascii=False, allow_nan=False)
            size = sum(len(value.encode('utf-8')) for value in (encoded, workspace, activity))
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError('项目运行记录无法保存。') from exc
        if size > PROJECT_RECORD_LIMIT:
            raise ValueError('项目运行记录超过 512 KiB。')
        return encoded

    @staticmethod
    def _write_project_run(db, session_id, assistant_index, encoded, workspace, activity):
        db.execute('INSERT OR REPLACE INTO session_project_runs VALUES(?,?,?,?,?)',
                   (session_id, assistant_index, encoded, workspace, activity))

    def append_project_reply(self, session_id, result, workspace, activity):
        encoded = self._encode_project_run(result, workspace, activity)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM sessions WHERE id=?', (session_id,)).fetchone():
                raise ValueError('会话不存在。')
            index = db.execute('SELECT count(*) FROM messages WHERE session_id=?', (session_id,)).fetchone()[0]
            db.execute('INSERT INTO messages(session_id,role,content) VALUES(?,?,?)',
                       (session_id, 'assistant', result['answer']))
            db.execute('UPDATE sessions SET updated=? WHERE id=?', (self.now(), session_id))
            self._write_project_run(db, session_id, index, encoded, workspace, activity)
        return index

    def update_project_reply(self, session_id, assistant_index, result, workspace, activity):
        if (type(assistant_index) is not int or assistant_index < 0 or not isinstance(result, dict)
                or not isinstance(result.get('answer'), str)):
            raise ValueError('项目回复无效。')
        encoded = self._encode_project_run(result, workspace, activity)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT workspace FROM session_project_runs WHERE session_id=? AND assistant_index=?',
                             (session_id, assistant_index)).fetchone()
            reply = db.execute('SELECT id,role FROM messages WHERE session_id=? ORDER BY id LIMIT 1 OFFSET ?',
                               (session_id, assistant_index)).fetchone()
            if run is None or run[0] != workspace or reply is None or reply[1] != 'assistant':
                raise ValueError('项目运行记录必须关联原工作区的助手消息。')
            db.execute('UPDATE messages SET content=? WHERE id=?', (result['answer'], reply[0]))
            self._write_project_run(db, session_id, assistant_index, encoded, workspace, activity)
            db.execute('UPDATE sessions SET updated=? WHERE id=?', (self.now(), session_id))

    def annotate_project_reply(self, session_id, assistant_index, note, activity):
        if type(assistant_index) is not int or assistant_index < 0 or not isinstance(note, str) or not note.strip():
            raise ValueError('项目回复注释无效。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT result_json,workspace FROM session_project_runs '
                             'WHERE session_id=? AND assistant_index=?', (session_id, assistant_index)).fetchone()
            reply = db.execute('SELECT id,role,content FROM messages WHERE session_id=? ORDER BY id LIMIT 1 OFFSET ?',
                               (session_id, assistant_index)).fetchone()
            if run is None or reply is None or reply[1] != 'assistant':
                raise ValueError('项目运行记录必须关联本会话的助手消息。')
            result = json.loads(run[0])
            content = reply[2] + '\n\n' + note
            result['answer'] = content
            encoded = self._encode_project_run(result, run[1], activity)
            db.execute('UPDATE messages SET content=? WHERE id=?', (content, reply[0]))
            self._write_project_run(db, session_id, assistant_index, encoded, run[1], activity)
        return content

    def project_runs(self, session_id):
        with self.connect() as db:
            rows = db.execute(
                'SELECT assistant_index,result_json,workspace,activity FROM session_project_runs '
                'WHERE session_id=? ORDER BY assistant_index', (session_id,),
            ).fetchall()
        return [{'assistant_index': index, 'result': json.loads(result), 'workspace': workspace,
                 'activity': activity} for index, result, workspace, activity in rows]

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))
