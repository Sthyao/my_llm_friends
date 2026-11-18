import os
import sqlite3
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .database import connect
from .llm import ModelError, complete


DEEPSEEK_URL = "https://api.deepseek.com"
router = APIRouter(prefix="/api/config")

PERSONAS = (
    (
        "忧郁",
        "你说话安静、克制，容易留意一个念头里藏着的失落、留恋、犹豫或舍不得。"
        "从用户的一个具体用词出发，试着说清这种感受为什么值得停一停，用‘也许’保留解释的余地。"
        "允许感受悬着，语气温和，留意话里同时存在的轻松、期待或好奇。",
    ),
    (
        "客观",
        "用平实的白话把复杂想法理出头绪。分清用户说过的事、用户明确表达的感受，以及你据此作出的推测。"
        "挑一个会改变理解的区别说清楚。信息不足时标明不确定，必要时只问一个具体问题。"
        "有相关旧记录时，引用原话并带上各自的语境。",
    ),
    (
        "积极",
        "从用户话里找到一件仍有余地的事，例如一份好奇、一点已做到的进展，或一个可以试试的小选择。"
        "先承认眼前的困难，再说出你看见的那个具体可能。"
        "用户只想随口聊时，可以陪他们想象接下来会怎样，也允许这个念头暂时放在这里。",
    ),
    (
        "浪漫",
        "在日常的细节里找值得多看一眼的东西。可以借用户提到的场景、物件、声音或颜色，"
        "用一个简短的画面把想法往外展开半步。画面要贴着这次聊天，像朋友忽然想到的一句。"
        "说完画面后，用一句白话说出你真正想表达的观点。",
    ),
)


def init_config(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            kind TEXT NOT NULL,
            api_key TEXT,
            use_deepseek_env INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            provider_id INTEGER NOT NULL REFERENCES providers(id),
            model_code TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS personas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            model_id INTEGER NOT NULL REFERENCES models(id),
            persona_id INTEGER NOT NULL REFERENCES personas(id),
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS config_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    seeded = conn.execute(
        "INSERT INTO config_settings (key, value) VALUES ('seeded', '1') ON CONFLICT DO NOTHING"
    )
    if seeded.rowcount == 0:
        return

    provider_id = conn.execute(
        """INSERT INTO providers (name, base_url, kind, use_deepseek_env)
           VALUES (?, ?, ?, 1)""",
        ("DeepSeek", DEEPSEEK_URL, "deepseek"),
    ).lastrowid
    model_id = conn.execute(
        "INSERT INTO models (name, provider_id, model_code) VALUES (?, ?, ?)",
        ("DeepSeek V4.1 Flash", provider_id, "deepseek-flash"),
    ).lastrowid
    for name, prompt in PERSONAS:
        persona_id = conn.execute(
            "INSERT INTO personas (name, prompt) VALUES (?, ?)", (name, prompt)
        ).lastrowid
        conn.execute(
            "INSERT INTO bots (name, model_id, persona_id) VALUES (?, ?, ?)",
            (name, model_id, persona_id),
        )
    set_summary_model_id(conn, model_id)


def provider_key(provider: sqlite3.Row) -> str | None:
    if provider["api_key"]:
        return provider["api_key"]
    if (
        provider["use_deepseek_env"]
        and provider["kind"] == "deepseek"
        and provider["base_url"] == DEEPSEEK_URL
    ):
        return os.getenv("DEEPSEEK_API_KEY") or None
    return None


def get_model(conn: sqlite3.Connection, model_id: int) -> dict:
    model = conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
    if model is None:
        raise HTTPException(404, "模型不存在。")
    if not model["enabled"]:
        raise HTTPException(400, f"模型「{model['name']}」已停用。")
    provider = conn.execute(
        "SELECT * FROM providers WHERE id = ?", (model["provider_id"],)
    ).fetchone()
    return {
        "id": model["id"],
        "name": model["name"],
        "model_code": model["model_code"],
        "base_url": provider["base_url"],
        "kind": provider["kind"],
        "api_key": provider_key(provider),
    }


def get_bot(conn: sqlite3.Connection, bot_id: int) -> dict:
    bot = conn.execute(
        """SELECT bots.*, personas.prompt
           FROM bots JOIN personas ON personas.id = bots.persona_id
           WHERE bots.id = ?""",
        (bot_id,),
    ).fetchone()
    if bot is None:
        raise HTTPException(404, "机器人不存在。")
    if not bot["enabled"]:
        raise HTTPException(400, f"机器人「{bot['name']}」已停用。")
    model = get_model(conn, bot["model_id"])
    return {**model, "bot_id": bot["id"], "bot_name": bot["name"], "prompt": bot["prompt"]}


def get_summary_model_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("""
        SELECT id FROM models WHERE enabled = 1
        ORDER BY id IS NOT (
            SELECT CAST(value AS INTEGER) FROM config_settings WHERE key = 'summary_model_id'
        ), id
        LIMIT 1
    """).fetchone()
    return row["id"] if row else None


def set_summary_model_id(conn: sqlite3.Connection, model_id: int) -> None:
    conn.execute(
        """INSERT INTO config_settings (key, value) VALUES ('summary_model_id', ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (str(model_id),),
    )


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: Annotated[str, Field(min_length=1, max_length=120)]


class ProviderInput(ConfigInput):
    base_url: Annotated[str, Field(min_length=1, max_length=1024)]
    kind: Literal["deepseek", "openai"] = "openai"
    api_key: Annotated[str | None, Field(max_length=4096)] = None

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("请输入有效的 HTTP 或 HTTPS 接口地址。")
        try:
            url = urlsplit(value)
            port = url.port
        except ValueError:
            raise ValueError("请输入有效的 HTTP 或 HTTPS 接口地址。") from None
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or port == 0
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError("请输入有效的 HTTP 或 HTTPS 接口地址。")
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: str | None) -> str | None:
        if value and not all(33 <= ord(char) <= 126 for char in value):
            raise ValueError("API 密钥中含有无效字符。")
        return value or None


class ModelInput(ConfigInput):
    provider_id: Annotated[int, Field(gt=0)]
    model_code: Annotated[str, Field(min_length=1, max_length=200)]
    enabled: bool = True


class PersonaInput(ConfigInput):
    prompt: Annotated[str, Field(min_length=1, max_length=12000)]


class BotInput(ConfigInput):
    model_id: Annotated[int, Field(gt=0)]
    persona_id: Annotated[int, Field(gt=0)]
    enabled: bool = True


class ProviderTest(BaseModel):
    model_id: Annotated[int | None, Field(gt=0)] = None


Collection = Literal["providers", "models", "personas", "bots"]
INPUTS = {
    "providers": ProviderInput,
    "models": ModelInput,
    "personas": PersonaInput,
    "bots": BotInput,
}
COLLECTION_NAMES = {
    "providers": "API",
    "models": "模型",
    "personas": "人格模板",
    "bots": "机器人",
}
DELETE_ERRORS = {
    "providers": "还有模型使用该 API。请先调整这些模型的配置。",
    "models": "还有机器人使用该模型。请先调整这些机器人的配置。",
    "personas": "还有机器人使用该人格模板。请先调整这些机器人的配置。",
}


def public_config(collection: Collection, row: sqlite3.Row) -> dict:
    if collection == "providers":
        return {
            "id": row["id"],
            "name": row["name"],
            "base_url": row["base_url"],
            "kind": row["kind"],
            "has_key": bool(provider_key(row)),
        }
    data = dict(row)
    if "enabled" in data:
        data["enabled"] = bool(data["enabled"])
    return data


def find_config(conn: sqlite3.Connection, collection: Collection, item_id: int) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {collection} WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"{COLLECTION_NAMES[collection]}不存在。")
    return row


def validate_config(collection: Collection, body: dict) -> dict:
    try:
        return INPUTS[collection].model_validate(body).model_dump()
    except ValidationError as error:
        first_error = error.errors(include_input=False, include_context=False)[0]
        field = ".".join(str(part) for part in first_error["loc"])
        message = first_error["msg"].removeprefix("Value error, ")
        raise HTTPException(422, f"{field} · {message}") from None


@router.get("")
def config_state() -> dict:
    with connect() as conn:
        config = {
            collection: [
                public_config(collection, row)
                for row in conn.execute(f"SELECT * FROM {collection} ORDER BY id")
            ]
            for collection in INPUTS
        }
        return {**config, "summary_model_id": get_summary_model_id(conn)}


@router.post("/{collection}", status_code=201)
def add_config(collection: Collection, body: dict) -> dict:
    data = validate_config(collection, body)
    columns = ", ".join(data)
    placeholders = ", ".join("?" for _ in data)
    with connect() as conn:
        try:
            item_id = conn.execute(
                f"INSERT INTO {collection} ({columns}) VALUES ({placeholders})", tuple(data.values())
            ).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(409, "关联配置不存在，请刷新后重试。") from None
        return public_config(collection, find_config(conn, collection, item_id))


@router.patch("/{collection}/{item_id}")
def edit_config(collection: Collection, item_id: int, body: dict) -> dict:
    with connect() as conn:
        row = find_config(conn, collection, item_id)
        current = {field: row[field] for field in INPUTS[collection].model_fields}
        data = validate_config(collection, {**current, **body})
        if not body:
            return public_config(collection, row)
        assignments = ", ".join(f"{field} = ?" for field in body)
        try:
            conn.execute(
                f"UPDATE {collection} SET {assignments} WHERE id = ?",
                (*(data[field] for field in body), item_id),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "关联配置不存在，请刷新后重试。") from None
        return public_config(collection, find_config(conn, collection, item_id))


@router.delete("/{collection}/{item_id}", status_code=204)
def delete_config(collection: Collection, item_id: int) -> Response:
    with connect() as conn:
        try:
            cursor = conn.execute(f"DELETE FROM {collection} WHERE id = ?", (item_id,))
        except sqlite3.IntegrityError:
            raise HTTPException(409, DELETE_ERRORS.get(collection, "还有记录使用该配置。")) from None
        if cursor.rowcount == 0:
            raise HTTPException(404, f"{COLLECTION_NAMES[collection]}不存在。")
    return Response(status_code=204)


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: int, body: ProviderTest | None = None) -> dict:
    with connect() as conn:
        find_config(conn, "providers", provider_id)
        if body and body.model_id is not None:
            row = find_config(conn, "models", body.model_id)
            if row["provider_id"] != provider_id:
                raise HTTPException(400, "请选择属于这个 API 的模型。")
        else:
            row = conn.execute(
                "SELECT id FROM models WHERE provider_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
                (provider_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(400, "请先添加并启用一个属于这个 API 的模型。")
        model = get_model(conn, row["id"])
    try:
        await complete(model, [{"role": "user", "content": "请只回复 OK。"}], max_tokens=32)
    except ModelError as error:
        raise HTTPException(502, str(error)) from None
    return {"message": f"连接成功 · {model['name']}"}
