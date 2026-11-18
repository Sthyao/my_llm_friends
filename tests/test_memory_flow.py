import asyncio
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class MemoryFlowTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        temp_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict(os.environ, {
            "THOUGHT_DB": str(temp_dir / "journal.sqlite3"),
            "DEEPSEEK_API_KEY": "synthetic-test-key",
        }))

        from thought_app.app import app
        from thought_app.llm import ModelError

        self.model_error = ModelError
        self.summary_payload = None
        self.fail_summary = False
        self.reply = self.default_reply
        self.chat_prompts = []
        stack.enter_context(patch("thought_app.llm.complete", self.fake_complete))
        self.client = stack.enter_context(TestClient(app))
        config = self.checked(self.client.get("/api/config"))
        self.model_id = config["models"][0]["id"]
        self.bots = config["bots"][:2]

    async def default_reply(self, settings, prompt):
        return "我会继续追问这个想法。"

    async def fake_complete(self, settings, messages, *, max_tokens=650, json_output=False):
        if json_output:
            if self.fail_summary:
                raise self.model_error("测试中的总结模型故障")
            self.assertIsNotNone(self.summary_payload)
            return json.dumps(self.summary_payload, ensure_ascii=False)

        prompt = "\n".join(message["content"] for message in messages)
        self.chat_prompts.append(prompt)
        return await self.reply(settings, prompt)

    def checked(self, response):
        self.assertTrue(response.is_success, response.text)
        return response.json()

    def create_session(self, title):
        return self.checked(self.client.post("/api/sessions", json={"title": title}))["id"]

    def session(self, session_id):
        return self.checked(self.client.get(f"/api/sessions/{session_id}"))

    def send(self, session_id, content, *, discuss=False):
        bots = self.bots if discuss else self.bots[:1]
        return self.checked(self.client.post(f"/api/sessions/{session_id}/chat", json={
            "content": content,
            "bot_ids": [bot["id"] for bot in bots],
            "discuss": discuss,
        }))

    def generate_summary(self, session_id, *, title, keyword, view, source_id):
        self.summary_payload = {
            "title": title,
            "keywords": [keyword],
            "views": [{"text": view, "source_ids": [source_id]}],
            "observations": [],
            "questions": [],
        }
        return self.checked(self.client.post(f"/api/sessions/{session_id}/summary", json={
            "model_id": self.model_id,
        }))

    def test_cross_session_memory_uses_current_summary_and_respects_sharing(self):
        origin = self.create_session("海边的风筝线")
        original_turn = self.send(origin, "海边的风筝线拉紧时，我觉得很放松。")
        source = next(message for message in original_turn["messages"] if message["role"] == "user")
        self.generate_summary(
            origin, title="海边的风筝线", keyword="风筝线",
            view="旧观察：更喜欢风大的海边", source_id=source["id"],
        )

        unrelated = self.create_session("咖啡器具")
        other_turn = self.send(unrelated, "我想换手冲咖啡用的磨豆机。")
        other_source = next(message for message in other_turn["messages"] if message["role"] == "user")
        self.generate_summary(
            unrelated, title="咖啡器具", keyword="磨豆机",
            view="无关观察：偏爱手摇磨豆机", source_id=other_source["id"],
        )

        token = f'[M{source["id"]}]'

        async def cite_recalled_message(settings, prompt):
            if token in prompt:
                return f"你之前聊过这种感觉。{token}"
            return "这条想法可以继续聊。"

        self.reply = cite_recalled_message
        reader = self.create_session("继续想一想")
        turn = self.send(reader, "风筝线")
        self.assertIn("旧观察", self.chat_prompts[-1])
        self.assertNotIn("无关观察", self.chat_prompts[-1])
        assistant = next(message for message in turn["messages"] if message["role"] == "assistant")
        self.assertIn((origin, source["id"]), {
            (reference["session_id"], reference.get("message_id"))
            for reference in assistant["references"]
        })

        endpoint = f"/api/sessions/{origin}/summary"
        invalid_edit = self.client.patch(endpoint, json={
            "views": [{"text": "引用了别处的原话", "source_ids": [other_source["id"]]}],
        })
        self.assertEqual(invalid_edit.status_code, 422, invalid_edit.text)
        self.checked(self.client.patch(endpoint, json={
            "views": [{"text": "修订观察：喜欢的是掌握风向", "source_ids": [source["id"]]}],
        }))
        self.send(self.create_session("修订后的追问"), "风筝线")
        self.assertIn("修订观察", self.chat_prompts[-1])
        self.assertNotIn("旧观察", self.chat_prompts[-1])

        self.checked(self.client.patch(endpoint, json={"share_enabled": False}))
        turn_without_recall = self.send(self.create_session("另一个念头"), "风筝线")
        self.assertNotIn("修订观察", self.chat_prompts[-1])
        self.assertNotIn(token, self.chat_prompts[-1])
        self.assertFalse(any(message["references"] for message in turn_without_recall["messages"]))

    def test_summary_is_stale_until_regeneration_succeeds(self):
        session_id = self.create_session("散步时长")
        first_turn = self.send(session_id, "以前我只喜欢走十分钟。")
        original = first_turn["messages"][0]
        first_summary = self.generate_summary(
            session_id, title="以前的散步", keyword="散步",
            view="十分钟刚好", source_id=original["id"],
        )
        self.assertEqual(first_summary["model_id"], self.model_id)
        self.assertFalse(first_summary["stale"])

        next_turn = self.send(session_id, "最近我想走三十分钟。")
        self.assertTrue(self.session(session_id)["summary"]["stale"])
        self.fail_summary = True
        result = self.client.post(f"/api/sessions/{session_id}/summary", json={})
        self.assertFalse(result.is_success)
        retained = self.session(session_id)["summary"]
        self.assertEqual(retained["through_message_id"], first_summary["through_message_id"])
        self.assertEqual(retained["views"], first_summary["views"])

        self.fail_summary = False
        updated = self.generate_summary(
            session_id, title="散步时长的变化", keyword="散步",
            view="最近想走三十分钟", source_id=next_turn["messages"][0]["id"],
        )
        self.assertFalse(updated["stale"])
        self.assertGreater(updated["through_message_id"], first_summary["through_message_id"])

    def test_discussion_waits_for_both_independent_answers_before_replying(self):
        first_replies = {
            self.bots[0]["name"]: "独立回应甲：先考察信任。",
            self.bots[1]["name"]: "独立回应乙：先考察分工。",
        }
        initial_bots = set()
        responded_to = set()
        first_round_started = None

        async def discuss(settings, prompt):
            nonlocal first_round_started
            name = settings["bot_name"]
            self.assertIn("失去工作和失去身份是一回事吗", prompt)
            if name not in initial_bots:
                if first_round_started is None:
                    first_round_started = asyncio.Event()
                initial_bots.add(name)
                self.assertTrue(all(reply not in prompt for reply in first_replies.values()))
                if len(initial_bots) == 2:
                    first_round_started.set()
                await asyncio.wait_for(first_round_started.wait(), timeout=5)
                return first_replies[name]

            peer_reply = next(reply for peer, reply in first_replies.items() if peer != name)
            self.assertIn(peer_reply, prompt)
            responded_to.add(name)
            return f"{name}的第二轮回应。"

        self.reply = discuss
        session_id = self.create_session("讨论工作和身份")
        result = self.send(session_id, "失去工作和失去身份是一回事吗？", discuss=True)
        self.assertEqual(result["errors"], [])
        self.assertEqual(responded_to, set(first_replies))
        persisted = self.session(session_id)["messages"]
        assistants = [message for message in persisted if message["role"] == "assistant"]
        self.assertEqual(len(assistants), 4)
        self.assertEqual(len({message["round"] for message in assistants}), 2)

    def test_a_failed_bot_does_not_discard_the_user_or_the_successful_bot(self):
        good_bot, failing_bot = (bot["name"] for bot in self.bots)

        async def partially_unavailable(settings, prompt):
            if settings["bot_name"] == failing_bot:
                raise self.model_error("测试中的机器人故障")
            return "一个正常的回答。"

        self.reply = partially_unavailable
        session_id = self.create_session("部分调用失败")
        result = self.checked(self.client.post(f"/api/sessions/{session_id}/chat", json={
            "content": "这个念头应当先保存。",
            "bot_ids": [bot["id"] for bot in self.bots],
            "discuss": False,
        }))
        self.assertEqual([error["bot_name"] for error in result["errors"]], [failing_bot])
        saved = self.session(session_id)["messages"]
        self.assertEqual([message["role"] for message in saved], ["user", "assistant"])
        self.assertEqual(saved[0]["content"], "这个念头应当先保存。")
        self.assertEqual(saved[1]["bot_name"], good_bot)


if __name__ == "__main__":
    unittest.main()
