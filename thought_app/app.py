from contextlib import asynccontextmanager
import json
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import llm
from .chat import chat
from .config import get_model, get_summary_model_id, init_config, router, set_summary_model_id
from .database import ROOT, connect, init_journal
from .journal import (
    ChatInput, SessionCreate, SessionPatch, SummaryPatch, SummaryRequest, SESSION_SELECT,
    clean_session, get_messages, get_session, get_summary, timestamp,
)
from .memory import SUMMARY_PROMPT, check_sources, parse_summary


@asynccontextmanager
async def lifespan(application):
    if "DEEPSEEK_API_KEY" not in os.environ:
        load_dotenv(ROOT / ".env")
    with connect() as db:
        db.execute("PRAGMA journal_mode = WAL")
        init_config(db)
        init_journal(db)
    application.state.chats = set()
    application.state.summaries = set()
    yield


app = FastAPI(title="想法簿", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, error: RequestValidationError):
    errors = [{field: entry[field] for field in ("loc", "msg", "type")} for entry in error.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.middleware("http")
async def protect_local_writes(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method in {"POST", "PATCH", "DELETE"} and origin:
        expected = f"{request.url.scheme}://{request.headers.get('host')}"
        if origin != expected:
            return JSONResponse({"detail": "请从本地应用页面操作。"}, status_code=403)
    return await call_next(request)


@app.get("/api/sessions")
def list_sessions():
    with connect() as db:
        rows = db.execute(SESSION_SELECT + " ORDER BY s.updated_at DESC, s.id DESC")
        return [clean_session(row) for row in rows]


@app.post("/api/sessions", status_code=201)
def create_session(body: SessionCreate | None = None):
    now = timestamp()
    with connect() as db:
        session_id = db.execute(
            "INSERT INTO sessions (title, created_at, updated_at) VALUES (?, ?, ?)",
            ((body or SessionCreate()).title, now, now),
        ).lastrowid
        return get_session(db, session_id)


@app.get("/api/sessions/{session_id}")
def session_detail(session_id: int):
    with connect() as db:
        return {
            "session": get_session(db, session_id),
            "messages": get_messages(db, session_id),
            "summary": get_summary(db, session_id),
        }


@app.patch("/api/sessions/{session_id}")
def patch_session(session_id: int, body: SessionPatch):
    changes = body.model_dump(exclude_none=True)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        session = get_session(db, session_id)
        if changes:
            db.execute("""
                UPDATE sessions SET title = ?, archived = ?, updated_at = ? WHERE id = ?
            """, (changes.get("title", session["title"]), changes.get("archived", session["archived"]),
                  timestamp(), session_id))
        return get_session(db, session_id)


@app.delete("/api/sessions/{session_id}", status_code=204)
async def delete_session(session_id: int):
    if session_id in app.state.chats or session_id in app.state.summaries:
        raise HTTPException(409, "当前会话正在生成内容，请稍后删除。")
    with connect() as db:
        get_session(db, session_id)
        db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return Response(status_code=204)


@app.post("/api/sessions/{session_id}/chat")
async def chat_route(session_id: int, body: ChatInput):
    active = app.state.chats
    if session_id in active:
        raise HTTPException(409, "这一轮还在生成，请等机器人回复。")
    active.add(session_id)
    try:
        return await chat(session_id, body)
    finally:
        active.remove(session_id)


@app.post("/api/sessions/{session_id}/summary")
async def generate_summary(session_id: int, body: SummaryRequest | None = None):
    active = app.state.summaries
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

        transcript = [
            {"id": msg["id"], "发言者": msg["bot_name"] or "用户", "正文": msg["content"]}
            for msg in messages
        ]
        try:
            raw = await llm.complete(model, [
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
                True, messages[-1]["id"], timestamp(), model["id"], model["name"],
            ))
            set_summary_model_id(db, model["id"])
            return get_summary(db, session_id)
    finally:
        active.remove(session_id)


@app.patch("/api/sessions/{session_id}/summary")
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
        return get_summary(db, session_id)


@app.delete("/api/sessions/{session_id}/summary", status_code=204)
def delete_summary(session_id: int):
    with connect() as db:
        if not db.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,)).rowcount:
            raise HTTPException(404, "这个会话还没有总结。")
    return Response(status_code=204)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
