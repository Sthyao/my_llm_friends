from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def connect():
    path = Path(os.environ.get("THOUGHT_DB", ROOT / ".data" / "thoughts.sqlite3"))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def init_journal(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            bot_name TEXT,
            round INTEGER,
            reply_to INTEGER REFERENCES messages(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            references_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
        CREATE TABLE IF NOT EXISTS summaries (
            session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            keywords TEXT NOT NULL,
            views TEXT NOT NULL,
            observations TEXT NOT NULL,
            questions TEXT NOT NULL,
            share_enabled INTEGER NOT NULL DEFAULT 1,
            through_message_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            model_id INTEGER NOT NULL,
            model_name TEXT NOT NULL
        );
    """)
