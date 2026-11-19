import json

from fastapi import APIRouter, HTTPException, Request, Response

from .. import llm
from ..config import get_model, get_summary_model_id, set_summary_model_id
from ..conversation import Conversation
from ..database import connect
from ..journal import SummaryPatch, SummaryRequest, get_messages, get_session, get_summary, timestamp
from ..memory import SUMMARY_PROMPT, check_sources, parse_summary


router = APIRouter(prefix="/api/sessions")


@router.post("/{session_id}/summary")
async def generate_summary(request: Request, session_id: int, body: SummaryRequest | None = None):
    runtime = request.app.state.runtime
    active = runtime.summaries
    if session_id in active:
        raise HTTPException(409, "总结正在生成。")
    active.add(session_id)
    try:
        with connect() as db:
            session = get_session(db, session_id)
            messages = get_messages(db, session_id)
            if not messages:
                raise HTTPException(400, "有对话内容后才能生成总结。")
            old = get_summary(db, session_id)
            model_id = (body.model_id if body else None) or get_summary_model_id(db)
            if model_id is None:
                raise HTTPException(400, "请在运维后台配置一个可用的总结模型。")
            model = get_model(db, model_id)

        conversation = Conversation(messages)
        transcript = [conversation.event(message) for message in messages]
        try:
            raw = await llm.complete({**model, "task": "summary"}, [
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": json.dumps({
                    "会话标题": session["title"], "消息": transcript,
                }, ensure_ascii=False)},
            ], max_tokens=2600, json_output=True)
        except llm.ModelError as exc:
            raise HTTPException(502, str(exc)) from exc
        draft = parse_summary(raw, messages)

        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            shared = get_session(db, session_id)["summary_share_enabled"]
            current = get_summary(db, session_id)
            old_version = old and old["updated_at"]
            current_version = current and current["updated_at"]
            if old_version != current_version:
                raise HTTPException(409, "生成期间总结已被编辑，请重新生成。")
            db.execute("""
                INSERT INTO summaries (
                    session_id, title, keywords, views, observations, questions, share_enabled,
                    through_message_id, updated_at, model_id, model_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    title = excluded.title, keywords = excluded.keywords, views = excluded.views,
                    observations = excluded.observations, questions = excluded.questions,
                    through_message_id = excluded.through_message_id, updated_at = excluded.updated_at,
                    model_id = excluded.model_id, model_name = excluded.model_name
            """, (
                session_id, draft["title"], *(json.dumps(draft[key], ensure_ascii=False)
                                             for key in ("keywords", "views", "observations", "questions")),
                shared, messages[-1]["id"], timestamp(), model["id"], model["name"],
            ))
            set_summary_model_id(db, model["id"])
            return get_summary(db, session_id)
    finally:
        active.remove(session_id)


@router.patch("/{session_id}/summary")
def patch_summary(session_id: int, body: SummaryPatch):
    changes = body.model_dump(exclude_none=True)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        summary = get_summary(db, session_id)
        if summary is None:
            raise HTTPException(404, "这个会话还没有总结。")
        if not changes:
            return summary
        messages = get_messages(db, session_id)
        check_sources(changes, messages, generated=False)
        fields = ("title", "keywords", "views", "observations", "questions", "share_enabled")
        values = summary | changes
        for field in ("keywords", "views", "observations", "questions"):
            values[field] = json.dumps(values[field], ensure_ascii=False)
        db.execute("""
            UPDATE summaries SET title = ?, keywords = ?, views = ?, observations = ?, questions = ?,
                share_enabled = ?, updated_at = ? WHERE session_id = ?
        """, (*(values[field] for field in fields), timestamp(), session_id))
        if "share_enabled" in changes:
            db.execute(
                "UPDATE sessions SET summary_share_enabled = ? WHERE id = ?", (changes["share_enabled"], session_id),
            )
        return get_summary(db, session_id)


@router.delete("/{session_id}/summary", status_code=204)
def delete_summary(session_id: int):
    with connect() as db:
        if not db.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,)).rowcount:
            raise HTTPException(404, "这个会话还没有总结。")
    return Response(status_code=204)
