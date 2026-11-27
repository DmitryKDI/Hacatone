"""Persistent text conversations scoped to a catalog vehicle."""
from contextlib import closing, contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import uuid4


class ConversationMissing(LookupError):
    pass


class ConversationStore:
    def __init__(self, path):
        self.path = Path(path)
        self.ready = False
        self.lock = Lock()

    def initialize(self):
        with self.lock:
            if self.ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                db.executescript("""
                    PRAGMA journal_mode = WAL;
                    CREATE TABLE IF NOT EXISTS conversations (
                        id TEXT PRIMARY KEY,
                        vehicle_id TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        author TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        ended_at TEXT,
                        summary TEXT
                    );
                    CREATE INDEX IF NOT EXISTS conversations_vehicle_date
                        ON conversations(vehicle_id, started_at DESC);
                    CREATE TABLE IF NOT EXISTS conversation_turns (
                        id INTEGER PRIMARY KEY,
                        conversation_id TEXT NOT NULL REFERENCES conversations(id),
                        check_id TEXT NOT NULL,
                        question TEXT NOT NULL,
                        reply TEXT NOT NULL,
                        blocked INTEGER NOT NULL,
                        sources TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS conversation_turns_id
                        ON conversation_turns(conversation_id, id);
                """)
            self.ready = True

    @contextmanager
    def connection(self, write=False):
        self.initialize()
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys = ON')
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db

    def append(self, conversation_id, pair, actor_id, author, question, reply, blocked, sources):
        vehicle_id, check_id = pair
        with self.connection(write=True) as db:
            current = db.execute(
                'SELECT ended_at FROM conversations WHERE id=? AND vehicle_id=? AND actor_id=?',
                (conversation_id, vehicle_id, actor_id),
            ).fetchone()
            if current is None or current['ended_at'] is not None:
                conversation_id = uuid4().hex
                db.execute(
                    'INSERT INTO conversations(id, vehicle_id, actor_id, author, started_at) VALUES (?, ?, ?, ?, ?)',
                    (conversation_id, vehicle_id, actor_id, author, now()),
                )
            db.execute(
                'INSERT INTO conversation_turns(conversation_id, check_id, question, reply, blocked, sources) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (conversation_id, check_id, question, reply, blocked, json.dumps(sources, ensure_ascii=False)),
            )
        return conversation_id

    def finish(self, conversation_id, vehicle_id, actor_id):
        with self.connection(write=True) as db:
            row = db.execute(
                'SELECT ended_at, summary FROM conversations WHERE id=? AND vehicle_id=? AND actor_id=?',
                (conversation_id, vehicle_id, actor_id),
            ).fetchone()
            if row is None:
                raise ConversationMissing()
            if row['summary'] is not None:
                return json.loads(row['summary'])
            summary = recent_context(db, conversation_id)
            db.execute('UPDATE conversations SET ended_at=?, summary=? WHERE id=?',
                       (row['ended_at'] or now(), json.dumps(summary, ensure_ascii=False), conversation_id))
        return summary

    def overview(self, vehicle_id, actor_id):
        with self.connection() as db:
            rows = db.execute(
                'SELECT id, author, started_at, ended_at FROM conversations WHERE vehicle_id=? '
                'AND (ended_at IS NOT NULL OR actor_id=?) ORDER BY ended_at DESC, rowid DESC LIMIT 20',
                (vehicle_id, actor_id),
            ).fetchall()
            latest = None
            if rows and rows[0]['ended_at']:
                identifier = rows[0]['id']
                stored = db.execute('SELECT summary FROM conversations WHERE id=?', (identifier,)).fetchone()['summary']
                latest = json.loads(stored) if stored is not None else recent_context(db, identifier)
        return {'latest': latest, 'saved': [dict(row) for row in rows]}

    def read(self, conversation_id, vehicle_id, actor_id):
        with self.connection() as db:
            row = db.execute(
                'SELECT id, author, started_at, ended_at FROM conversations WHERE id=? AND vehicle_id=? '
                'AND (ended_at IS NOT NULL OR actor_id=?)',
                (conversation_id, vehicle_id, actor_id),
            ).fetchone()
            if row is None:
                raise ConversationMissing()
            rows = db.execute(
                'SELECT * FROM conversation_turns WHERE conversation_id=? ORDER BY id DESC LIMIT 100',
                (conversation_id,),
            ).fetchall()
        return {**dict(row), 'turns': [turn(item) for item in reversed(rows)]}


def recent_context(db, conversation_id):
    rows = db.execute(
        'SELECT * FROM conversation_turns WHERE conversation_id=? ORDER BY id DESC LIMIT 4',
        (conversation_id,),
    ).fetchall()
    rows.reverse()
    return {
        'exchanges': [{**turn(item), 'reply': item['reply'][:720]} for item in rows],
        'open_questions': [item['question'] for item in rows if item['blocked']],
    }


def now():
    return datetime.now(UTC).isoformat(timespec='milliseconds')


def turn(row):
    return {'check_id': row['check_id'], 'question': row['question'], 'reply': row['reply'],
            'blocked': bool(row['blocked']), 'sources': json.loads(row['sources'])}
