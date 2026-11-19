from collections.abc import Collection
import json
import re
from sqlite3 import Connection
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .database import connect
from .journal import timestamp
from .memory import normalize


router = APIRouter(prefix="/api/config/bots/{bot_id}/evolution")

EvolutionKeyword = Annotated[str, StringConstraints(min_length=2, max_length=48)]
GENERIC_KEYWORDS = frozenset({
    "聊天", "对话", "用户", "机器人", "想法", "看法", "感受", "情绪", "问题",
})


class EvolutionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    kind: Literal["view", "interest", "habit"]
    text: str = Field(min_length=4, max_length=360)
    keywords: list[EvolutionKeyword] = Field(min_length=1, max_length=8)
    evidence: str = Field(min_length=4, max_length=240)
    replaces_id: int | None = Field(default=None, gt=0)


class EvolutionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


def init_evolution(db: Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS bot_evolution_settings (
            bot_id INTEGER PRIMARY KEY REFERENCES bots(id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1))
        );
        CREATE TABLE IF NOT EXISTS bot_evolution_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
            source_message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('view', 'interest', 'habit')),
            text TEXT NOT NULL,
            normalized_text TEXT NOT NULL,
            keywords_json TEXT NOT NULL,
            evidence TEXT NOT NULL,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
            replaces_id INTEGER REFERENCES bot_evolution_notes(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS bot_evolution_active
            ON bot_evolution_notes(bot_id, active, id DESC);
        CREATE INDEX IF NOT EXISTS bot_evolution_revisions
            ON bot_evolution_notes(replaces_id);
    """)


NOTE_SELECT = """
    SELECT n.id, n.kind, n.text, n.keywords_json, n.evidence, n.active,
           n.replaces_id, n.created_at, n.source_message_id,
           m.session_id AS source_session_id, s.title AS source_session_title
    FROM bot_evolution_notes n
    JOIN messages m ON m.id = n.source_message_id
    JOIN sessions s ON s.id = m.session_id
"""


def _public_note(row) -> dict:
    note = dict(row)
    note["keywords"] = json.loads(note.pop("keywords_json"))
    note["active"] = bool(note["active"])
    return note


def _enabled(db: Connection, bot_id: int) -> bool:
    row = db.execute(
        "SELECT enabled FROM bot_evolution_settings WHERE bot_id = ?", (bot_id,),
    ).fetchone()
    return row is None or bool(row["enabled"])


def _require_bot(db: Connection, bot_id: int) -> None:
    if db.execute("SELECT 1 FROM bots WHERE id = ?", (bot_id,)).fetchone() is None:
        raise HTTPException(404, "机器人不存在。")


def _match_score(note: dict, query: str) -> int:
    score = 0
    for keyword in note["keywords"]:
        term = normalize(keyword)
        if term.isascii() and term.isalnum():
            matches = re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", query)
        else:
            matches = term in query
        if matches:
            score += min(len(term), 12)
    return score


def get_evolution(db: Connection, bot_id: int, recent_text: str) -> dict:
    """Recall this bot's notes. Turning off auto-updates keeps existing notes readable."""
    rows = db.execute(
        NOTE_SELECT + " WHERE n.bot_id = ? AND n.active = 1 ORDER BY n.id DESC", (bot_id,),
    )
    notes = [_public_note(row) for row in rows]
    query = normalize(recent_text)
    scored = [(score, note) for note in notes if (score := _match_score(note, query))]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    selected = [note for _, note in scored[:4]]
    selected_ids = {note["id"] for note in selected}
    enduring = [note for note in notes if
                note["kind"] in {"interest", "habit"} and note["id"] not in selected_ids]
    return {"enabled": _enabled(db, bot_id), "notes": selected + enduring[:2]}


def remember_evolution(db: Connection, bot_id: int, source_message_id: int, proposal,
                       *, seen_note_ids: Collection[int]) -> dict | None:
    """Persist one evidenced note under the same transaction as its visible reply."""
    if proposal is None:
        return None
    try:
        draft = EvolutionProposal.model_validate(proposal)
    except ValidationError:
        return None
    keywords = {
        normalize(word): word for word in draft.keywords
        if normalize(word) not in GENERIC_KEYWORDS and any(char.isalnum() for char in word)
    }
    normalized_text = " ".join(normalize(draft.text).strip("。.!！？?，,；;").split())
    if not keywords or not any(char.isalnum() for char in normalized_text):
        return None

    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    if not _enabled(db, bot_id):
        return None
    source = db.execute("""
        SELECT m.content FROM messages m JOIN bots b ON b.id = m.bot_id
        WHERE m.id = ? AND m.role = 'assistant' AND b.id = ?
    """, (source_message_id, bot_id)).fetchone()
    if source is None or draft.evidence not in source["content"]:
        return None
    already_saved = db.execute("""
        SELECT 1 FROM bot_evolution_notes WHERE source_message_id = ?
           OR (bot_id = ? AND normalized_text = ? AND active = 1)
    """, (source_message_id, bot_id, normalized_text)).fetchone()
    if already_saved:
        return None

    replaces_id = draft.replaces_id
    if replaces_id is not None:
        if replaces_id not in seen_note_ids:
            return None
        current = db.execute("""
            SELECT 1 FROM bot_evolution_notes
            WHERE id = ? AND bot_id = ? AND kind = ? AND active = 1
        """, (replaces_id, bot_id, draft.kind)).fetchone()
        if current is None:
            return None

    note_id = db.execute("""
        INSERT INTO bot_evolution_notes (
            bot_id, source_message_id, kind, text, normalized_text, keywords_json,
            evidence, created_at, replaces_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        bot_id, source_message_id, draft.kind, draft.text, normalized_text,
        json.dumps(list(keywords.values()), ensure_ascii=False), draft.evidence,
        timestamp(), replaces_id,
    )).lastrowid
    if replaces_id is not None:
        db.execute("UPDATE bot_evolution_notes SET active = 0 WHERE id = ?", (replaces_id,))
    return _public_note(db.execute(NOTE_SELECT + " WHERE n.id = ?", (note_id,)).fetchone())


def rewind_evolution(db: Connection, session_id: int, message_id: int) -> int:
    """Repair note history before the caller deletes a session's message suffix."""
    params = {"session_id": session_id, "message_id": message_id}
    removed = db.execute("""
        SELECT n.id, n.replaces_id, n.active
        FROM bot_evolution_notes n JOIN messages m ON m.id = n.source_message_id
        WHERE m.session_id = :session_id AND m.id >= :message_id
        ORDER BY n.id
    """, params).fetchall()
    if not removed:
        return 0

    # Each revision points to an older note, so its removed parent's ancestor is known.
    ancestors = {}
    for note in removed:
        previous = note["replaces_id"]
        ancestors[note["id"]] = ancestors.get(previous, previous)

    successors = db.execute("""
        SELECT n.id, n.replaces_id
        FROM bot_evolution_notes n
        JOIN messages m ON m.id = n.source_message_id
        JOIN bot_evolution_notes previous ON previous.id = n.replaces_id
        JOIN messages p ON p.id = previous.source_message_id
        WHERE p.session_id = :session_id AND p.id >= :message_id
          AND (m.session_id != :session_id OR m.id < :message_id)
    """, params).fetchall()
    db.executemany(
        "UPDATE bot_evolution_notes SET replaces_id = ? WHERE id = ?",
        [(ancestors[note["replaces_id"]], note["id"]) for note in successors],
    )

    restored = 0
    for note in reversed(removed):
        previous = ancestors[note["id"]]
        if not note["active"] or previous is None:
            continue
        restored += db.execute("""
            UPDATE bot_evolution_notes SET active = 1
            WHERE id = :previous AND active = 0 AND NOT EXISTS (
                SELECT 1
                FROM bot_evolution_notes remaining
                JOIN messages m ON m.id = remaining.source_message_id
                WHERE (m.session_id != :session_id OR m.id < :message_id)
                  AND (remaining.replaces_id = bot_evolution_notes.id OR (
                      remaining.bot_id = bot_evolution_notes.bot_id AND remaining.active = 1
                      AND remaining.normalized_text = bot_evolution_notes.normalized_text
                  ))
            )
        """, {**params, "previous": previous}).rowcount
    return restored


@router.get("")
def evolution_state(bot_id: int):
    with connect() as db:
        _require_bot(db, bot_id)
        rows = db.execute(
            NOTE_SELECT + " WHERE n.bot_id = ? ORDER BY n.id DESC LIMIT 200", (bot_id,),
        )
        return {"enabled": _enabled(db, bot_id), "notes": [_public_note(row) for row in rows]}


@router.patch("")
def patch_evolution(bot_id: int, body: EvolutionPatch):
    """Set whether new chats can update this bot's persona notes."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bot(db, bot_id)
        db.execute("""
            INSERT INTO bot_evolution_settings (bot_id, enabled) VALUES (?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET enabled = excluded.enabled
        """, (bot_id, body.enabled))
    return {"enabled": body.enabled}


@router.delete("/{note_id}", status_code=204)
def delete_evolution_note(bot_id: int, note_id: int):
    with connect() as db:
        cursor = db.execute(
            "DELETE FROM bot_evolution_notes WHERE id = ? AND bot_id = ?", (note_id, bot_id),
        )
        if not cursor.rowcount:
            raise HTTPException(404, "这条人格记录不存在。")
    return Response(status_code=204)
