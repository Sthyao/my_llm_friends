import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient


class ApiMonitorTests(unittest.TestCase):
    POE_KEY = "synthetic-poe-key"
    DEEPSEEK_KEY = "synthetic-deepseek-key"
    PRIVATE_TEXT = "synthetic private message"
    RAW_UPSTREAM_ERROR = "do not copy this upstream error to the database"

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.db = folder / "api-monitor.sqlite3"
        stack.enter_context(patch.dict(os.environ, {
            "THOUGHT_DB": str(self.db),
            "DEEPSEEK_API_KEY": self.DEEPSEEK_KEY,
            "POE_API_KEY": self.POE_KEY,
        }))
        from thought_app import llm
        from thought_app.app import app

        real_async_client = httpx.AsyncClient

        def mocked_async_client(*args, **kwargs):
            return real_async_client(*args, transport=httpx.MockTransport(self.respond), **kwargs)

        self.llm = llm
        self.expert_response = None
        self.poe_response = self.normal_reply
        self.poe_model_code = "deepseek-v3.2"
        stack.enter_context(patch("thought_app.llm.httpx.AsyncClient", mocked_async_client))
        stack.enter_context(patch("thought_app.sides.SIDE_PROBABILITY", 0))
        self.client = stack.enter_context(TestClient(app))
        self.config = self.checked(self.client.get("/api/config"))
        self.poe = next(p for p in self.config["providers"] if p["kind"] == "poe")
        self.model = next(model for model in self.config["models"] if model["provider_id"] == self.poe["id"])

    def checked(self, response):
        self.assertTrue(response.is_success, response.text)
        return response.json() if response.content else None

    def normal_reply(self, request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 18, "completion_tokens": 3, "total_tokens": 21},
        })

    async def respond(self, request):
        if request.url.host == "api.poe.com":
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], f"Bearer {self.POE_KEY}")
            self.assertEqual(json.loads(request.content)["model"], self.poe_model_code)
            return self.poe_response(request)
        self.assertEqual(request.url.host, "api.deepseek.com")
        if request.url.path == "/anthropic/v1/messages":
            self.assertEqual(request.headers["x-api-key"], self.DEEPSEEK_KEY)
            return self.expert_response(request)
        self.assertNotIn("tools", json.loads(request.content))
        self.assertEqual(request.headers["Authorization"], f"Bearer {self.DEEPSEEK_KEY}")
        # The Observer can run alongside a direct request to another provider.
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"memory_to_save": []}'}}],
        })

    def read_monitor(self, **params):
        return self.checked(self.client.get("/api/monitor", params=params))

    def health(self):
        return self.client.post(f"/api/config/providers/{self.poe['id']}/test", json={"model_id": self.model["id"]})

    def test_model_probe_calls_the_exact_model_and_reports_observed_metrics(self):
        self.poe_model_code = "synthetic-new-model"
        model = self.checked(self.client.post("/api/config/models", json={
            "name": "待启用模型", "provider_id": self.poe["id"],
            "model_code": self.poe_model_code, "enabled": False,
        }))
        route = f"/api/config/models/{model['id']}/test"

        def verify_probe_request(request):
            self.assertEqual(json.loads(request.content)["max_tokens"], 12)
            return self.normal_reply(request)

        self.poe_response = verify_probe_request
        result = self.checked(self.client.post(route))
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "success")
        self.assertEqual((result["model_id"], result["model_code"]), (model["id"], self.poe_model_code))
        self.assertEqual(result["http_status"], 200)
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertEqual((result["input_tokens"], result["output_tokens"], result["total_tokens"]), (18, 3, 21))
        event = self.read_monitor(model_id=model["id"])["recent"][0]
        self.assertEqual((event["task"], event["latency_ms"]), ("health", result["latency_ms"]))
        config = self.checked(self.client.get("/api/config"))
        self.assertFalse(next(row for row in config["models"] if row["id"] == model["id"])["enabled"])

    def test_model_probe_reports_sanitized_failures_and_missing_metrics(self):
        route = f"/api/config/models/{self.model['id']}/test"

        def timeout(request):
            raise httpx.ReadTimeout(self.RAW_UPSTREAM_ERROR)

        for respond, outcome, status in (
            (lambda request: httpx.Response(401, json={"error": self.RAW_UPSTREAM_ERROR}), "auth", 401),
            (timeout, "timeout", None),
        ):
            with self.subTest(outcome=outcome):
                self.poe_response = respond
                result = self.checked(self.client.post(route))
                self.assertFalse(result["ok"])
                self.assertGreaterEqual(result["latency_ms"], 0)
                self.assertEqual((result["outcome"], result["http_status"]), (outcome, status))
                self.assertIsNone(result["total_tokens"])
                self.assertNotIn(self.RAW_UPSTREAM_ERROR, json.dumps(result))
                self.assertNotIn(self.POE_KEY, json.dumps(result))
        self.assertEqual(self.client.post("/api/config/models/999999/test").status_code, 404)

    def test_experts_can_search_and_keep_provider_citations(self):
        experts = [b for b in self.config["bots"] if b["name"].endswith("专家")]
        self.assertEqual(len(experts), 2)
        for bot, question, thinking in zip(experts, ["请查研究", "请读 https://example.org/study"], ["enabled", "disabled"]):
            def expert_response(request):
                payload = json.loads(request.content)
                self.assertNotIn("max_tokens", payload)
                self.assertEqual(payload["thinking"]["type"], thinking)
                self.assertEqual([t["name"] for t in payload["tools"]], ["deepseek_web_search", "deepseek_read_url"])
                packet = json.loads(payload["messages"][0]["content"])
                return httpx.Response(200, json={"stop_reason": "end_turn", "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "server_tool_use", "name": "deepseek_web_search"},
                    {"type": "text", "text": json.dumps({"address": packet["本次发言"], "sources": [],
                     "reply": "这项研究明确了样本的范围。"}), "citations": [{"title": "Study", "url": "https://example.org/study"}]},
                ]})

            self.expert_response = expert_response
            sid = self.checked(self.client.post("/api/sessions", json={}))["id"]
            result = self.checked(self.client.post(f"/api/sessions/{sid}/chat", json={"content": f"@{bot['name']} {question}"}))
            self.assertEqual(result["errors"], [])
            _, answer = result["messages"]
            self.assertEqual(answer["bot_id"], bot["id"])
            self.assertIn("https://example.org/study", answer["content"])
            self.assertNotIn("hidden", answer["content"])
            event = self.read_monitor(web_only=True)["recent"][0]
            self.assertEqual((event["task"], event["search_count"], event["read_count"]), ("reply", 1, 0))

    def test_manual_web_search_reports_tool_use_and_unknown_failures(self):
        bot = next(b for b in self.config["bots"] if b["can_web_search"])
        path = f"/api/config/bots/{bot['id']}/web-search"
        question = {"query": self.PRIVATE_TEXT}
        for counters, expected in [({"tool_search": 3, "tool_fetch": 2}, (3, 2)), ({}, (0, 0))]:
            def expert_response(request):
                self.assertEqual(json.loads(request.content)["max_tokens"], 1200 + 4096)
                return httpx.Response(200, json={
                    "stop_reason": "end_turn", "usage": {"server_tool_use": counters},
                    "content": [{"type": "text", "text": "测试答复"}],
                })

            self.expert_response = expert_response
            result = self.checked(self.client.post(path, json=question))
            self.assertEqual((result["search_count"], result["read_count"]), expected)

        self.expert_response = lambda request: httpx.Response(503)
        self.assertEqual(self.client.post(path, json=question).status_code, 502)
        self.checked(self.health())
        monitor = self.read_monitor(web_only=True)
        self.assertEqual(monitor["totals"]["calls"], 3)
        self.assertEqual(monitor["totals"]["search_count"], 3)
        self.assertEqual(monitor["totals"]["read_count"], 2)
        self.assertIsNone(monitor["recent"][0]["search_count"])
        self.assertTrue(all(row["task"] == "web_search_test" for row in monitor["recent"]))
        self.assertNotIn(self.PRIVATE_TEXT, json.dumps(monitor))
        self.assertEqual(self.checked(self.client.get("/api/sessions")), [])
        self.checked(self.client.patch(f"/api/config/bots/{bot['id']}", json={"enabled": False}))
        self.assertEqual(self.client.post(path, json=question).status_code, 400)

    def test_switching_a_bot_model_routes_its_reply_and_records_the_actual_provider(self):
        self.assertEqual(self.poe["base_url"], "https://api.poe.com/v1")
        self.assertNotIn(self.POE_KEY, json.dumps(self.config))
        bot = self.config["bots"][0]
        self.assertNotEqual(bot["model_id"], self.model["id"])
        updated = self.checked(self.client.patch(
            f"/api/config/bots/{bot['id']}", json={"model_id": self.model["id"]},
        ))
        self.assertEqual(updated["model_id"], self.model["id"])

        def chat_reply(request):
            payload = json.loads(request.content)
            self.assertNotIn("max_tokens", payload)
            self.assertNotIn("max_completion_tokens", payload)
            response = self.normal_reply(request).json()
            response["choices"][0]["message"]["content"] = json.dumps({
                "address": json.loads(payload["messages"][-1]["content"])["本次发言"],
                "sources": [], "reply": "收到。", "inner_thought": None, "persona_update": None,
            })
            return httpx.Response(200, json=response)

        self.poe_response = chat_reply
        session_id = self.checked(self.client.post("/api/sessions", json={"title": "model switch"}))["id"]
        turn = self.checked(self.client.post(f"/api/sessions/{session_id}/chat", json={
            "content": f"@{bot['name']} {self.PRIVATE_TEXT}",
        }))
        reply = next(message for message in turn["messages"] if message["role"] == "assistant")
        self.assertEqual(reply["provider_name"], self.poe["name"])
        self.assertEqual(reply["model_name"], self.model["name"])
        self.assertEqual(reply["model_code"], self.model["model_code"])
        saved = self.checked(self.client.get(f"/api/sessions/{session_id}"))["messages"][-1]
        self.assertEqual(saved["provider_name"], self.poe["name"])
        self.assertEqual(saved["model_code"], self.model["model_code"])

        monitor = self.read_monitor(period="7d", provider_id=self.poe["id"], model_id=self.model["id"])
        self.assertEqual(monitor["totals"]["calls"], 1)
        self.assertEqual(monitor["totals"]["successes"], 1)
        self.assertEqual(monitor["totals"]["usage_reports"], 1)
        self.assertEqual(monitor["totals"]["input_tokens"], 18)
        self.assertEqual(monitor["totals"]["output_tokens"], 3)
        self.assertEqual(monitor["totals"]["total_tokens"], 21)
        self.assertEqual(monitor["breakdown"][0]["model_id"], self.model["id"])
        self.assertEqual(monitor["recent"][0]["task"], "reply")
        self.assertGreaterEqual(monitor["recent"][0]["latency_ms"], 0)
        serialized = json.dumps(monitor)
        self.assertNotIn(self.PRIVATE_TEXT, serialized)
        self.assertNotIn(self.POE_KEY, serialized)

    def test_quota_and_timeout_alerts_aggregate_reopen_and_do_not_expose_raw_error(self):
        def exhausted(request):
            return httpx.Response(402, json={"error": {
                "message": self.RAW_UPSTREAM_ERROR, "code": "insufficient_quota",
            }})

        self.poe_response = exhausted
        for _ in range(2):
            response = self.health()
            self.assertFalse(response.is_success)
            self.assertNotIn(self.RAW_UPSTREAM_ERROR, response.text)
        monitor = self.read_monitor(provider_id=self.poe["id"])
        quota = next(alert for alert in monitor["alerts"] if alert["outcome"] == "quota")
        self.assertEqual(quota["count"], 2)
        self.assertEqual(quota["http_status"], 402)
        self.assertEqual(monitor["totals"]["failures"], 2)
        self.assertTrue(all(event["task"] == "health" for event in monitor["recent"]))

        self.checked(self.client.post(f"/api/monitor/alerts/{quota['id']}/ack"))
        acknowledged = self.read_monitor()["alerts"][0]
        self.assertIsNotNone(acknowledged["acknowledged_at"])

        self.poe_response = lambda request: httpx.Response(429, json={"error": {
            "message": self.RAW_UPSTREAM_ERROR, "code": "insufficient_quota",
        }})
        self.assertFalse(self.health().is_success)

        self.poe_response = lambda request: httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})
        self.assertFalse(self.health().is_success)

        def timeout(request):
            raise httpx.ReadTimeout(self.RAW_UPSTREAM_ERROR, request=request)

        self.poe_response = timeout
        self.assertFalse(self.health().is_success)
        monitor = self.read_monitor(period="30d")
        by_outcome = {alert["outcome"]: alert for alert in monitor["alerts"]}
        self.assertEqual(by_outcome["quota"]["id"], quota["id"])
        self.assertEqual(by_outcome["quota"]["count"], 3)
        self.assertEqual(by_outcome["quota"]["http_status"], 429)
        self.assertIsNone(by_outcome["quota"]["acknowledged_at"])
        self.assertEqual(by_outcome["timeout"]["count"], 1)
        self.assertEqual(by_outcome["rate_limit"]["count"], 1)
        self.assertEqual(monitor["totals"]["failures"], 5)
        self.assertNotIn(self.RAW_UPSTREAM_ERROR, json.dumps(monitor))
        with sqlite3.connect(self.db) as db:
            self.assertFalse(any(self.RAW_UPSTREAM_ERROR in statement for statement in db.iterdump()))

    def test_unreported_usage_and_cancellation_are_not_fabricated_as_zero_or_alerts(self):
        self.poe_response = lambda request: httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}}],
        })
        self.checked(self.health())

        def cancelled(request):
            raise asyncio.CancelledError

        self.poe_response = cancelled
        from thought_app.config import get_model
        from thought_app.database import connect

        with connect() as db:
            settings = get_model(db, self.model["id"])
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.llm.complete(settings, [{"role": "user", "content": "stop"}]))

        monitor = self.read_monitor(provider_id=self.poe["id"])
        self.assertEqual(monitor["totals"]["calls"], 2)
        self.assertEqual(monitor["totals"]["cancelled"], 1)
        self.assertEqual(monitor["totals"]["failures"], 0)
        self.assertEqual(monitor["totals"]["usage_reports"], 0)
        self.assertIsNone(monitor["totals"]["total_tokens"])
        self.assertEqual(monitor["alerts"], [])
        self.assertCountEqual([row["outcome"] for row in monitor["recent"]], ["success", "cancelled"])

    def test_poe_truncation_of_required_complete_output_counts_as_failure(self):
        from thought_app.config import get_model
        from thought_app.database import connect

        def truncated(request):
            payload = json.loads(request.content)
            for field in ("response_format", "max_tokens", "max_completion_tokens"):
                self.assertNotIn(field, payload)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": '{"action":"speak"'}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 646},
            })

        self.poe_response = truncated
        with connect() as db:
            settings = {**get_model(db, self.model["id"]), "task": "agent_decide"}
        with self.assertRaises(self.llm.ModelError) as failure:
            asyncio.run(self.llm.complete(
                settings, [{"role": "user", "content": "输出 JSON。"}],
                max_tokens=self.llm.agent_output_limit(settings, 650), require_complete=True, json_output=False,
            ))
        self.assertEqual(failure.exception.outcome, "invalid_response")
        recent = self.read_monitor(model_id=self.model["id"])["recent"]
        self.assertEqual(len(recent), 1)
        call = recent[0]
        self.assertEqual((call["task"], call["outcome"]), ("agent_decide", "invalid_response"))
        self.assertEqual((call["http_status"], call["output_tokens"]), (200, 646))

    def test_seeded_poe_environment_key_does_not_follow_an_edited_url(self):
        updated = self.checked(self.client.patch(
            f"/api/config/providers/{self.poe['id']}", json={"base_url": "https://other-endpoint.test/v1"},
        ))
        self.assertFalse(updated["has_key"])
        # The transport would reject a request to other-endpoint.test.
        self.assertEqual(self.health().status_code, 502)
        monitor = self.read_monitor(provider_id=self.poe["id"])
        self.assertEqual(monitor["alerts"][0]["outcome"], "config")


if __name__ == "__main__":
    unittest.main()
