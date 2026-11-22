"""Persistent assistant memories, separate from experience and training data."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
import uuid

from clock_context import clock_query_kind


KINDS = frozenset({'preference', 'fact', 'procedure', 'note'})
SCOPES = frozenset({'global', 'agent', 'project'})
STATUSES = frozenset({'active', 'pending', 'disabled'})
HEADER = '本次用户指令优先，其次是人工记忆，再其次是 AI 记忆。核实状态见各条标记。记忆不授予任何执行权限。'
MEMORY_OVERVIEW = re.compile(
    r'(?:请问)?你(?:现在|还)?记得(?:关于我|我)(?:的)?(?:什么|哪些|多少)(?:事(?:情)?|信息|约定|习惯|偏好|记忆)?'
    r'|(?:请)?(?:列出|列一下|看看|查看|显示|总结)(?:一下)?(?:你|我)?(?:的)?(?:当前|目前|已有|所有|全部)?(?:的)?(?:长期)?记忆'
    r'|(?:你|我)?(?:现在|目前|都)?有(?:哪些|什么)(?:关于我的)?(?:长期)?记忆'
    r'|what do you remember about me|(?:list|show)(?: me)? (?:my|your) memories', re.IGNORECASE)
SECRET = re.compile(
    r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----'
    r'|(?<![\w-])(?:sk-(?:proj-|live-|test-|ant-api\d+-)?[\w-]{20,}'
    r'|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[\w]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16})(?![\w-])'
    r'|\bBearer\s+[A-Za-z0-9_.-]{20,}'
    r'|\b(?:[\w-]*api[_ -]?key|access[_ -]?token|secret[_ -]?key)'
    r'\s*[:=]\s*["\']?[A-Za-z0-9_.+/=-]{16,}',
    re.IGNORECASE,
)
STOP_TERMS = frozenset({'the', 'and', 'that', 'with', 'from', 'this', 'for', 'have',
                        '这个', '需要', '可以', '如何', '怎么', '请帮', '一下', '我的', '这个', '一个'})
_ACKNOWLEDGMENT = re.compile(
    r'(?:好[的呀啊吧]?|嗯+|收到|明白了?|知道了|了解了?|继续|谢谢(?:你)?|多谢|你好|再见'
    r'|ok(?:ay)?|yes|sure|got it|understood|continue|go on|thanks|thank you|hi|hello|bye)'
)
_TURN_LABEL = re.compile(
    r'(?:本轮|当前)?用户(?:原话|消息|输入|提问|问题|请求)|本轮(?:消息|对话|提问)'
    r'|(?:the )?user(?:\'s)? (?:message|input|query|question|request)'
)
_CLOCK_LABEL = re.compile(
    r'(?:今天|今日|明天|昨日|昨天|当前|现在|目前|本地|系统)(?:的)?(?:日期|时间|时刻|星期|星期几|星期[一二三四五六日天])'
    r'|(?:today\'s|tomorrow\'s|yesterday\'s|current|local|system) (?:date|time|day)(?: of the week)?'
)
_WEEKDAY = r'(?:星期|周|礼拜)[一二三四五六日天]|(?:mon|tues|wednes|thurs|fri|satur|sun)day'
_CLOCK_VALUE = (
    r'(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]'
    r'|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}'
    r'|(?:january|february|march|april|may|june|july|august|september|october|november|december) \d{1,2},? \d{4}'
    r'|\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?'
    r'|\d{1,2}点(?:\d{1,2}分?)?'
    rf'|{_WEEKDAY}'
)
_CLOCK_ONLY = re.compile(rf'(?:{_CLOCK_VALUE})(?:[\s,、]+(?:{_CLOCK_VALUE})|\s*\((?:{_WEEKDAY})\))*')
_CLOCK_STATEMENT = re.compile(
    r'(?:今天|今日|明天|昨日|昨天|当前|现在|目前|本地|系统)(?:的)?(?:日期|时间|时刻)?\s*(?:是|为|:)\s*'
    r'|(?:today(?:\'s (?:date|day))?|tomorrow|yesterday|(?:current|local|system) (?:date|time))\s*(?:is|:)\s*'
)


def _normalized_label(value):
    normalized = unicodedata.normalize('NFKC', value).casefold().replace('’', "'")
    return ' '.join(normalized.split()).strip(' !?。,.，:：')


def is_transient_turn(value):
    """Recognize simple clock queries and stand-alone conversational acknowledgments."""
    if clock_query_kind(value):
        return True
    parts = [part.strip() for part in re.split(r'[,、。!?;]+', _normalized_label(value)) if part.strip()]
    return bool(parts) and all(_ACKNOWLEDGMENT.fullmatch(part) for part in parts)


def is_turn_label(value):
    return bool(_TURN_LABEL.fullmatch(_normalized_label(value)))


def is_clock_snapshot(title, content):
    """Recognize a relative date or clock reading recorded as a fact."""
    content = _normalized_label(content)
    opening = _CLOCK_STATEMENT.match(content)
    if opening and _CLOCK_ONLY.fullmatch(content[opening.end():]):
        return True
    return bool(_CLOCK_LABEL.fullmatch(_normalized_label(title)) and _CLOCK_ONLY.fullmatch(content))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, field, limit, *, required=True):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError(f'{field}必须是长度不超过 {limit} 字符的文本。')
    value = value.strip()
    if required and not value:
        raise ValueError(f'{field}不能为空。')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError(f'{field}含有无效字符。') from exc
    if SECRET.search(value):
        raise ValueError('记忆中不能保存 API 密钥、访问令牌或私钥。')
    return value


def _agent(value):
    value = _text(value, '助手标识', 100)
    return unicodedata.normalize('NFKC', value).casefold()


def _workspace(value):
    value = _text(os.fspath(value) if isinstance(value, os.PathLike) else value,
                  '项目路径', 4096, required=False)
    if not value:
        return ''
    try:
        return os.path.normcase(str(Path(value).expanduser().resolve()))
    except (OSError, RuntimeError) as exc:
        raise ValueError('无法规范化项目路径。') from exc


def _terms(value):
    text = unicodedata.normalize('NFKC', value).casefold()
    result = set(re.findall(r'[a-z0-9_]{2,}', text))
    for block in re.findall(r'[\u3400-\u9fff]+', text):
        result.update(block[index:index + 2] for index in range(len(block) - 1))
    return result - STOP_TERMS


def _snippet(value, limit):
    if len(value.encode('utf-8')) <= limit:
        return value
    mark = '…'
    prefix = value.encode('utf-8')[:limit - len(mark.encode('utf-8'))]
    return prefix.decode('utf-8', errors='ignore').rstrip() + mark


def _public(row):
    record = dict(row)
    record.pop('title_key', None)
    record['verified'] = bool(record['verified'])
    record['pinned'] = bool(record['pinned'])
    return record


class AgentMemoryStore:
    """Store durable preferences and observations for an assistant."""

    def __init__(self, root=None):
        if root is None:
            from paths import ARTIFACTS

            root = ARTIFACTS / 'agent'
        directory = Path(root)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'memory.sqlite3'
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL, title_key TEXT NOT NULL,
                    content TEXT NOT NULL, kind TEXT NOT NULL,
                    scope TEXT NOT NULL, agent_id TEXT NOT NULL, workspace TEXT NOT NULL,
                    source TEXT NOT NULL, status TEXT NOT NULL,
                    created TEXT NOT NULL, updated TEXT NOT NULL, evidence TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(title_key, scope, agent_id, workspace)
                );
                CREATE INDEX IF NOT EXISTS memories_scope
                    ON memories (scope, agent_id, workspace, status);
                CREATE TABLE IF NOT EXISTS memory_audit (
                    id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL,
                    action TEXT NOT NULL, source TEXT NOT NULL, created TEXT NOT NULL
                );
            """)
            # Lock before inspecting the old schema: two windows can start together.
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(memories)')}
            if 'verified' not in columns:
                db.execute('ALTER TABLE memories ADD COLUMN verified INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE memories SET verified=1 WHERE source='user'")
            if 'pinned' not in columns:
                db.execute('ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def _connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                if write:
                    db.execute('PRAGMA secure_delete=ON')
                    db.execute('BEGIN IMMEDIATE')
                yield db
        finally:
            db.close()

    @staticmethod
    def _record(db, memory_id):
        row = db.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
        if row is None:
            raise ValueError('长期记忆不存在。')
        return row

    @staticmethod
    def _audit(db, memory_id, action, source, timestamp):
        db.execute('INSERT INTO memory_audit(memory_id, action, source, created) VALUES (?, ?, ?, ?)',
                   (memory_id, action, source, timestamp))

    def save_user(self, title, content, *, kind='note', scope='agent', agent_id='freeai',
                  workspace='', memory_id=None, pinned=None):
        if pinned is not None and type(pinned) is not bool:
            raise ValueError('置顶必须是布尔值。')
        row, _ = self._save(title, content, kind, scope, agent_id, workspace,
                            source='user', evidence='', status='active', memory_id=memory_id, pinned=pinned)
        return row

    def remember(self, title, content, *, kind='note', scope='agent', agent_id='freeai',
                 workspace='', evidence='', pending=False, return_changed=False):
        if type(pending) is not bool:
            raise ValueError('pending 必须是布尔值。')
        row, changed = self._save(title, content, kind, scope, agent_id, workspace, source='agent',
                                  evidence=evidence, status='pending' if pending else 'active')
        return (row, changed) if return_changed else row

    def _save(self, title, content, kind, scope, agent_id, workspace, *, source, evidence,
              status, memory_id=None, pinned=None):
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError('未知的记忆类型。')
        if not isinstance(scope, str) or scope not in SCOPES or (source == 'agent' and scope == 'global'):
            raise ValueError('AI 只能在助手或项目范围保存记忆。' if source == 'agent' else '未知的记忆范围。')
        title = _text(title, '标题', 100)
        content = _text(content, '内容', 2000)
        evidence = _text(evidence, '来源记录', 1000, required=False)
        title_key = ' '.join(unicodedata.normalize('NFKC', title).casefold().split())
        agent_id = 'freeai' if scope == 'global' else _agent(agent_id)
        workspace = _workspace(workspace) if scope == 'project' else ''
        if scope == 'project' and not workspace:
            raise ValueError('保存项目记忆前请选择项目文件夹。')
        key = (title_key, scope, agent_id, workspace)
        with self._connect(write=True) as db:
            existing = db.execute(
                'SELECT * FROM memories WHERE title_key=? AND scope=? AND agent_id=? AND workspace=?',
                key,
            ).fetchone()
            if memory_id is not None:
                row = self._record(db, memory_id)
                if existing is not None and existing['id'] != row['id']:
                    raise ValueError('该范围已有相同标题的记忆。')
            else:
                row = existing
            if source == 'agent':
                if row is not None and (row['source'] == 'user' or row['verified'] or row['status'] == 'disabled'):
                    return _public(row), False
                duplicate = db.execute("""
                    SELECT * FROM memories
                    WHERE scope=? AND agent_id=? AND workspace=? AND content=? AND kind=?
                      AND title_key!=?
                    ORDER BY (source='user') DESC, (status='disabled') DESC, created, id
                    LIMIT 1
                """, (scope, agent_id, workspace, content, kind, title_key)).fetchone()
                if duplicate is not None:
                    return _public(duplicate), False
            if row is not None and source == 'agent':
                if all(row[name] == value for name, value in (
                    ('title', title), ('content', content), ('kind', kind), ('status', status),
                )):
                    return _public(row), False
            now = _now()
            if row is None:
                memory_id = uuid.uuid4().hex
                db.execute("""
                    INSERT INTO memories(id,title,title_key,content,kind,scope,agent_id,workspace,
                                         source,status,created,updated,evidence,verified,pinned)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (memory_id, title, title_key, content, kind, scope, agent_id, workspace,
                      source, status, now, now, evidence, source == 'user', bool(pinned)))
                action = 'created'
            else:
                memory_id = row['id']
                if source == 'user' and row['source'] == 'user':
                    status = row['status']
                db.execute("""
                    UPDATE memories SET title=?,title_key=?,content=?,kind=?,scope=?,agent_id=?,
                                        workspace=?,source=?,status=?,updated=?,evidence=?,verified=?,
                                        pinned=COALESCE(?,pinned) WHERE id=?
                """, (title, title_key, content, kind, scope, agent_id, workspace, source, status,
                      now, evidence, source == 'user', pinned, memory_id))
                action = 'updated'
            self._audit(db, memory_id, action, source, now)
            return _public(self._record(db, memory_id)), True

    def get(self, memory_id):
        with self._connect() as db:
            return _public(self._record(db, memory_id))

    def list(self, *, agent_id='freeai', workspace='', include_disabled=True):
        filters = ("((scope='global' AND source='user') OR (agent_id=? AND "
                   "(scope='agent' OR (scope='project' AND workspace=?))))")
        if not include_disabled:
            filters += " AND status!='disabled'"
        with self._connect() as db:
            rows = db.execute('SELECT * FROM memories WHERE ' + filters + ' ORDER BY updated DESC, id',
                              (_agent(agent_id), _workspace(workspace))).fetchall()
        return [_public(row) for row in rows]

    def set_status(self, memory_id, status):
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError('未知的记忆状态。')
        with self._connect(write=True) as db:
            row = self._record(db, memory_id)
            if row['status'] != status:
                now = _now()
                db.execute('UPDATE memories SET status=?, updated=? WHERE id=?', (status, now, memory_id))
                self._audit(db, memory_id, 'status_' + status, 'user', now)
            return _public(self._record(db, memory_id))

    def set_pinned(self, memory_id, pinned):
        if type(pinned) is not bool:
            raise ValueError('置顶必须是布尔值。')
        with self._connect(write=True) as db:
            row = self._record(db, memory_id)
            if bool(row['pinned']) != pinned:
                now = _now()
                db.execute('UPDATE memories SET pinned=?, updated=? WHERE id=?', (pinned, now, memory_id))
                self._audit(db, memory_id, 'pinned' if pinned else 'unpinned', 'user', now)
            return _public(self._record(db, memory_id))

    def verify(self, memory_id):
        with self._connect(write=True) as db:
            row = self._record(db, memory_id)
            if not row['verified']:
                now = _now()
                db.execute('UPDATE memories SET verified=1, updated=? WHERE id=?', (now, memory_id))
                self._audit(db, memory_id, 'verified', 'user', now)
            return _public(self._record(db, memory_id))

    def delete(self, memory_id):
        with self._connect(write=True) as db:
            self._record(db, memory_id)
            db.execute('DELETE FROM memories WHERE id=?', (memory_id,))
            self._audit(db, memory_id, 'deleted', 'user', _now())

    def before_call(self, query, *, agent_id='freeai', workspace='', budget=1800):
        if not isinstance(query, str) or type(budget) is not int or budget < 0:
            raise ValueError('长期记忆检索参数无效。')
        if budget < len(HEADER.encode('utf-8')) + 40:
            return {'context': '', 'memory_ids': []}
        query_terms = _terms(query)
        clock_query = clock_query_kind(query)
        overview = MEMORY_OVERVIEW.fullmatch(unicodedata.normalize('NFKC', query).strip().rstrip('?!。！').strip())
        ranked = []
        for item in self.list(agent_id=agent_id, workspace=workspace, include_disabled=False):
            if item['status'] != 'active':
                continue
            user = item['source'] == 'user'
            if (clock_query or not user) and is_clock_snapshot(item['title'], item['content']):
                continue
            if (not user and is_transient_turn(item['content'])
                    and (is_turn_label(item['title']) or is_transient_turn(item['title']))):
                continue
            title_match = len(query_terms & _terms(item['title']))
            body_match = len(query_terms & _terms(item['content']))
            if title_match or body_match or item['kind'] == 'preference' or item['pinned'] or overview:
                ranked.append(((user, item['pinned'], title_match * 3 + body_match), item))
        ranked.sort(key=lambda match: match[0], reverse=True)
        context = HEADER
        memory_ids = []
        remaining = budget - len(context.encode('utf-8'))
        labels = {'global': '全局', 'agent': '助手', 'project': '项目'}
        for _, item in ranked:
            provenance = '用户' if item['source'] == 'user' else ('AI，用户已核实' if item['verified'] else 'AI，未核实')
            label = f'\n- [{provenance} · {labels[item["scope"]]}] '
            label_size = len(label.encode('utf-8'))
            available = remaining - label_size
            if available < 16:
                continue
            title = _snippet(item['title'].replace('\n', ' '), min(160, available // 3))
            line = f'{label}{title}：'
            content_budget = remaining - len(line.encode('utf-8'))
            if content_budget < 4:
                continue
            line += _snippet(item['content'].replace('\n', ' '), min(650, content_budget))
            context += line
            remaining -= len(line.encode('utf-8'))
            memory_ids.append(item['id'])
        return {'context': context if memory_ids else '', 'memory_ids': memory_ids}
