import asyncio
import logging
import re
from time import perf_counter
from urllib.parse import urlsplit

import httpx

from .telemetry import record_call


_LOG = logging.getLogger(__name__)
_QUOTA = re.compile(
    r"insufficient[ _-]?(?:quota|credits?|balance|points|funds)|"
    r"(?:not enough|out of|ran out of|lack of) (?:compute )?(?:points|credits|funds)|"
    r"(?:points|credits|quota|balance) (?:are |is |was )?(?:exhausted|insufficient|depleted|too low)|"
    r"billing[_ -]hard[_ -]limit|(?:额度|余额|点数|积分)不足",
    re.IGNORECASE,
)


class ModelError(Exception):
    """A normalized model failure suitable for the local operations panel."""

    def __init__(self, message, outcome="invalid_response"):
        super().__init__(message)
        self.outcome = outcome


def response_error(response):
    status = response.status_code
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        error = {}
    if isinstance(error, dict):
        marker = " ".join(str(error.get(field, ""))[:300] for field in ("type", "code", "message"))
    else:
        marker = str(error)[:600]
    if status == 402 or (status in {400, 403, 429} and _QUOTA.search(marker)):
        return ModelError("模型服务余额或可用额度不足。", "quota")
    if status in {408, 504}:
        return ModelError("模型响应超时。", "timeout")
    failure = {
        401: ("API 密钥无效或已失效。", "auth"),
        403: ("API 密钥无权访问请求的模型。", "permission"),
        404: ("接口或模型不存在，请检查地址和模型 ID。", "config"),
        429: ("模型服务限制了调用频率。", "rate_limit"),
    }.get(status, (f"模型服务返回 HTTP {status}。", "upstream"))
    return ModelError(*failure)


def usage_tokens(data):
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {}

    def read(*names):
        for name in names:
            count = usage.get(name)
            if type(count) is int and 0 <= count <= 1_000_000_000:
                return count
        return None

    input_tokens = read("prompt_tokens", "input_tokens")
    if input_tokens is not None and "cache_read_input_tokens" in usage:
        input_tokens += read("cache_read_input_tokens") or 0
    output_tokens = read("completion_tokens", "output_tokens")
    total_tokens = read("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens}


def supports_web_search(bot):
    return (bot.get("web_search") and bot["role_mode"] == "friend" and bot["kind"] == "deepseek"
            and bot["model_code"] == "deepseek-flash"
            and bot["base_url"].rstrip("/") in {"https://api.deepseek.com", "https://api.deepseek.com/v1"})


def expert_answer(data, sources):
    if data.get("error"):
        raise ModelError("DeepSeek 搜索服务未能完成本次回复。", "upstream")
    if data.get("stop_reason") != "end_turn":
        raise ModelError("DeepSeek 专家回复未完整生成。")
    parts = []
    for block in data["content"]:
        if block["type"] != "text":
            continue
        parts.append(block["text"])
        for citation in block.get("citations") or []:
            url = citation.get("url")
            if isinstance(url, str) and urlsplit(url).scheme in {"https", "http"} and urlsplit(url).hostname:
                sources.setdefault(url, citation.get("title") or url)
    return "".join(parts)


async def complete(settings, messages, *, max_tokens=650, json_output=False, web_sources=None, read_links=False):
    started = perf_counter()
    status = None
    usage = {}
    outcome, detail = "upstream", None
    try:
        if not settings["api_key"]:
            raise ModelError("这个 API 还没有配置密钥。", "config")

        payload = {
            "model": settings["model_code"],
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if web_sources is not None:
            if not supports_web_search(settings) or settings.get("task") not in {"reply", "aside"}:
                raise ModelError("此人格或模型无法使用 DeepSeek 联网工具。", "config")
            url = "https://api.deepseek.com/anthropic/v1/messages"
            headers = {"x-api-key": settings["api_key"], "anthropic-version": "2023-06-01"}
            payload.update({
                "system": "\n\n".join(m["content"] for m in messages if m["role"] == "system"),
                "messages": [m for m in messages if m["role"] != "system"],
                "max_tokens": max_tokens + 4096,
                "thinking": {"type": "disabled" if read_links else "enabled"},
                "tools": [{"type": name, "name": name} for name in ("deepseek_web_search", "deepseek_read_url")],
                "stream": False,
            })
        else:
            url = settings["base_url"].rstrip("/") + "/chat/completions"
            headers = {"Authorization": "Bearer " + settings["api_key"]}
            if settings["kind"] == "deepseek":
                payload["thinking"] = {"type": "disabled"}
            if json_output:
                payload["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180 if web_sources is not None else 55, connect=8)) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ModelError("模型响应超时，可以稍后重试。", "timeout") from exc
        except (httpx.RequestError, httpx.InvalidURL) as exc:
            raise ModelError("无法连接模型服务，请检查接口地址和网络。", "network") from exc

        status = response.status_code
        if not response.is_success:
            raise response_error(response)
        try:
            data = response.json()
            usage = usage_tokens(data)
            if web_sources is not None:
                body = expert_answer(data, web_sources)
            else:
                choice = data["choices"][0]
                body = choice["message"]["content"]
                if json_output and choice.get("finish_reason") == "length":
                    raise ModelError("模型输出超出长度限制，请重试或选择其他模型。")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ModelError("模型服务未返回兼容的聊天响应。") from exc
        if not isinstance(body, str) or not body.strip():
            raise ModelError("模型返回了空内容。")
        outcome = "success"
        return body.strip()
    except ModelError as exc:
        outcome, detail = exc.outcome, str(exc)
        raise
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        try:
            recorded = settings
            if settings.get("task") in {"route", "aside_route"}:
                recorded = {**settings, "bot_name": None}
            record_call(
                recorded, outcome=outcome, latency_ms=round((perf_counter() - started) * 1000),
                http_status=status, detail=detail, **usage,
            )
        except Exception as exc:
            _LOG.warning("API usage recording failed (%s)", type(exc).__name__)
