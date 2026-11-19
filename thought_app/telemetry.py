from datetime import datetime, timedelta, timezone
import logging
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from .database import connect


router = APIRouter(prefix="/api/monitor")
_LOG = logging.getLogger(__name__)
_PERIOD_HOURS = {"24h": 24, "7d": 7 * 24, "30d": 30 * 24}
_DETAILS = {
    "success": None,
    "timeout": "API 响应超时",
    "quota": "API 额度或余额不足",
    "rate_limit": "API 请求达到速率限制",
    "auth": "API 密钥无效或已失效",
    "permission": "API 密钥无权访问该服务或模型",
    "network": "无法连接 API",
    "config": "API 或模型配置不可用",
    "upstream": "API 服务返回错误",
    "invalid_response": "API 返回了无效响应",
    "cancelled": None,
}


def init_telemetry(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS api_usage_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            model_id INTEGER NOT NULL,
            model_code TEXT NOT NULL,
            outcome TEXT NOT NULL,
            acknowledged_at TEXT,
            UNIQUE(provider_id, model_id, model_code, outcome)
        );
        CREATE TABLE IF NOT EXISTS api_usage_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            provider_id INTEGER NOT NULL,
            provider_name TEXT NOT NULL,
            model_id INTEGER NOT NULL,
            model_name TEXT NOT NULL,
            model_code TEXT NOT NULL,
            task TEXT NOT NULL,
            bot_name TEXT,
            outcome TEXT NOT NULL,
            latency_ms INTEGER NOT NULL,
            http_status INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            detail TEXT,
            alert_id INTEGER REFERENCES api_usage_alerts(id)
        );
        CREATE INDEX IF NOT EXISTS api_usage_calls_created_at ON api_usage_calls(created_at);
        CREATE INDEX IF NOT EXISTS api_usage_calls_provider ON api_usage_calls(provider_id, created_at);
        CREATE INDEX IF NOT EXISTS api_usage_calls_model ON api_usage_calls(model_id, created_at);
    """)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(api_usage_calls)")}
    for column in ("web_enabled", "search_count", "read_count"):
        if column not in columns:
            db.execute(f"ALTER TABLE api_usage_calls ADD COLUMN {column} INTEGER")


def _timestamp(instant=None):
    return (instant or datetime.now(timezone.utc)).isoformat(timespec="microseconds")


def _metric(value):
    return value if type(value) is int and 0 <= value < 2 ** 63 else None


def _tokens(value):
    return value if type(value) is int and 0 <= value < 2 ** 31 else None


def _label(value, fallback, credential):
    if not isinstance(value, str):
        return fallback
    if isinstance(credential, str) and credential:
        value = value.replace(credential, "已隐藏")
    return " ".join(value.split())[:200] or fallback


def record_call(settings, *, outcome: str, latency_ms: int, http_status: int | None = None,
                input_tokens: int | None = None, output_tokens: int | None = None,
                total_tokens: int | None = None, detail: str | None = None,
                web_enabled=False, search_count=None, read_count=None) -> None:
    """Store an operational event without retaining request or provider error bodies."""
    if outcome not in _DETAILS:
        outcome = "upstream"
    # Only the fixed descriptions above are stored. The caller's detail can come from a remote body.
    credential = settings.get("api_key")
    provider_id = _metric(settings.get("provider_id")) or 0
    model_id = _metric(settings.get("id")) or 0
    model_code = _label(settings.get("model_code"), "", credential)
    provider_name = _label(settings.get("provider_name"), "未知 API", credential)
    model_name = _label(settings.get("name"), model_code or "未知模型", credential)
    task = _label(settings.get("task"), "chat", credential)
    bot_name = _label(settings.get("bot_name"), None, credential)
    input_tokens = _tokens(input_tokens)
    output_tokens = _tokens(output_tokens)
    total_tokens = _tokens(total_tokens)
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = _metric(input_tokens + output_tokens)
    http_status = _metric(http_status)
    if http_status is not None and not 100 <= http_status <= 599:
        http_status = None

    try:
        with connect() as db:
            # Taking the write lock before stamping orders acknowledgement and occurrence events.
            db.execute("BEGIN IMMEDIATE")
            created_at = _timestamp()
            alert_id = None
            if outcome not in {"success", "cancelled"}:
                alert_id = db.execute("""
                    INSERT INTO api_usage_alerts (provider_id, model_id, model_code, outcome, acknowledged_at)
                    VALUES (?, ?, ?, ?, NULL)
                    ON CONFLICT (provider_id, model_id, model_code, outcome) DO UPDATE SET acknowledged_at = NULL
                    RETURNING id
                """, (provider_id, model_id, model_code, outcome)).fetchone()["id"]
            db.execute("""
                INSERT INTO api_usage_calls (
                    created_at, provider_id, provider_name, model_id, model_name, model_code, task,
                    bot_name, outcome, latency_ms, http_status, input_tokens, output_tokens,
                    total_tokens, detail, alert_id, web_enabled, search_count, read_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                created_at, provider_id, provider_name, model_id, model_name, model_code, task,
                bot_name, outcome, _metric(latency_ms) or 0, http_status, input_tokens,
                output_tokens, total_tokens, _DETAILS[outcome], alert_id, int(web_enabled), _metric(search_count), _metric(read_count),
            ))
    except (sqlite3.Error, OSError) as error:
        _LOG.warning("API telemetry write failed (%s)", type(error).__name__)


_METRICS = """
    COUNT(*) AS calls,
    COUNT(CASE WHEN outcome = 'success' THEN 1 END) AS successes,
    COUNT(CASE WHEN outcome NOT IN ('success', 'cancelled') THEN 1 END) AS failures,
    COUNT(CASE WHEN outcome = 'cancelled' THEN 1 END) AS cancelled,
    SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
    SUM(total_tokens) AS total_tokens,
    COUNT(CASE WHEN input_tokens IS NOT NULL OR output_tokens IS NOT NULL OR total_tokens IS NOT NULL THEN 1 END)
        AS usage_reports,
    SUM(search_count) AS search_count, SUM(read_count) AS read_count,
    ROUND(AVG(latency_ms)) AS avg_latency_ms
"""


@router.get("")
def monitor_state(
    period: Literal["24h", "7d", "30d"] = "24h",
    provider_id: int | None = Query(default=None, gt=0),
    model_id: int | None = Query(default=None, gt=0),
    web_only: bool = False,
):
    cutoff = _timestamp(datetime.now(timezone.utc) - timedelta(hours=_PERIOD_HOURS[period]))
    clauses = ["created_at >= ?"]
    params = [cutoff]
    if web_only:
        clauses.append("web_enabled = 1")
    for column, value in (("provider_id", provider_id), ("model_id", model_id)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = " AND ".join(clauses)
    with connect() as db:
        db.execute("BEGIN")
        totals = dict(db.execute(f"SELECT {_METRICS} FROM api_usage_calls WHERE {where}", params).fetchone())
        breakdown = db.execute(f"""
            WITH grouped AS (
                SELECT MAX(id) AS latest, {_METRICS}
                FROM api_usage_calls WHERE {where}
                GROUP BY provider_id, model_id, model_code
            )
            SELECT c.provider_id, c.provider_name, c.model_id, c.model_name, c.model_code,
                   g.calls, g.successes, g.failures, g.cancelled, g.input_tokens, g.output_tokens,
                   g.total_tokens, g.usage_reports, g.avg_latency_ms
            FROM grouped g JOIN api_usage_calls c ON c.id = g.latest
            ORDER BY g.calls DESC, g.latest DESC
        """, params).fetchall()
        alerts = db.execute(f"""
            WITH occurrences AS (
                SELECT alert_id, COUNT(*) AS count, MIN(created_at) AS first_seen,
                       MAX(created_at) AS last_seen, MAX(id) AS latest
                FROM api_usage_calls WHERE {where} AND alert_id IS NOT NULL
                GROUP BY alert_id
            )
            SELECT a.id, c.provider_id, c.provider_name, c.model_id, c.model_name, c.model_code,
                   c.outcome, c.detail, c.http_status, o.count, o.first_seen, o.last_seen, a.acknowledged_at
            FROM occurrences o JOIN api_usage_alerts a ON a.id = o.alert_id
            JOIN api_usage_calls c ON c.id = o.latest
            ORDER BY a.acknowledged_at IS NOT NULL, o.last_seen DESC, a.id DESC LIMIT 80
        """, params).fetchall()
        recent = db.execute(f"""
            SELECT id, created_at, provider_id, provider_name, model_id, model_name, model_code,
                   task, bot_name, outcome, latency_ms, http_status, input_tokens, output_tokens,
                   total_tokens, detail, web_enabled, search_count, read_count
            FROM api_usage_calls WHERE {where} ORDER BY created_at DESC, id DESC LIMIT 100
        """, params).fetchall()
    return {
        "totals": totals,
        "breakdown": [dict(row) for row in breakdown],
        "alerts": [dict(row) for row in alerts],
        "recent": [dict(row) for row in recent],
    }


@router.post("/alerts/{alert_id}/ack")
def acknowledge_alert(alert_id: int):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""
            UPDATE api_usage_alerts SET acknowledged_at = COALESCE(acknowledged_at, ?)
            WHERE id = ? RETURNING id, acknowledged_at
        """, (_timestamp(), alert_id)).fetchone()
        if row is None:
            raise HTTPException(404, "找不到这条 API 报警。")
    return dict(row)
