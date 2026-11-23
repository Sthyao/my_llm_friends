"""Agent settings, persona compilation, and the shared model adapter."""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from exp.harness import COMPILER, persona_source_hash, validate_profile

from . import llm
from .config import ConfigInput, get_bot, get_model, get_router_model
from .database import connect


ThinkingLevel = Literal["off", "low", "high", "max"]
AgentThinkingLevel = Literal["default", "off", "low", "high", "max"]
_DEFAULTS = {
    "enabled": True,
    "compile_thinking": "low",
    "decision_thinking": "off",
    "reply_thinking": "off",
}
_THINKING_LEVELS = {"off", "low", "high", "max"}
_MAX_TOKENS = {
    "compile": 4096,
    "recall": 350,
    "decide": 650,
    "archive_summarize": 1200,
    "express": 1800,
    "check_reply": 150,
}
_STAGE_LABELS = {
    "compile": "人格编译",
    "recall": "记忆筛选",
    "decide": "行动判断",
    "archive_summarize": "历史归纳",
    "express": "回复生成",
    "check_reply": "回复检查",
}
AGENT_SETTING_KEYS = tuple(f"agent_{key}" for key in _DEFAULTS)
router = APIRouter(prefix="/api/config/agents")

# Keep this guidance outside COMPILER so existing author and evolution fingerprints stay stable.
_COMPILE_INSTRUCTION = COMPILER + """
每轮回复的篇幅、展开程度和主动程度由本轮路由及用户的要求决定。
把原文对日常回复的固定句数、段数和字数约束转成背后的表达偏好，例如聚焦具体细节或先交代判断依据。
在 express 和 voice 中保留这种表达偏好、角色语气及互动界限。"""


class AgentSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool = True
    compile_thinking: ThinkingLevel = "low"
    decision_thinking: ThinkingLevel = "off"
    reply_thinking: ThinkingLevel = "off"


class AgentInput(ConfigInput):
    model_id: Annotated[int, Field(gt=0, strict=True)]
    prompt: Annotated[str, Field(min_length=1, max_length=12000)]
    role_mode: Literal["friend", "mirror"] = "friend"
    enabled: bool = True
    web_search: bool = False
    thinking_effort: AgentThinkingLevel = "default"


class PersonaChanged(llm.ModelError):
    def __init__(self):
        super().__init__("人格配置已变化，请刷新后重试。", "config")


def init_agent_config(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS agent_persona_programs (
            persona_id INTEGER PRIMARY KEY REFERENCES personas(id) ON DELETE CASCADE,
            source_hash TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            compiled_json TEXT NOT NULL,
            model_name TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS agent_thinking (
            bot_id INTEGER PRIMARY KEY REFERENCES bots(id) ON DELETE CASCADE,
            effort TEXT NOT NULL CHECK (effort IN ('off', 'low', 'high', 'max'))
        )
    """)


def agent_settings(db):
    keys = ", ".join("?" for _ in AGENT_SETTING_KEYS)
    saved = dict(db.execute(f"SELECT key, value FROM config_settings WHERE key IN ({keys})", AGENT_SETTING_KEYS))
    result = dict(_DEFAULTS)
    for key, default in _DEFAULTS.items():
        value = saved.get(f"agent_{key}")
        if isinstance(default, bool):
            if value in {"0", "1"}:
                result[key] = value == "1"
        elif value in _THINKING_LEVELS:
            result[key] = value
    return result


def effective_agent_settings(bot, preferences):
    override = bot.get("thinking_effort")
    if override in _THINKING_LEVELS and llm.supports_thinking(bot):
        return {**preferences, "decision_thinking": override, "reply_thinking": override}
    return preferences


class AgentModel:
    def __init__(self, bot, preferences):
        self.bot = bot
        self.preferences = effective_agent_settings(bot, preferences)

    async def ask(self, stage, instruction, data, *, thinking=False):
        if stage == "compile":
            setting = "compile_thinking"
        elif stage == "express":
            setting = "reply_thinking"
        else:
            setting = "decision_thinking"
        effort = self.preferences.get(setting, _DEFAULTS[setting])
        settings = {**self.bot, "task": f"agent_{stage}", "thinking_effort": effort}
        guidance = instruction + "\n仅返回符合本阶段约定的 JSON 对象。"
        payload = {"role": "user", "content": json.dumps(data, ensure_ascii=False)}
        budget = _MAX_TOKENS.get(stage, 650)
        # Compilation already retries in _compile, which also checks the persona schema.
        attempts = 1 if stage == "compile" else 2
        if stage == "compile" and "validation_error" in data:
            budget *= 2
        for attempt in range(attempts):
            try:
                raw = await llm.complete(
                    settings,
                    [{"role": "system", "content": guidance}, payload],
                    max_tokens=llm.agent_output_limit(settings, budget),
                    json_output=settings["kind"] == "deepseek", require_complete=True,
                )
                return self._parse(raw)
            except llm.ModelError as exc:
                if exc.outcome != "invalid_response":
                    raise
                if attempt + 1 == attempts:
                    stage_label = _STAGE_LABELS.get(stage, stage)
                    raise llm.ModelError(f"Agent {stage_label}失败。{exc}") from exc
            budget *= 2
            guidance += "\n重新生成一个完整 JSON 对象，说明字段保持简短，省略格式外的文字。"

    @staticmethod
    def _parse(raw):
        if raw.startswith("```") and raw.endswith("```"):
            start = raw.find("\n")
            if start >= 0:
                raw = raw[start + 1:-3].strip()
        try:
            output = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            raise llm.ModelError("模型没有返回有效的 JSON。") from None
        if not isinstance(output, dict):
            raise llm.ModelError("模型没有返回 JSON 对象。")
        return output


def _program(row, source):
    if row is None or row["source_hash"] != persona_source_hash(source):
        return None
    try:
        compiled = validate_profile(json.loads(row["compiled_json"]), source)
    except (AttributeError, TypeError, ValueError, RecursionError):
        return None
    return {"version": row["version"], "model_name": row["model_name"], **compiled}


async def _compile(model, source):
    request = {"source": source}
    for attempt in range(2):
        try:
            raw = await model.ask("compile", _COMPILE_INSTRUCTION, request)
            return validate_profile(raw, source)
        except ValueError as exc:
            error = llm.ModelError(f"人格无法编译。{exc}")
        except llm.ModelError as exc:
            if exc.outcome != "invalid_response":
                raise
            error = exc
        if attempt:
            raise error
        request["validation_error"] = str(error)


async def ensure_profile(bot, preferences, *, force=False):
    """Compile the current authored persona on demand and reuse a matching program."""
    persona_id, source = bot["persona_id"], bot["prompt"]
    source_hash = persona_source_hash(source)
    with connect() as db:
        existing = db.execute(
            """SELECT p.prompt, a.source_hash, a.version, a.compiled_json, a.model_name
            FROM personas p LEFT JOIN agent_persona_programs a ON a.persona_id = p.id
            WHERE p.id = ?""", (persona_id,),
        ).fetchone()
    if existing is None or existing["prompt"] != source:
        raise PersonaChanged()
    cached = _program(existing, source)
    if cached is not None and not force:
        return cached

    compiled = await _compile(AgentModel(bot, preferences), source)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT prompt FROM personas WHERE id = ?", (persona_id,)).fetchone()
        if not current or current["prompt"] != source:
            raise PersonaChanged()
        if bot_id := bot.get("bot_id"):
            binding = db.execute("SELECT persona_id, model_id FROM bots WHERE id = ?", (bot_id,)).fetchone()
            if not binding or (binding["persona_id"], binding["model_id"]) != (persona_id, bot["id"]):
                raise PersonaChanged()
        existing = db.execute(
            "SELECT * FROM agent_persona_programs WHERE persona_id = ?", (persona_id,),
        ).fetchone()
        cached = _program(existing, source)
        if cached is not None and not force:
            return cached
        version = existing["version"] + 1 if existing else 1
        db.execute("""
            INSERT INTO agent_persona_programs (persona_id, source_hash, version, compiled_json, model_name)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(persona_id) DO UPDATE SET source_hash = excluded.source_hash,
                version = excluded.version, compiled_json = excluded.compiled_json, model_name = excluded.model_name
        """, (persona_id, source_hash, version, json.dumps(compiled, ensure_ascii=False), bot["name"]))
    return {"version": version, "model_name": bot["name"], **compiled}


def _public_persona(row):
    program = _program(row, row["prompt"])
    result = {"id": row["id"], "name": row["name"]}
    if program is None:
        if row["version"] is None:
            return {**result, "status": "uncompiled"}
        return {**result, "status": "stale", "version": row["version"], "model_name": row["model_name"]}
    return {**result, "status": "ready", **program}


_AGENTS_SELECT = """
    SELECT b.*, p.prompt, p.web_search, COALESCE(t.effort, 'default') AS thinking_effort,
           m.model_code, endpoint.base_url, endpoint.kind
    FROM bots b JOIN personas p ON p.id = b.persona_id
    JOIN models m ON m.id = b.model_id JOIN providers endpoint ON endpoint.id = m.provider_id
    LEFT JOIN agent_thinking t ON t.bot_id = b.id
"""


def _public_agent(row):
    data = dict(row)
    return {
        **{key: data[key] for key in ("id", "persona_id", "name", "model_id", "role_mode", "prompt", "thinking_effort")},
        "web_search": bool(data["web_search"]),
        "enabled": bool(data["enabled"]),
        "thinking_supported": llm.supports_thinking(data),
    }


def _read_agent(db, bot_id):
    row = db.execute(_AGENTS_SELECT + " WHERE b.id = ?", (bot_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Agent 不存在。")
    return _public_agent(row)


def _public_settings(db):
    from .persona_defaults import agent_starters

    rows = db.execute("""
        SELECT p.id, p.name, p.prompt, a.source_hash, a.version, a.compiled_json, a.model_name
        FROM personas p LEFT JOIN agent_persona_programs a ON p.id = a.persona_id ORDER BY p.id
    """)
    personas = [_public_persona(row) for row in rows]
    return {
        **agent_settings(db), "personas": personas, "starters": agent_starters(),
        "agents": [_public_agent(row) for row in db.execute(_AGENTS_SELECT + " ORDER BY b.id")],
    }


@router.get("")
def read_agent_settings():
    with connect() as db:
        return _public_settings(db)


@router.patch("")
def update_agent_settings(body: AgentSettingsPatch):
    changes = body.model_dump(exclude_unset=True)
    with connect() as db:
        db.executemany("""
            INSERT INTO config_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, [(f"agent_{key}", str(int(value)) if type(value) is bool else value) for key, value in changes.items()])
        return _public_settings(db)


def _save_thinking(db, bot_id, effort):
    if effort == "default":
        db.execute("DELETE FROM agent_thinking WHERE bot_id = ?", (bot_id,))
    else:
        db.execute("""
            INSERT INTO agent_thinking (bot_id, effort) VALUES (?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET effort = excluded.effort
        """, (bot_id, effort))


def _new_persona(db, agent):
    return db.execute(
        "INSERT INTO personas (name, prompt, web_search) VALUES (?, ?, ?)",
        (agent.name, agent.prompt, agent.web_search),
    ).lastrowid


@router.post("", status_code=201)
def create_agent(body: AgentInput):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        get_model(db, body.model_id, allow_disabled=True)
        persona_id = _new_persona(db, body)
        bot_id = db.execute("""
            INSERT INTO bots (name, model_id, persona_id, enabled, role_mode) VALUES (?, ?, ?, ?, ?)
        """, (body.name, body.model_id, persona_id, body.enabled, body.role_mode)).lastrowid
        _save_thinking(db, bot_id, body.thinking_effort)
        return _read_agent(db, bot_id)


@router.patch("/{bot_id}")
def edit_agent(bot_id: int, changes: dict):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _read_agent(db, bot_id)
        fields = {field: current[field] for field in AgentInput.model_fields}
        try:
            updated = AgentInput.model_validate({**fields, **changes})
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from None
        if not changes:
            return current
        get_model(db, updated.model_id, allow_disabled=True)
        persona_id = current["persona_id"]
        personal_change = updated.prompt != current["prompt"] or updated.web_search != current["web_search"]
        shared = db.execute(
            "SELECT 1 FROM bots WHERE persona_id = ? AND id != ?", (persona_id, bot_id),
        ).fetchone()
        if shared and personal_change:
            persona_id = _new_persona(db, updated)
            # Retain the status of the last compilation. The source hash detects edited text.
            db.execute("""
                INSERT INTO agent_persona_programs (persona_id, source_hash, version, compiled_json, model_name)
                SELECT ?, source_hash, version, compiled_json, model_name FROM agent_persona_programs
                WHERE persona_id = ?
            """, (persona_id, current["persona_id"]))
        elif not shared and (personal_change or updated.name != current["name"]):
            db.execute("UPDATE personas SET name = ?, prompt = ?, web_search = ? WHERE id = ?",
                       (updated.name, updated.prompt, updated.web_search, persona_id))
        db.execute("""
            UPDATE bots SET name = ?, model_id = ?, persona_id = ?, enabled = ?, role_mode = ? WHERE id = ?
        """, (updated.name, updated.model_id, persona_id, updated.enabled, updated.role_mode, bot_id))
        _save_thinking(db, bot_id, updated.thinking_effort)
        return _read_agent(db, bot_id)


@router.delete("/{bot_id}", status_code=204)
def delete_agent(bot_id: int):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        agent = _read_agent(db, bot_id)
        db.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
        db.execute("""
            DELETE FROM personas WHERE id = ?
            AND NOT EXISTS (SELECT 1 FROM bots WHERE persona_id = personas.id)
        """, (agent["persona_id"],))
    return Response(status_code=204)


async def _compile_response(bot, preferences, persona_name):
    try:
        program = await ensure_profile(bot, preferences, force=True)
    except PersonaChanged as exc:
        raise HTTPException(409, str(exc)) from None
    except llm.ModelError as exc:
        raise HTTPException(502, str(exc)) from None
    return {"id": bot["persona_id"], "name": persona_name, "status": "ready", **program}


@router.post("/{bot_id}/compile")
async def compile_agent(bot_id: int):
    with connect() as db:
        bot = get_bot(db, bot_id, allow_disabled=True)
        if not bot["api_key"]:
            raise HTTPException(400, "请先为此 Agent 的模型配置 API 密钥。")
        name = db.execute("SELECT name FROM personas WHERE id = ?", (bot["persona_id"],)).fetchone()["name"]
        preferences = agent_settings(db)
    return await _compile_response(bot, preferences, name)


def _compile_model(db, persona_id):
    attached = db.execute("""
        SELECT DISTINCT m.id FROM bots b JOIN models m ON m.id = b.model_id
        WHERE b.persona_id = ? AND b.enabled = 1 AND m.enabled = 1 ORDER BY m.id
    """, (persona_id,)).fetchall()
    if attached:
        models = [get_model(db, row["id"]) for row in attached]
    else:
        preferred = get_router_model(db)
        if preferred and preferred["api_key"]:
            return preferred
        rows = db.execute("SELECT id FROM models WHERE enabled = 1 ORDER BY id").fetchall()
        models = [get_model(db, row["id"]) for row in rows]
    available = (model for model in models if model["api_key"])
    return max(available, key=llm.supports_thinking, default=None)


@router.post("/personas/{persona_id}/compile")
async def compile_persona(persona_id: int):
    with connect() as db:
        row = db.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "人格模板不存在。")
        model = _compile_model(db, persona_id)
        if model is None:
            raise HTTPException(400, "请启用一个已配置 API 密钥的模型。")
        preferences = agent_settings(db)
    return await _compile_response({**model, "persona_id": persona_id, "prompt": row["prompt"]},
                                   preferences, row["name"])
