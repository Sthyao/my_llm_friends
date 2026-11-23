"""Persist agent memory and archive provenance in the journal database."""

from exp.archive import rank_evidence

from .database import connect


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_archive_sharing (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1))
);
CREATE TABLE IF NOT EXISTS agent_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('promise', 'boundary')),
    topic TEXT NOT NULL,
    text TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    resolved_by INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    UNIQUE (bot_id, kind, source_id)
);
CREATE TABLE IF NOT EXISTS agent_reply_sources (
    reply_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('archive', 'private')),
    PRIMARY KEY (reply_id, source_id, kind),
    CHECK (source_id < reply_id)
);
CREATE INDEX IF NOT EXISTS agent_reply_sources_by_source ON agent_reply_sources(source_id);
CREATE TABLE IF NOT EXISTS agent_reply_metadata (
    reply_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK(version > 0)
);
CREATE TRIGGER IF NOT EXISTS discard_agent_replies_with_deleted_sources
BEFORE DELETE ON messages FOR EACH ROW
BEGIN
    DELETE FROM messages WHERE id IN (
        SELECT reply_id FROM agent_reply_sources WHERE source_id = OLD.id
    );
END;
"""

# Derived replies are not archive sources. V1 replies can also carry references
# to foreign sessions, even when those sessions have not opted into this archive.
_ARCHIVE_SCOPE = """
    (m.session_id = ? OR EXISTS (
        SELECT 1 FROM agent_archive_sharing shar
        WHERE shar.session_id = m.session_id AND shar.enabled = 1
    ))
    AND NOT EXISTS (SELECT 1 FROM agent_reply_sources refs WHERE refs.reply_id = m.id)
    AND (m.role = 'user' OR NOT EXISTS (
        SELECT 1 FROM json_each(m.references_json) ref
        WHERE json_extract(ref.value, '$.session_id') != m.session_id
    ))
"""

visible_agent_message = """
    NOT EXISTS (
        SELECT 1
        FROM agent_reply_sources provenance
        JOIN messages source ON source.id = provenance.source_id
        WHERE provenance.reply_id = m.id AND provenance.kind = 'archive'
        AND source.session_id != m.session_id
        AND NOT EXISTS (
            SELECT 1 FROM agent_archive_sharing shar
            WHERE shar.session_id = source.session_id AND shar.enabled = 1
        )
    )
"""


def init_agent_store(db):
    db.executescript(_SCHEMA)


def archive_shared(db, session_id):
    row = db.execute(
        "SELECT enabled FROM agent_archive_sharing WHERE session_id = ?", (session_id,),
    ).fetchone()
    return bool(row and row["enabled"])


def set_archive_shared(db, session_id, enabled):
    db.execute("""
        INSERT INTO agent_archive_sharing (session_id, enabled) VALUES (?, ?)
        ON CONFLICT (session_id) DO UPDATE SET enabled = excluded.enabled
    """, (session_id, int(enabled)))


class AgentStore:
    def memories(self, agent):
        with connect() as db:
            rows = db.execute("""
                SELECT memory.id, memory.kind, memory.topic, memory.text, memory.source_id,
                       source.content AS source, source.session_id, session.title AS session_title
                FROM agent_memories memory
                JOIN messages source ON source.id = memory.source_id
                JOIN sessions session ON session.id = source.session_id
                WHERE memory.bot_id = ? AND memory.resolved_by IS NULL ORDER BY memory.id DESC
            """, (agent,))
            return [dict(row) for row in rows]

    def search_archive(self, session, query, *, recent_ids=()):
        with connect() as db:
            rows = db.execute(f"""
                SELECT m.id, m.session_id AS session, s.title AS session_title,
                       CASE WHEN m.role = 'user' THEN 'user' ELSE COALESCE(m.bot_name, 'assistant') END AS author,
                       CASE WHEN m.role = 'user' THEN 'user' WHEN m.bot_id IS NOT NULL THEN 'bot:' || m.bot_id
                            ELSE 'legacy:' || COALESCE(m.bot_name, 'unknown') END AS sender_id,
                       m.content AS text, COALESCE(m.turn_id, m.reply_to) AS turn_id,
                       NULL AS evidence_id, m.created_at
                FROM messages m JOIN sessions s ON s.id = m.session_id
                WHERE {_ARCHIVE_SCOPE} ORDER BY m.id
            """, (session,))
            return rank_evidence(rows, query, recent_ids=recent_ids)


def apply_agent_change(db, bot_id, source_id, change):
    """Apply one quote-backed memory write in the caller's transaction."""
    if not isinstance(change, dict):
        return False
    source = db.execute("SELECT content, role FROM messages WHERE id = ?", (source_id,)).fetchone()
    quote = change.get("quote")
    if (source is None or source["role"] != "user" or not isinstance(quote, str)
            or not quote.strip() or quote not in source["content"]):
        return False
    match change:
        case {"op": "remember", "kind": "promise" | "boundary" as kind, "topic": str(topic)} if topic.strip():
            result = db.execute("""
                INSERT INTO agent_memories (bot_id, kind, topic, text, source_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (bot_id, kind, source_id) DO NOTHING
            """, (bot_id, kind, topic.strip()[:100], quote, source_id))
        case {"op": "resolve", "memory_id": int(memory_id)} if type(memory_id) is int:
            result = db.execute("""
                UPDATE agent_memories SET resolved_by = ?
                WHERE id = ? AND bot_id = ? AND source_id < ? AND resolved_by IS NULL
            """, (source_id, memory_id, bot_id, source_id))
        case _:
            return False
    return bool(result.rowcount)


def validate_agent_sources(db, session_id, archive_ids):
    ids = tuple(archive_ids)
    if any(type(value) is not int or value < 1 for value in ids):
        return False
    if not ids:
        return True
    ids = tuple(dict.fromkeys(ids))
    placeholders = ", ".join("?" for _ in ids)
    available = db.execute(
        f"SELECT COUNT(*) FROM messages m WHERE {_ARCHIVE_SCOPE} AND m.id IN ({placeholders})",
        (session_id, *ids),
    ).fetchone()[0]
    return available == len(ids)


def store_agent_sources(db, reply_id, archive_ids, private_ids=()):
    sources = {(reply_id, source_id, "archive") for source_id in archive_ids}
    sources.update((reply_id, source_id, "private") for source_id in private_ids)
    db.executemany("INSERT INTO agent_reply_sources (reply_id, source_id, kind) VALUES (?, ?, ?)", sources)


def store_agent_metadata(db, reply_id, version):
    db.execute("INSERT INTO agent_reply_metadata (reply_id, version) VALUES (?, ?)", (reply_id, version))


def get_agent_metadata(db, session_id):
    return {
        row["id"]: {"mode": "agent", "version": row["version"], "archive_used": bool(row["archive_used"])}
        for row in db.execute(f"""
            SELECT m.id, profile.version, EXISTS (
                SELECT 1 FROM agent_reply_sources provenance
                WHERE provenance.reply_id = m.id AND provenance.kind = 'archive'
            ) AS archive_used
            FROM agent_reply_metadata profile JOIN messages m ON m.id = profile.reply_id
            WHERE m.session_id = ? AND {visible_agent_message}
        """, (session_id,))
    }
