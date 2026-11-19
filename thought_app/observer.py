import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from . import llm
from .config import get_model, get_summary_model_id
from .database import connect
from .journal import timestamp
from .memory import GENERIC_TERMS, conversation_terms, normalize, overlap_score
from .prompts import load_prompt


LOG = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
SOURCE_LIMIT = 4800
OBSERVER_PROMPT = load_prompt("observer")


Keyword = Annotated[str, StringConstraints(min_length=2, max_length=48)]


class ObservationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    kind: Literal["fact", "time", "mood", "value", "stance", "concern", "style", "relation"]
    text: str = Field(min_length=3, max_length=280)
    certainty: Literal["explicit", "inferred"]
    evidence: str = Field(min_length=2, max_length=300)
    keywords: list[Keyword] = Field(min_length=1, max_length=6)
    related_message_id: int | None = Field(default=None, gt=0)
    related_evidence: str | None = Field(default=None, min_length=2, max_length=300)
    relation: Literal["related", "contrast"] | None = None


def init_observer(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS observer_jobs (
            source_message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
            state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'running', 'complete', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            ready_at REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS observer_jobs_ready ON observer_jobs(state, ready_at, source_message_id);
        CREATE TABLE IF NOT EXISTS observer_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('fact', 'time', 'mood', 'value', 'stance', 'concern', 'style', 'relation')),
            text TEXT NOT NULL,
            certainty TEXT NOT NULL CHECK(certainty IN ('explicit', 'inferred')),
            evidence TEXT NOT NULL,
            keywords_json TEXT NOT NULL,
            related_message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
            related_evidence TEXT,
            relation TEXT CHECK(relation IN ('related', 'contrast')),
            UNIQUE(source_message_id, kind, text)
        );
        CREATE INDEX IF NOT EXISTS observer_observations_source ON observer_observations(source_message_id);
        CREATE INDEX IF NOT EXISTS observer_observations_related ON observer_observations(related_message_id);
        UPDATE observer_jobs SET state = 'pending', ready_at = 0 WHERE state = 'running';
    """)


def enqueue_observation(db, user_message_id):
    db.execute("""
        INSERT INTO observer_jobs (source_message_id, ready_at)
        SELECT id, ? FROM messages WHERE id = ? AND role = 'user'
        ON CONFLICT(source_message_id) DO NOTHING
    """, (time.time(), user_message_id))


OBSERVATIONS = """
    SELECT o.*, m.created_at, m.session_id, s.title AS session_title,
           pm.created_at AS related_created_at, pm.session_id AS related_session_id, ps.title AS related_session_title
    FROM observer_observations o JOIN messages m ON m.id = o.source_message_id AND m.role = 'user'
    JOIN sessions s ON s.id = m.session_id
    LEFT JOIN messages pm ON pm.id = o.related_message_id AND pm.role = 'user'
    LEFT JOIN sessions ps ON ps.id = pm.session_id
"""


def session_observations(db, session_id):
    rows = db.execute(OBSERVATIONS + """
        WHERE m.session_id = ?
        ORDER BY o.source_message_id DESC, o.id LIMIT 150
    """, (session_id,))
    return [
        {
            "id": row["id"], "kind": row["kind"], "text": row["text"], "certainty": row["certainty"],
            "evidence": row["evidence"], "source_message_id": row["source_message_id"],
            "created_at": row["created_at"], "related_message_id": row["related_message_id"],
            "related_session_id": row["related_session_id"], "related_evidence": row["related_evidence"],
            "relation": row["relation"], "keywords": json.loads(row["keywords_json"]),
        }
        for row in rows
    ]


def observer_pending_count(db, session_id):
    return _job_count(db, session_id, "j.state IN ('pending', 'running')")


def observer_failed_count(db, session_id):
    return _job_count(db, session_id, "j.state = 'failed'")


def _job_count(db, session_id, condition):
    return db.execute("""
        SELECT COUNT(*) FROM observer_jobs j JOIN messages m ON m.id = j.source_message_id
        WHERE m.session_id = ? AND m.role = 'user' AND """ + condition, (session_id,)).fetchone()[0]


def _local_time(value):
    try:
        return datetime.fromisoformat(value).astimezone().isoformat(timespec="minutes")
    except (ValueError, TypeError, OverflowError):
        return value


def _keyword_score(keywords, query):
    score = 0
    for word in keywords:
        term = normalize(word)
        if term.isascii() and term.isalnum():
            matched = re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", query)
        else:
            matched = term in query
        if matched and term not in GENERIC_TERMS:
            score += min(len(term), 8) * 4
    return score


def _scope(db, before_id):
    row = db.execute("SELECT session_id FROM messages WHERE id = ?", (before_id,)).fetchone()
    return {"before_id": before_id, "session_id": row["session_id"] if row else -1}


def _available_observations(db, before_id):
    return db.execute(OBSERVATIONS + """
        WHERE m.id < :before_id
          AND (m.session_id = :session_id OR s.summary_share_enabled = 1)
          AND (o.related_message_id IS NULL OR (
              pm.id < m.id AND (pm.session_id = :session_id OR ps.summary_share_enabled = 1)
          ))
        ORDER BY o.source_message_id DESC, o.id DESC
    """, _scope(db, before_id))


def _prior_user_messages(db, before_id, *, limit):
    return db.execute("""
        SELECT m.id, m.session_id, s.title AS session_title, m.created_at, SUBSTR(m.content, 1, :length) AS content
        FROM messages m JOIN sessions s ON s.id = m.session_id
        WHERE m.id < :before_id AND m.role = 'user'
          AND (m.session_id = :session_id OR s.summary_share_enabled = 1)
        ORDER BY m.id DESC LIMIT :limit
    """, {**_scope(db, before_id), "limit": limit, "length": 1600}).fetchall()


def _cite(citations, message_id, session_id, title, label="观察切片原话"):
    token = f"M{message_id}"
    citations[token] = {"session_id": session_id, "session_title": title, "message_id": message_id, "label": label}
    return f"[{token}]"


def _to_context(rows, citations):
    result = []
    for row in rows:
        fragment = {
            "kind": row["kind"],
            "text": row["text"],
            "certainty": row["certainty"],
            "evidence": row["evidence"],
            "date": _local_time(row["created_at"]),
            "source": _cite(citations, row["source_message_id"], row["session_id"], row["session_title"]),
        }
        if row["related_message_id"] is not None:
            fragment["related"] = {
                "type": row["relation"],
                "evidence": row["related_evidence"],
                "date": _local_time(row["related_created_at"]),
                "source": _cite(
                    citations, row["related_message_id"], row["related_session_id"], row["related_session_title"],
                ),
            }
        result.append(fragment)
    return result


def _ranked_observations(rows, query):
    normalized = normalize(query[:SOURCE_LIMIT])
    terms = conversation_terms(normalized)
    matches = []
    for row in rows:
        score = _keyword_score(json.loads(row["keywords_json"]), normalized) + overlap_score(terms, row["evidence"])
        if score:
            matches.append((score, row))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in matches]


def recall_observations(db, query, before_id):
    rows = _ranked_observations(_available_observations(db, before_id), query)
    citations = {}
    return _to_context(_distinct(rows, 4), citations), citations


def _distinct(rows, limit):
    unique = []
    seen = set()
    for row in rows:
        key = row["kind"], normalize(row["text"]).strip("。！？，、.? ,!")
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
        if len(unique) >= limit:
            break
    return unique


def mirror_context(db, query, before_id):
    rows = list(_available_observations(db, before_id))
    related = _distinct(_ranked_observations(rows, query), 4)
    used = {row["id"] for row in related}
    stable = [
        row for row in rows if row["id"] not in used and row["kind"] in {"fact", "value", "stance", "concern", "style"}
    ]
    # Ground the profile in directly expressed material before including tentative observations.
    stable.sort(key=lambda row: row["certainty"] == "explicit", reverse=True)
    candidates = _prior_user_messages(db, before_id, limit=36)
    terms = conversation_terms(query[:SOURCE_LIMIT])
    candidates = [row for row in candidates if len(row["content"].strip()) >= 8]
    candidates.sort(key=lambda row: overlap_score(terms, row["content"]), reverse=True)
    citations = {}
    profile = {
        "related_observations": _to_context(related, citations),
        "expressed_perspectives": _to_context(_distinct(stable, 6), citations),
        "voice_samples": [
            {
                "example": row["content"][:280],
                "date": _local_time(row["created_at"]),
                "source": _cite(citations, row["id"], row["session_id"], row["session_title"], "用户原话"),
            }
            for row in candidates[:4]
        ],
    }
    return profile, citations


def _claim_job(db):
    db.execute("BEGIN IMMEDIATE")
    source = db.execute("""
        SELECT m.id, m.session_id, m.created_at, m.content, j.attempts
        FROM observer_jobs j JOIN messages m ON m.id = j.source_message_id AND m.role = 'user'
        WHERE j.state = 'pending' AND j.ready_at <= ?
        ORDER BY j.ready_at, j.source_message_id LIMIT 1
    """, (time.time(),)).fetchone()
    if source is None:
        return None
    job = dict(source)
    job["attempts"] += 1
    db.execute("""
        UPDATE observer_jobs SET state = 'running', attempts = ? WHERE source_message_id = ?
    """, (job["attempts"], job["id"]))
    return job


def _relation_sources(db, job):
    query = job["content"][:SOURCE_LIMIT]
    terms = conversation_terms(query)
    candidates = _prior_user_messages(db, job["id"], limit=60)
    recent_by_id = {row["id"]: row for row in candidates}
    observed = _ranked_observations(_available_observations(db, job["id"]), query)
    observed_ids = list(dict.fromkeys(row["source_message_id"] for row in observed))[:4]
    observed_sources = []
    for message_id in observed_ids:
        source = recent_by_id.get(message_id)
        if source is None:
            source = db.execute("""
                SELECT id, session_id, created_at, SUBSTR(content, 1, 1600) AS content FROM messages
                WHERE id = ? AND role = 'user'
            """, (message_id,)).fetchone()
        if source is not None:
            observed_sources.append(source)
    matches = [
        (score, row) for row in candidates if (score := overlap_score(terms, row["content"]))
    ]
    matches.sort(key=lambda item: item[0], reverse=True)
    found = [*observed_sources, *(row for _, row in matches)]
    relevant = list({row["id"]: row for row in found}.values())[:4]
    selected = {row["id"] for row in relevant}
    recent = [row for row in candidates if row["session_id"] == job["session_id"] and row["id"] not in selected]
    return {
        row["id"]: {"id": row["id"], "created_at": _local_time(row["created_at"]), "content": row["content"]}
        for row in relevant + recent[:2]
    }


def _parse_result(raw, job, prior):
    value = raw.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value.split("\n", 1)[-1][:-3].strip()
    try:
        items = json.loads(value)["memory_to_save"]
    except (ValueError, TypeError, KeyError) as error:
        raise llm.ModelError("观察器返回了无效格式。") from error
    if not isinstance(items, list):
        raise llm.ModelError("观察器未返回切片列表。")

    observations = []
    seen = set()
    for item in items[:5]:
        try:
            draft = ObservationDraft.model_validate(item)
        except ValidationError:
            continue
        if draft.evidence not in job["content"][:SOURCE_LIMIT]:
            continue
        if draft.kind in {"fact", "time", "mood"} and draft.certainty != "explicit":
            continue
        if draft.kind == "relation":
            older = prior.get(draft.related_message_id)
            if not (older and draft.relation and draft.related_evidence and draft.related_evidence in older["content"]):
                continue
            draft.certainty = "inferred"
        elif draft.related_message_id is not None or draft.related_evidence is not None or draft.relation is not None:
            continue
        key = draft.kind, normalize(draft.text).strip("。！？，、.? ,!")
        if key in seen:
            continue
        seen.add(key)
        keywords = {
            normalize(word): word for word in draft.keywords
            if normalize(word) not in GENERIC_TERMS and any(char.isalnum() for char in word)
        }
        observations.append((
            job["id"], draft.kind, draft.text, draft.certainty, draft.evidence,
            json.dumps(list(keywords.values()), ensure_ascii=False),
            draft.related_message_id, draft.related_evidence, draft.relation,
        ))
    return observations


def _finish_job(db, job, observations):
    db.execute("BEGIN IMMEDIATE")
    job_alive = db.execute("""
        SELECT 1 FROM observer_jobs j JOIN messages m ON m.id = j.source_message_id AND m.role = 'user'
        WHERE j.source_message_id = ? AND j.state = 'running' AND j.attempts = ?
    """, (job["id"], job["attempts"])).fetchone()
    if not job_alive:
        return

    for observation in observations:
        related_id = observation[-3]
        if related_id is not None:
            available = db.execute("""
                SELECT 1 FROM messages m JOIN sessions s ON s.id = m.session_id
                WHERE m.id = ? AND m.id < ? AND m.role = 'user'
                  AND (m.session_id = ? OR s.summary_share_enabled = 1)
            """, (related_id, job["id"], job["session_id"])).fetchone()
            if not available:
                continue
        db.execute("""
            INSERT INTO observer_observations (
                source_message_id, kind, text, certainty, evidence, keywords_json,
                related_message_id, related_evidence, relation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_message_id, kind, text) DO NOTHING
        """, observation)
    db.execute("""
        UPDATE observer_jobs SET state = 'complete' WHERE source_message_id = ?
    """, (job["id"],))


def _retry_job(db, job):
    final = job["attempts"] >= MAX_ATTEMPTS
    db.execute("""
        UPDATE observer_jobs SET state = ?, ready_at = ?
        WHERE source_message_id = ? AND state = 'running' AND attempts = ?
    """, ("failed" if final else "pending", time.time() + job["attempts"] * 15, job["id"], job["attempts"]))


async def _observe(job):
    with connect() as db:
        db.execute("BEGIN")
        model_id = get_summary_model_id(db)
        if model_id is None:
            raise llm.ModelError("未配置观察模型。")
        model = get_model(db, model_id)
        prior = _relation_sources(db, job)
    raw = await llm.complete({**model, "task": "observe"}, [
        {"role": "system", "content": OBSERVER_PROMPT},
        {"role": "user", "content": json.dumps({
            "当前用户原话": {"id": job["id"], "created_at": _local_time(job["created_at"]),
                            "content": job["content"][:SOURCE_LIMIT]},
            "较早的用户原话": list(prior.values()),
        }, ensure_ascii=False)},
    ], max_tokens=1100, json_output=model["kind"] == "deepseek")
    with connect() as db:
        _finish_job(db, job, _parse_result(raw, job, prior))


async def observer_worker(wake: asyncio.Event):
    """Drain durable jobs independently of chat requests, waking on new input or retries."""
    while True:
        wake.clear()
        with connect() as db:
            job = _claim_job(db)
            next_ready = None
            if job is None:
                next_ready = db.execute(
                    """
                    SELECT MIN(j.ready_at)
                    FROM observer_jobs j JOIN messages m ON m.id = j.source_message_id
                    WHERE j.state = 'pending' AND m.role = 'user'
                    """
                ).fetchone()[0]
        if job is None:
            if next_ready is None:
                await wake.wait()
            else:
                try:
                    await asyncio.wait_for(wake.wait(), timeout=max(next_ready - time.time(), 0.05))
                except asyncio.TimeoutError:
                    pass
            continue
        try:
            await _observe(job)
        except (llm.ModelError, HTTPException):
            with connect() as db:
                _retry_job(db, job)
        except Exception as error:
            # An unexpected model/adapter payload must not prevent processing later turns.
            LOG.warning("Observer job %s failed (%s)", job["id"], type(error).__name__)
            with connect() as db:
                _retry_job(db, job)
