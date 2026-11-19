from fastapi import APIRouter, HTTPException, Request, Response

from ..chat import chat
from ..database import connect
from ..evolution import rewind_evolution
from ..journal import (
    ChatInput, InterjectInput, RewindInput, SessionCreate, SessionPatch, SESSION_SELECT,
    clean_session, get_messages, get_session, get_summary, timestamp,
)
from ..observer import observer_failed_count, observer_pending_count, session_observations


router = APIRouter(prefix="/api/sessions")


@router.get("")
def list_sessions():
    with connect() as db:
        rows = db.execute(SESSION_SELECT + " ORDER BY s.updated_at DESC, s.id DESC")
        return [clean_session(row) for row in rows]


@router.post("", status_code=201)
def create_session(body: SessionCreate | None = None):
    now = timestamp()
    with connect() as db:
        session_id = db.execute(
            "INSERT INTO sessions (title, created_at, updated_at) VALUES (?, ?, ?)",
            ((body or SessionCreate()).title, now, now),
        ).lastrowid
        return get_session(db, session_id)


@router.get("/{session_id}")
async def session_detail(request: Request, session_id: int):
    runtime = request.app.state.runtime
    with connect() as db:
        db.execute("BEGIN")
        draft = db.execute("SELECT content FROM rewind_drafts WHERE session_id = ?", (session_id,)).fetchone()
        return {
            "session": get_session(db, session_id),
            "messages": get_messages(db, session_id),
            "summary": get_summary(db, session_id),
            "rewind_draft": draft["content"] if draft else None,
            "observations": session_observations(db, session_id),
            "observer_pending": observer_pending_count(db, session_id),
            "observer_failed": observer_failed_count(db, session_id),
            "side_active": session_id in runtime.sides.tasks,
            "side_error": runtime.sides.errors.get(session_id),
        }


@router.patch("/{session_id}")
async def patch_session(request: Request, session_id: int, body: SessionPatch):
    runtime = request.app.state.runtime
    changes = body.model_dump(exclude_none=True)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        session = get_session(db, session_id)
        if "title" in changes or "archived" in changes:
            db.execute("""
                UPDATE sessions SET title = ?, archived = ?, updated_at = ? WHERE id = ?
            """, (changes.get("title", session["title"]), changes.get("archived", session["archived"]),
                  timestamp(), session_id))
        if "summary_share_enabled" in changes:
            shared = changes["summary_share_enabled"]
            db.execute("UPDATE sessions SET summary_share_enabled = ? WHERE id = ?", (shared, session_id))
            db.execute(
                "UPDATE summaries SET share_enabled = ?, updated_at = ? WHERE session_id = ?",
                (shared, timestamp(), session_id),
            )
        if changes.get("archived"):
            runtime.sides.stop(session_id)
        return get_session(db, session_id)


@router.delete("/{session_id}", status_code=204)
async def delete_session(request: Request, session_id: int):
    runtime = request.app.state.runtime
    if session_id in runtime.chats or session_id in runtime.summaries:
        raise HTTPException(409, "当前会话正在生成内容，请稍后删除。")
    with connect() as db:
        get_session(db, session_id)
        runtime.sides.stop(session_id)
        db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return Response(status_code=204)


@router.post("/{session_id}/chat")
async def chat_route(request: Request, session_id: int, body: ChatInput):
    runtime = request.app.state.runtime
    active = runtime.chats
    if session_id in active:
        raise HTTPException(409, "这一轮还在生成，请等机器人回复。")
    active.add(session_id)
    runtime.sides.stop(session_id)
    try:
        outcome, intent = await chat(session_id, body, runtime.observer_wake)
        runtime.sides.maybe_start(session_id, outcome, intent, body.bot_ids)
        return {**outcome, "side_active": session_id in runtime.sides.tasks}
    finally:
        active.remove(session_id)


@router.post("/{session_id}/side", status_code=202)
async def interject(request: Request, session_id: int, body: InterjectInput):
    runtime = request.app.state.runtime
    if session_id in runtime.chats:
        raise HTTPException(409, "等主聊的回复出来后再请角色插嘴。")
    runtime.sides.manual_start(session_id, body.bot_id, body.message_id)
    return {"started": True}


@router.post("/{session_id}/side/stop", status_code=204)
async def stop_side(request: Request, session_id: int):
    runtime = request.app.state.runtime
    with connect() as db:
        get_session(db, session_id)
    runtime.sides.stop(session_id)
    return Response(status_code=204)


@router.post("/{session_id}/rewind")
async def rewind_session(request: Request, session_id: int, body: RewindInput):
    runtime = request.app.state.runtime
    if session_id in runtime.chats or session_id in runtime.summaries:
        raise HTTPException(409, "当前会话正在生成内容，等它完成后再回退。")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        session = get_session(db, session_id)
        if session["archived"]:
            raise HTTPException(409, "恢复会话后就可以回退。")
        target = db.execute(
            "SELECT role, content FROM messages WHERE session_id = ? AND id = ?",
            (session_id, body.message_id),
        ).fetchone()
        if target is None:
            raise HTTPException(404, "这条消息不在当前会话中。")
        if target["role"] != "user":
            raise HTTPException(422, "请从一条自己的消息开始回退。")

        runtime.sides.stop(session_id)
        restored = rewind_evolution(db, session_id, body.message_id)
        summary = get_summary(db, session_id)
        summary_removed = bool(summary and (
            summary["through_message_id"] >= body.message_id or any(
                source_id >= body.message_id
                for field in ("views", "observations")
                for point in summary[field]
                for source_id in point["source_ids"]
            )
        ))
        if summary_removed:
            db.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
        cutoff = (session_id, body.message_id)
        removed = db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND id >= ?", cutoff,
        ).fetchone()[0]
        db.execute("DELETE FROM messages WHERE session_id = ? AND id >= ?", cutoff)
        db.execute("""
            INSERT INTO rewind_drafts (session_id, content) VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET content = excluded.content
        """, (session_id, target["content"]))
        db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (timestamp(), session_id))
        return {
            "draft": target["content"], "removed_count": removed,
            "summary_removed": summary_removed, "restored_note_count": restored,
        }
