"""DeepSeek JSON completions for the standalone persona experiment."""

import asyncio
import json
import os
from pathlib import Path
from time import perf_counter

import httpx
from dotenv import dotenv_values


class DeepSeek:
    model = "deepseek-flash"
    endpoint = "https://api.deepseek.com/chat/completions"

    def __init__(self):
        self._key = os.getenv("DEEPSEEK_API_KEY")
        directory = Path(__file__).resolve().parent
        for path in (directory / ".env", directory.parent / ".env"):
            if self._key:
                break
            self._key = dotenv_values(path).get("DEEPSEEK_API_KEY")
        if not self._key:
            raise RuntimeError("请在环境、exp/.env 或项目 .env 中配置 DEEPSEEK_API_KEY。")
        self.calls: list[dict] = []

    async def ask(self, stage: str, instruction: str, data: dict, *, thinking: bool = False) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instruction + "\n仅返回符合本阶段约定的 JSON 对象。"},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            "thinking": {"type": "enabled" if thinking else "disabled"},
            "max_tokens": 8192 if thinking else 4096,
            "response_format": {"type": "json_object"},
        }
        if thinking:
            payload["reasoning_effort"] = "low"
        headers = {"Authorization": f"Bearer {self._key}"}
        timeout = httpx.Timeout(90 if thinking else 55, connect=8)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(2):
                metric = {"stage": stage, "attempt": attempt + 1, "model": None,
                          "prompt_tokens": None, "completion_tokens": None, "thinking": thinking}
                started = perf_counter()
                try:
                    response = await client.post(self.endpoint, headers=headers, json=payload)
                    response.raise_for_status()
                    body = response.json()
                    usage = body.get("usage") or {}
                    metric.update(model=body.get("model"), prompt_tokens=usage.get("prompt_tokens"),
                                  completion_tokens=usage.get("completion_tokens"))
                    details = usage.get("completion_tokens_details") or {}
                    if "reasoning_tokens" in details:
                        metric["reasoning_tokens"] = details["reasoning_tokens"]
                    choice = body["choices"][0]
                    if choice.get("finish_reason") != "stop":
                        raise ValueError("JSON 输出不完整")
                    result = json.loads(choice["message"]["content"])
                    if not isinstance(result, dict):
                        raise ValueError("输出不是 JSON 对象")
                    metric["status"] = "ok"
                    return result
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    metric["status"] = f"http_{status}"
                    if attempt or status not in {408, 429, 500, 502, 503, 504}:
                        raise RuntimeError(f"DeepSeek {stage} 返回 HTTP {status}。") from None
                except httpx.RequestError as exc:
                    metric["status"] = type(exc).__name__
                    if attempt:
                        raise RuntimeError(f"DeepSeek {stage} 连接失败 ({metric['status']})。") from None
                except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                    metric["status"] = "invalid_json_response"
                    if attempt:
                        raise RuntimeError(f"DeepSeek {stage} 未返回完整 JSON 对象。") from None
                finally:
                    metric["elapsed_ms"] = round((perf_counter() - started) * 1000)
                    self.calls.append(metric)
                await asyncio.sleep(0.5)
        raise AssertionError("DeepSeek attempt loop exited unexpectedly")
