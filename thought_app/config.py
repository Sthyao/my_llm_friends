from hashlib import sha256
import os
import sqlite3
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .database import connect
from .llm import ModelError, complete
from .prompts import load_prompt


DEEPSEEK_URL = "https://api.deepseek.com"
POE_URL = "https://api.poe.com/v1"
POE_MODEL_CODE = "deepseek-v3.2"
router = APIRouter(prefix="/api/config")

MIRROR_NAME = "影子自我"
PERSONAS = {
    "造梦师": load_prompt("personas/dreamer"),
    "刺头": load_prompt("personas/critic"),
    "拾荒者": load_prompt("personas/keeper"),
    MIRROR_NAME: load_prompt("personas/mirror"),
}

EXPERT_PERSONAS = {
    "自我分析心理学专家": load_prompt("personas/psychology"),
    "社会科学分析专家": load_prompt("personas/social-science"),
}

_PRIOR_STARTER_NAMES = {"浪漫": "造梦师", "客观": "刺头", "忧郁": "拾荒者"}
_PRIOR_STARTER_HASHES = {
    "忧郁": {
        "5bea8566c16ca920472ed43eaabe5aecf700904eaa01e99c6142b8cbeb778ec2",
        "0a3b5387fe7d84b6c55e05e4a4d9679dd2ea0e2b237ae5860f06a1329096c262",
    },
    "客观": {
        "102ee4b08a3530ac6cb4daf2390f8a9306886e5756ab2da7392cd9ac24c4408e",
        "73c14880fe324883e50b8e1ca88537249735c2c7c6058a4432268ab9fe8b2d19",
    },
    "积极": {
        "2e2fe9e0c408d9951bba993fc5b490d7d0eb25a5af642ccc7184c0be21093a85",
        "9cb79c617956bb627ed6ff6258572a9e24ce817376c9f310c19a7232acacd40e",
    },
    "浪漫": {
        "6f816bcd895b60521f6cdbacca50f9e5f64a018dcaf8ab533bdda20d61ee32e4",
        "3e1bb9d7683fbe563ad0534a1e8ad9ea78df02f806770f2358979f2b09683d53",
    },
}


def add_starter_bot(conn, name, model_id):
    persona_id = conn.execute(
        "INSERT INTO personas (name, prompt) VALUES (?, ?)", (name, PERSONAS[name])
    ).lastrowid
    conn.execute(
        "INSERT INTO bots (name, model_id, persona_id, role_mode) VALUES (?, ?, ?, ?)",
        (name, model_id, persona_id, "mirror" if name == MIRROR_NAME else "friend"),
    )


def refresh_builtin_personas(conn):
    marker = conn.execute("""
        INSERT INTO config_settings (key, value) VALUES ('group_personas_v3', '1') ON CONFLICT DO NOTHING
    """)
    if not marker.rowcount:
        return
    bots = conn.execute("""
        SELECT b.*, p.name AS persona_name, p.prompt, m.model_code, prov.use_deepseek_env
        FROM bots b JOIN personas p ON p.id = b.persona_id
        JOIN models m ON m.id = b.model_id JOIN providers prov ON prov.id = m.provider_id
    """).fetchall()
    existing_names = {bot["name"] for bot in bots}
    default_model = conn.execute("""
        SELECT m.id FROM models m JOIN providers p ON p.id = m.provider_id
        WHERE p.use_deepseek_env = 1 AND m.model_code = 'deepseek-flash' AND m.enabled = 1
        ORDER BY m.id LIMIT 1
    """).fetchone()
    starter_model_id = default_model["id"] if default_model else get_summary_model_id(conn)
    for bot in bots:
        old = bot["name"]
        hashes = _PRIOR_STARTER_HASHES.get(old, ())
        if (
            bot["role_mode"] != "friend"
            or bot["persona_name"] != old
            or sha256(bot["prompt"].encode()).hexdigest() not in hashes
        ):
            continue
        replacement = _PRIOR_STARTER_NAMES.get(old)
        if replacement in existing_names:
            continue
        if replacement is None:
            if bot["use_deepseek_env"] and bot["model_code"] == "deepseek-flash":
                retire_starter_bot(conn, bot)
            continue

        assign_starter_persona(conn, bot, replacement)
        existing_names.add(replacement)

    if starter_model_id is not None:
        for name in PERSONAS:
            if name not in existing_names:
                add_starter_bot(conn, name, starter_model_id)


def assign_starter_persona(conn, bot, name):
    persona_id = bot["persona_id"]
    shared = conn.execute(
        "SELECT 1 FROM bots WHERE persona_id = ? AND id != ?", (persona_id, bot["id"]),
    ).fetchone()
    if shared:
        persona_id = conn.execute(
            "INSERT INTO personas (name, prompt) VALUES (?, ?)", (name, PERSONAS[name]),
        ).lastrowid
    else:
        conn.execute("UPDATE personas SET name = ?, prompt = ? WHERE id = ?", (name, PERSONAS[name], persona_id))
    conn.execute("UPDATE bots SET name = ?, persona_id = ? WHERE id = ?", (name, persona_id, bot["id"]))


def refresh_friend_voices(conn):
    previous = {
        "造梦师": "a26311bcd8dfb796cdf2a8cf3c62b5b5d3f18803d1d7fda1f33cb0557fb747c8",
        "刺头": "39e3a77f4cdd9eb7f30f083581ee95ee36615c3705b275ff236b91174ec50589",
        "拾荒者": "a710fcc41cd4d5cf232412a7807a3e3395be70f3ca72efc933b4692863b0b910",
    }
    marker = conn.execute(
        "INSERT INTO config_settings (key, value) VALUES ('friend_voices_v4', '1') ON CONFLICT DO NOTHING"
    )
    if marker.rowcount:
        for bot in conn.execute("""
            SELECT b.id, b.name, b.persona_id, p.prompt FROM bots b JOIN personas p ON p.id = b.persona_id
            WHERE b.role_mode = 'friend' AND b.name = p.name
        """).fetchall():
            if previous.get(bot["name"]) == sha256(bot["prompt"].encode()).hexdigest():
                assign_starter_persona(conn, bot, bot["name"])


def retire_starter_bot(conn, bot):
    tables = conn.execute("""
        SELECT name FROM sqlite_master WHERE type = 'table'
        AND name IN ('bot_evolution_notes', 'bot_evolution_settings')
    """).fetchall()
    if any(
        conn.execute(f"SELECT 1 FROM {table['name']} WHERE bot_id = ?", (bot["id"],)).fetchone()
        for table in tables
    ):
        conn.execute("UPDATE bots SET enabled = 0 WHERE id = ?", (bot["id"],))
        return
    conn.execute("DELETE FROM bots WHERE id = ?", (bot["id"],))
    conn.execute("""
        DELETE FROM personas WHERE id = ?
        AND NOT EXISTS (SELECT 1 FROM bots WHERE bots.persona_id = personas.id)
    """, (bot["persona_id"],))


def seed_experts(conn):
    if conn.execute("SELECT 1 FROM config_settings WHERE key = 'expert_personas_v1'").fetchone():
        return
    model = conn.execute("""
        SELECT m.id FROM models m JOIN providers p ON p.id = m.provider_id
        WHERE p.kind = 'deepseek' AND p.base_url IN (?, ?) AND m.model_code = 'deepseek-flash'
        ORDER BY m.enabled DESC, m.id LIMIT 1
    """, (DEEPSEEK_URL, DEEPSEEK_URL + "/v1")).fetchone()
    if model is None:
        provider = conn.execute(
            "SELECT id FROM providers WHERE kind = 'deepseek' AND base_url = ? ORDER BY id LIMIT 1", (DEEPSEEK_URL,)
        ).fetchone()
        provider_id = provider["id"] if provider else conn.execute("""
            INSERT INTO providers (name, base_url, kind, use_deepseek_env) VALUES ('DeepSeek', ?, 'deepseek', 1)
        """, (DEEPSEEK_URL,)).lastrowid
        model_id = conn.execute("""
            INSERT INTO models (name, provider_id, model_code) VALUES ('DeepSeek Flash', ?, 'deepseek-flash')
        """, (provider_id,)).lastrowid
    else:
        model_id = model["id"]
    for name, prompt in EXPERT_PERSONAS.items():
        if conn.execute("SELECT 1 FROM bots WHERE name = ?", (name,)).fetchone():
            continue
        persona_id = conn.execute(
            "INSERT INTO personas (name, prompt, web_search) VALUES (?, ?, 1)", (name, prompt)
        ).lastrowid
        conn.execute("INSERT INTO bots (name, persona_id, model_id) VALUES (?, ?, ?)", (name, persona_id, model_id))
    conn.execute("INSERT INTO config_settings (key, value) VALUES ('expert_personas_v1', '1')")


def init_config(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            kind TEXT NOT NULL,
            api_key TEXT,
            use_deepseek_env INTEGER NOT NULL DEFAULT 0,
            use_poe_env INTEGER NOT NULL DEFAULT 0
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
            enabled INTEGER NOT NULL DEFAULT 1,
            role_mode TEXT NOT NULL DEFAULT 'friend' CHECK(role_mode IN ('friend', 'mirror'))
        );
        CREATE TABLE IF NOT EXISTS config_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    if "use_poe_env" not in {row["name"] for row in conn.execute("PRAGMA table_info(providers)")}:
        conn.execute("ALTER TABLE providers ADD COLUMN use_poe_env INTEGER NOT NULL DEFAULT 0")
    if "role_mode" not in {row["name"] for row in conn.execute("PRAGMA table_info(bots)")}:
        conn.execute("""
            ALTER TABLE bots ADD COLUMN role_mode TEXT NOT NULL DEFAULT 'friend'
            CHECK(role_mode IN ('friend', 'mirror'))
        """)
    if "web_search" not in {row["name"] for row in conn.execute("PRAGMA table_info(personas)")}:
        conn.execute("ALTER TABLE personas ADD COLUMN web_search INTEGER NOT NULL DEFAULT 0 CHECK(web_search IN (0, 1))")
    seeded = conn.execute(
        "INSERT INTO config_settings (key, value) VALUES ('seeded', '1') ON CONFLICT DO NOTHING"
    )
    if seeded.rowcount == 0:
        refresh_builtin_personas(conn)
        refresh_friend_voices(conn)
        seed_poe(conn)
        seed_experts(conn)
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
    for name in PERSONAS:
        add_starter_bot(conn, name, model_id)
    conn.execute("INSERT INTO config_settings (key, value) VALUES ('group_personas_v3', '1'), ('friend_voices_v4', '1')")
    set_summary_model_id(conn, model_id)
    seed_poe(conn)
    seed_experts(conn)


def seed_poe(conn):
    if not os.getenv("POE_API_KEY"):
        return
    marker = conn.execute("""
        INSERT INTO config_settings (key, value) VALUES ('poe_seeded', '1') ON CONFLICT DO NOTHING
    """)
    if not marker.rowcount:
        return
    existing = conn.execute(
        "SELECT id FROM providers WHERE base_url = ? AND kind IN ('poe', 'openai') ORDER BY id LIMIT 1", (POE_URL,),
    ).fetchone()
    if existing:
        provider_id = existing["id"]
        conn.execute("UPDATE providers SET kind = 'poe', use_poe_env = 1 WHERE id = ?", (provider_id,))
    else:
        provider_id = conn.execute(
            "INSERT INTO providers (name, base_url, kind, use_poe_env) VALUES (?, ?, 'poe', 1)", ("Poe", POE_URL),
        ).lastrowid
    conn.execute("""
        INSERT INTO models (name, provider_id, model_code)
        SELECT 'DeepSeek V3.2', ?, ?
        WHERE NOT EXISTS (SELECT 1 FROM models WHERE provider_id = ? AND model_code = ?)
    """, (provider_id, POE_MODEL_CODE, provider_id, POE_MODEL_CODE))


def provider_key(provider: sqlite3.Row) -> str | None:
    if provider["api_key"]:
        return provider["api_key"]
    if (
        provider["use_deepseek_env"]
        and provider["kind"] == "deepseek"
        and provider["base_url"] == DEEPSEEK_URL
    ):
        return os.getenv("DEEPSEEK_API_KEY") or None
    if provider["use_poe_env"] and provider["kind"] == "poe" and provider["base_url"] == POE_URL:
        return os.getenv("POE_API_KEY") or None
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
        "provider_id": provider["id"],
        "provider_name": provider["name"],
        "base_url": provider["base_url"],
        "kind": provider["kind"],
        "api_key": provider_key(provider),
    }


def get_bot(conn: sqlite3.Connection, bot_id: int) -> dict:
    bot = conn.execute(
        """SELECT bots.*, personas.prompt, personas.web_search
           FROM bots JOIN personas ON personas.id = bots.persona_id
           WHERE bots.id = ?""",
        (bot_id,),
    ).fetchone()
    if bot is None:
        raise HTTPException(404, "机器人不存在。")
    if not bot["enabled"]:
        raise HTTPException(400, f"机器人「{bot['name']}」已停用。")
    model = get_model(conn, bot["model_id"])
    return {
        **model, "bot_id": bot["id"], "bot_name": bot["name"], "prompt": bot["prompt"],
        "role_mode": bot["role_mode"], "web_search": bool(bot["web_search"]),
    }


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
    kind: Literal["deepseek", "poe", "openai"] = "openai"
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
    role_mode: Literal["friend", "mirror"] = "friend"


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


class ReplyPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    long_replies: bool


def long_replies_enabled(conn):
    row = conn.execute("SELECT value FROM config_settings WHERE key = 'long_replies'").fetchone()
    return bool(row and row["value"] == "1")


@router.patch("/reply-preferences")
def update_reply_preferences(body: ReplyPreferences):
    with connect() as conn:
        conn.execute("""
            INSERT INTO config_settings (key, value) VALUES ('long_replies', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, ("1" if body.long_replies else "0",))
    return body


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
        return {**config, "summary_model_id": get_summary_model_id(conn), "long_replies": long_replies_enabled(conn)}


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
        await complete({**model, "task": "health"}, [{"role": "user", "content": "Only reply OK."}], max_tokens=12)
    except ModelError as error:
        raise HTTPException(502, str(error)) from None
    return {"message": f"连接成功 · {model['name']}"}
