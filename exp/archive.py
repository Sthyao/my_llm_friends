"""Shared conversation archive with source-level read permissions."""

import re
import unicodedata


ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_sharing (
    session TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
);
CREATE TABLE IF NOT EXISTS archive_sources (
    reply_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    PRIMARY KEY (reply_id, source_id),
    CHECK (source_id < reply_id)
);
CREATE INDEX IF NOT EXISTS archive_sources_by_source ON archive_sources(source_id);
CREATE TRIGGER IF NOT EXISTS discard_archive_replies_with_deleted_sources
BEFORE DELETE ON events FOR EACH ROW
BEGIN
    DELETE FROM events WHERE id IN (
        SELECT reply_id FROM archive_sources WHERE source_id = OLD.id
    );
END;
"""

# Keep archive retrieval over source events. A derived reply might quote another
# session whose share setting has changed since the reply was published.
SOURCE_SCOPE = """
    (e.session = ? OR EXISTS (
        SELECT 1 FROM archive_sharing s WHERE s.session = e.session AND s.enabled = 1
    ))
    AND e.evidence_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM archive_sources a WHERE a.reply_id = e.id)
"""

# Relevant only to derived replies already in a current conversation window.
# Both aliases are local to this predicate except e, the visible event.
VISIBLE_EVENT_FILTER = """
    NOT EXISTS (
        SELECT 1 FROM archive_sources a JOIN events source ON source.id = a.source_id
        WHERE a.reply_id = e.id AND source.session != e.session
        AND NOT EXISTS (
            SELECT 1 FROM archive_sharing s WHERE s.session = source.session AND s.enabled = 1
        )
    )
"""

MAX_TURNS = 6
MAX_EVIDENCE = 12
WORD_PARTS = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+")
SEARCH_WORDS = re.compile(
    r"还记得|记不记得|帮我查|找一下|翻一下|查一下|之前|以前|当时|过去|咱们|我们|"
    r"上次|哪次|到底|怎么|什么|几次|聊过|提过|说过|有没有|的看法|的观点|的理由|"
    r"改主意|有变化|变化了|我在|我对|一下|吗|呢"
)
STOP_TERMS = frozenset({
    "这个", "那个", "之前", "上次", "已经", "你说", "可以", "觉得", "还是", "就是",
    "我想", "想法", "说了", "写了", "写完", "完了", "做完", "到了", "现在", "后来",
    "是什么", "写完了", "the", "and", "with", "about",
})


def set_sharing(db, session, enabled):
    """Change cross-session access. The caller owns the transaction."""
    if not session.strip():
        raise ValueError("会话名不能为空")
    db.execute("""
        INSERT INTO archive_sharing (session, enabled) VALUES (?, ?)
        ON CONFLICT (session) DO UPDATE SET enabled = excluded.enabled
    """, (session, int(enabled)))


def accessible_evidence(db, session, ids):
    """Check that every supplied provenance ID is still a readable source."""
    ids = tuple(ids)
    if any(type(value) is not int or value < 1 for value in ids):
        return False
    if not ids:
        return True
    ids = tuple(dict.fromkeys(ids))
    placeholders = ", ".join("?" for _ in ids)
    available = db.execute(
        f"SELECT COUNT(*) FROM events e WHERE {SOURCE_SCOPE} AND e.id IN ({placeholders})",
        (session, *ids),
    ).fetchone()[0]
    return available == len(ids)


def _terms(text):
    terms = set()
    normalized = unicodedata.normalize("NFKC", text).casefold()
    for part in WORD_PARTS.findall(normalized):
        if part.isascii():
            if len(part) >= 2 and not part.isdigit():
                terms.add(part)
        else:
            for width in (2, 3, 4):
                terms.update(part[index:index + width] for index in range(len(part) - width + 1))
    return terms - STOP_TERMS


def _match(query_terms, text, whole_topic):
    overlap = query_terms & _terms(text)
    if not overlap:
        return 0
    if whole_topic not in overlap and len(overlap) < 2 and max(map(len, overlap)) < 3:
        return 0
    return sum(len(term) ** 2 for term in overlap)


def retrieve(db, session, query, *, recent_ids=()):
    """Rank topical raw hits, expand their turns, and return bounded evidence."""
    rows = db.execute(f"""
        SELECT e.id, e.session, e.author, e.text, e.turn_id, e.evidence_id
        FROM events e WHERE {SOURCE_SCOPE} ORDER BY e.id
    """, (session,))
    return rank_evidence(rows, query, recent_ids=recent_ids)


def rank_evidence(rows, query, *, recent_ids=()):
    """Rank source rows already filtered by the caller's access rules."""
    query = SEARCH_WORDS.sub(" ", unicodedata.normalize("NFKC", query).casefold())
    terms = _terms(query)
    parts = WORD_PARTS.findall(query)
    if not terms:
        return []
    whole_topic = parts[0] if len(parts) == 1 else None
    recent_ids = set(recent_ids)
    turns = {}
    hits = {}
    for row in rows:
        if row["id"] in recent_ids:
            continue
        row = dict(row)
        root = (row["session"], row["turn_id"] or row["id"])
        turns.setdefault(root, []).append(row)
        score = _match(terms, row["text"], whole_topic)
        if score:
            rank = (score, row["author"] == "user", row["id"])
            if root not in hits or rank > hits[root][0]:
                hits[root] = (rank, row)

    roots = sorted(hits, key=lambda root: hits[root][0], reverse=True)[:MAX_TURNS]
    # Reserve one direct hit per turn before expanding nearby replies.
    evidence = {hits[root][1]["id"]: hits[root][1] for root in roots}
    for root in roots:
        anchor = hits[root][1]["id"]
        neighbors = sorted(turns[root], key=lambda row: (row["id"] != root[1], abs(row["id"] - anchor)))
        for row in neighbors:
            if len(evidence) == MAX_EVIDENCE:
                break
            evidence[row["id"]] = row
    return [evidence[event_id] for event_id in sorted(evidence)]
