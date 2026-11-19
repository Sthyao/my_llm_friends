from datetime import datetime, timezone
import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing import Annotated


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Title = Annotated[str, StringConstraints(min_length=1, max_length=120)]
Keyword = Annotated[str, StringConstraints(min_length=1, max_length=80)]
Note = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class SessionCreate(Input):
    title: Title = "新会话"


class SessionPatch(Input):
    title: Title | None = None
    archived: bool | None = None
    summary_share_enabled: bool | None = None


class ChatInput(Input):
    content: str = Field(min_length=1, max_length=20000)
    bot_ids: list[int] | None = Field(default=None, max_length=6)
    discuss: bool = False
    rewind_message_id: int | None = Field(default=None, gt=0, strict=True)


class InterjectInput(Input):
    bot_id: int = Field(gt=0, strict=True)
    message_id: int = Field(gt=0, strict=True)


class RewindInput(Input):
    message_id: int = Field(gt=0, strict=True)


class SummaryRequest(Input):
    model_id: int | None = None


class SummaryPoint(Input):
    text: Note
    source_ids: list[int] = Field(default_factory=list, max_length=12)


class SummaryDraft(Input):
    title: Title
    keywords: list[Keyword] = Field(max_length=24)
    views: list[SummaryPoint] = Field(max_length=30)
    observations: list[SummaryPoint] = Field(max_length=30)
    questions: list[Note] = Field(max_length=30)


class SummaryPatch(Input):
    title: Title | None = None
    keywords: list[Keyword] | None = Field(default=None, max_length=24)
    views: list[SummaryPoint] | None = Field(default=None, max_length=30)
    observations: list[SummaryPoint] | None = Field(default=None, max_length=30)
    questions: list[Note] | None = Field(default=None, max_length=30)
    share_enabled: bool | None = None


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


SESSION_SELECT = """
    SELECT s.*, EXISTS (SELECT 1 FROM summaries m WHERE m.session_id = s.id) AS has_summary
    FROM sessions s
"""


def clean_session(row):
    session = dict(row)
    session["archived"] = bool(session["archived"])
    session["summary_share_enabled"] = bool(session["summary_share_enabled"])
    session["has_summary"] = bool(session["has_summary"])
    return session


def get_session(db, session_id):
    row = db.execute(SESSION_SELECT + " WHERE s.id = ?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "会话不存在。")
    return clean_session(row)


def get_summary(db, session_id):
    row = db.execute("""
        SELECT m.*, EXISTS (
            SELECT 1 FROM messages q WHERE q.session_id = m.session_id AND q.id > m.through_message_id
        ) AS stale
        FROM summaries m WHERE m.session_id = ?
    """, (session_id,)).fetchone()
    if not row:
        return None
    summary = dict(row)
    for field in ("keywords", "views", "observations", "questions"):
        summary[field] = json.loads(summary[field])
    for field in ("stale", "share_enabled"):
        summary[field] = bool(summary[field])
    return summary


def clean_message(row):
    message = dict(row)
    audience = message.pop("audience_json")
    message["audience"] = json.loads(audience) if audience is not None else None
    message["references"] = json.loads(message.pop("references_json"))
    return message


def get_messages(db, session_id):
    rows = db.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,))
    messages = [clean_message(row) for row in rows]
    references = [ref for message in messages for ref in message["references"]]
    if references:
        session_ids = {row["id"] for row in db.execute("SELECT id FROM sessions")}
        source_ids = list({ref["message_id"] for ref in references if ref.get("message_id") is not None})
        sources = {}
        for start in range(0, len(source_ids), 500):
            batch = source_ids[start:start + 500]
            placeholders = ", ".join("?" for _ in batch)
            sources.update(db.execute(f"SELECT id, session_id FROM messages WHERE id IN ({placeholders})", batch))
        for message in messages:
            message["references"] = [
                ref for ref in message["references"]
                if ref["session_id"] in session_ids
                and (ref.get("message_id") is None or sources.get(ref["message_id"]) == ref["session_id"])
            ]
    return messages


def add_message(db, session_id, role, content, *, bot_id=None, bot_name=None, round_number=None,
                reply_to=None, turn_id=None, channel="main", inner_thought=None, references=(), model=None, audience=None):
    now = timestamp()
    model = model or {}
    new_id = db.execute("""
        INSERT INTO messages
            (session_id, role, content, bot_id, bot_name, round, reply_to, turn_id, channel,
             inner_thought, provider_name, model_name, model_code, created_at, audience_json, references_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (session_id, role, content, bot_id, bot_name, round_number, reply_to, turn_id, channel,
          inner_thought, model.get("provider_name"), model.get("name"), model.get("model_code"), now,
          json.dumps(audience) if audience is not None else None, json.dumps(references, ensure_ascii=False))).lastrowid
    db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    row = db.execute("SELECT * FROM messages WHERE id = ?", (new_id,)).fetchone()
    return clean_message(row)
