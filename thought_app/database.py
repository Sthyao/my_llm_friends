from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parent.parent


def database_path():
    return Path(os.environ.get("THOUGHT_DB", ROOT / ".data" / "thoughts.sqlite3"))


@contextmanager
def connect():
    path = database_path()
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
            summary_share_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            bot_id INTEGER,
            bot_name TEXT,
            round INTEGER,
            reply_to INTEGER REFERENCES messages(id) ON DELETE CASCADE,
            turn_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
            channel TEXT NOT NULL DEFAULT 'main' CHECK(channel IN ('main', 'aside')),
            inner_thought TEXT,
            provider_name TEXT,
            model_name TEXT,
            model_code TEXT,
            created_at TEXT NOT NULL,
            audience_json TEXT,
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
        CREATE TABLE IF NOT EXISTS rewind_drafts (
            session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            content TEXT NOT NULL
        );
    """)
    if "summary_share_enabled" not in {column["name"] for column in db.execute("PRAGMA table_info(sessions)")}:
        db.execute("ALTER TABLE sessions ADD COLUMN summary_share_enabled INTEGER NOT NULL DEFAULT 1")
        db.execute("""
            UPDATE sessions
            SET summary_share_enabled = COALESCE(
                (SELECT share_enabled FROM summaries WHERE summaries.session_id = sessions.id), 1
            )
        """)
    columns = {column["name"] for column in db.execute("PRAGMA table_info(messages)")}
    if "bot_id" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN bot_id INTEGER")
        db.execute("""
            UPDATE messages
            SET bot_id = (SELECT MIN(id) FROM bots WHERE bots.name = messages.bot_name HAVING COUNT(*) = 1)
            WHERE role = 'assistant' AND bot_name IS NOT NULL
        """)
    if "turn_id" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN turn_id INTEGER REFERENCES messages(id) ON DELETE CASCADE")
        db.execute("""
            WITH RECURSIVE turns(id, origin) AS (
                SELECT id, id FROM messages WHERE role = 'user'
                UNION ALL
                SELECT m.id, t.origin FROM messages m JOIN turns t ON m.reply_to = t.id
                WHERE m.role = 'assistant' AND m.id > t.id
            )
            UPDATE messages SET turn_id = (SELECT origin FROM turns WHERE turns.id = messages.id)
            WHERE role = 'assistant'
        """)
    if "channel" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN channel TEXT NOT NULL DEFAULT 'main' CHECK(channel IN ('main', 'aside'))")
    if "audience_json" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN audience_json TEXT")
    if "inner_thought" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN inner_thought TEXT")
    for field in ("provider_name", "model_name", "model_code"):
        if field not in columns:
            db.execute(f"ALTER TABLE messages ADD COLUMN {field} TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS messages_turn ON messages(session_id, turn_id, channel)")
