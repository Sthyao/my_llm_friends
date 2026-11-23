from collections.abc import Collection
import json
import re
from sqlite3 import Connection
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from exp.harness import persona_source_hash

from .agent_store import visible_agent_message
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


class EvolutionSkill(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    phase: Literal["attention", "decision", "express"]
    instruction: str = Field(min_length=4, max_length=360)


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
        CREATE TABLE IF NOT EXISTS agent_evolution_traits (
            note_id INTEGER PRIMARY KEY REFERENCES bot_evolution_notes(id) ON DELETE CASCADE,
            base_hash TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('attention', 'decision', 'express')),
            instruction TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS retract_evolved_skill
        BEFORE DELETE ON bot_evolution_notes
        WHEN EXISTS (SELECT 1 FROM agent_evolution_traits WHERE note_id = OLD.id)
          OR EXISTS (SELECT 1 FROM agent_evolution_traits t JOIN bot_evolution_notes successor
                     ON successor.id = t.note_id WHERE successor.replaces_id = OLD.id)
        BEGIN
            UPDATE bot_evolution_notes SET replaces_id = OLD.replaces_id
            WHERE replaces_id = OLD.id;
            UPDATE bot_evolution_notes SET active = 1
            WHERE OLD.active = 1 AND id = OLD.replaces_id AND active = 0
              AND NOT EXISTS (
                SELECT 1 FROM bot_evolution_notes other
                WHERE other.id != OLD.id AND (other.replaces_id = OLD.replaces_id OR (
                    other.bot_id = OLD.bot_id AND other.active = 1
                    AND other.normalized_text = bot_evolution_notes.normalized_text
                ))
              );
        END;
    """)


_NOTE_FIELDS = """
           n.id, n.kind, n.text, n.keywords_json, n.evidence, n.active,
           n.replaces_id, n.created_at, n.source_message_id,
           m.session_id AS source_session_id, s.title AS source_session_title
"""
_NOTE_FROM = """
    FROM bot_evolution_notes n
    JOIN messages m ON m.id = n.source_message_id
    JOIN sessions s ON s.id = m.session_id
"""
NOTE_SELECT = "SELECT " + _NOTE_FIELDS + _NOTE_FROM
_TRAIT_JOIN = " LEFT JOIN agent_evolution_traits t ON t.note_id = n.id "
_SKILL_FIELD = """CASE WHEN t.note_id IS NOT NULL THEN
    json_object('name', t.skill_name, 'phase', t.phase, 'instruction', t.instruction) END AS skill_json"""


def _public_note(row) -> dict:
    note = dict(row)
    note["keywords"] = json.loads(note.pop("keywords_json"))
    note["active"] = bool(note["active"])
    if "skill_json" in note:
        skill = note.pop("skill_json")
        note["skill"] = json.loads(skill) if skill is not None else None
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


def active_traits(db: Connection, bot_id: int, base_hash: str) -> list[dict]:
    rows = db.execute(f"""
        SELECT n.id AS note_id, n.text AS description, t.skill_name AS name, t.phase, t.instruction
        FROM agent_evolution_traits t JOIN bot_evolution_notes n ON n.id = t.note_id
        JOIN messages m ON m.id = n.source_message_id JOIN bots b ON b.id = n.bot_id
        WHERE n.bot_id = ? AND n.active = 1 AND b.role_mode != 'mirror' AND t.base_hash = ?
          AND {visible_agent_message}
        ORDER BY n.id
    """, (bot_id, base_hash))
    return [dict(row) for row in rows]


def get_evolution(db: Connection, bot_id: int, recent_text: str, *, base_hash: str | None = None) -> dict:
    """Recall this bot's notes. Turning off auto-updates keeps existing notes readable."""
    rows = db.execute(f"""
        SELECT {_NOTE_FIELDS}, {_SKILL_FIELD} {_NOTE_FROM} {_TRAIT_JOIN}
        WHERE n.bot_id = ? AND n.active = 1 AND (t.note_id IS NULL OR t.base_hash = ?)
          AND {visible_agent_message} ORDER BY n.id DESC
    """, (bot_id, base_hash))
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
                       *, seen_note_ids: Collection[int], base_hash: str | None = None) -> dict | None:
    """Persist one evidenced note under the same transaction as its visible reply."""
    if not isinstance(proposal, dict):
        return None
    note_fields = dict(proposal)
    raw_skill = note_fields.pop("skill", None)
    try:
        draft = EvolutionProposal.model_validate(note_fields)
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
    source = db.execute(f"""
        SELECT m.content, p.prompt FROM messages m JOIN bots b ON b.id = m.bot_id
        JOIN personas p ON p.id = b.persona_id
        WHERE m.id = ? AND m.role = 'assistant' AND b.id = ? AND b.enabled = 1
          AND b.role_mode != 'mirror' AND {visible_agent_message}
    """, (source_message_id, bot_id)).fetchone()
    if source is None or draft.evidence not in source["content"]:
        return None
    if base_hash is not None and base_hash != persona_source_hash(source["prompt"]):
        return None
    skill = None
    if raw_skill is not None and base_hash:
        try:
            skill = EvolutionSkill.model_validate(raw_skill)
        except ValidationError:
            pass  # A valid ordinary note can still be retained.

    replaces_id = draft.replaces_id
    if replaces_id is not None:
        if replaces_id not in seen_note_ids:
            return None
        current = db.execute(f"""
            SELECT t.base_hash, t.skill_name, t.phase, t.instruction, n.normalized_text
            {_NOTE_FROM} {_TRAIT_JOIN}
            WHERE n.id = ? AND n.bot_id = ? AND n.kind = ? AND n.active = 1 AND {visible_agent_message}
        """, (replaces_id, bot_id, draft.kind)).fetchone()
        if current is None:
            return None
        if current["base_hash"] is not None:
            if not skill or current["base_hash"] != base_hash:
                return None
            if (normalized_text, skill.name, skill.phase, skill.instruction) == tuple(
                current[field] for field in ("normalized_text", "skill_name", "phase", "instruction")
            ):
                return None

    # A skill-only revision may keep the predecessor's public description.
    revised_note = replaces_id if skill and replaces_id is not None else 0
    already_saved = db.execute(f"""
        SELECT 1 FROM bot_evolution_notes n {_TRAIT_JOIN}
        WHERE n.source_message_id = ? OR (
            n.bot_id = ? AND n.normalized_text = ? AND n.active = 1
            AND (t.note_id IS NULL OR t.base_hash = ?)
            AND n.id != ?
        )
    """, (source_message_id, bot_id, normalized_text, base_hash, revised_note)).fetchone()
    if already_saved:
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
    if skill:
        db.execute("""
            INSERT INTO agent_evolution_traits (note_id, base_hash, skill_name, phase, instruction)
            VALUES (?, ?, ?, ?, ?)
        """, (note_id, base_hash, skill.name, skill.phase, skill.instruction))
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


@router.get("/path")
def evolution_path(bot_id: int):
    with connect() as db:
        db.execute("BEGIN")
        base = db.execute("""
            SELECT p.prompt, b.role_mode, compiled.source_hash, compiled.version
            FROM bots b JOIN personas p ON p.id = b.persona_id
            LEFT JOIN agent_persona_programs compiled ON compiled.persona_id = p.id
            WHERE b.id = ?
        """, (bot_id,)).fetchone()
        if base is None:
            raise HTTPException(404, "Agent 不存在。")
        base_hash = persona_source_hash(base["prompt"])
        rows = db.execute(f"""
            SELECT {_NOTE_FIELDS}, {_SKILL_FIELD}, CASE
                WHEN NOT ({visible_agent_message}) THEN 'source_hidden'
                WHEN t.note_id IS NOT NULL AND (t.base_hash != ? OR ? = 'mirror') THEN 'previous_base'
                WHEN n.active = 0 THEN 'superseded'
                ELSE 'active' END AS status
            {_NOTE_FROM} {_TRAIT_JOIN} WHERE n.bot_id = ? ORDER BY n.id
        """, (base_hash, base["role_mode"], bot_id))
        events = []
        revisions = {}
        for row in rows:
            event = _public_note(row)
            event["previous"] = revisions.get(event["replaces_id"])
            revisions[event["id"]] = {"text": event["text"], "skill": event["skill"]}
            events.append(event)
        return {
            "enabled": _enabled(db, bot_id),
            "base_description": base["prompt"],
            "base_version": base["version"] if base["source_hash"] == base_hash else None,
            "traits": active_traits(db, bot_id, base_hash),
            "events": events,
        }


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
