import asyncio
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

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
        self.aside = self.default_reply
        self.observe = self.default_observe
        self.route = self.default_route
        self.aside_route = self.skip_aside
        self.chat_prompts = []
        self.route_prompts = []
        self.aside_prompts = []
        stack.enter_context(patch("thought_app.llm.complete", self.fake_complete))
        stack.enter_context(patch("thought_app.sides.SIDE_PROBABILITY", 0))
        self.client = stack.enter_context(TestClient(app))
        config = self.checked(self.client.get("/api/config"))
        self.model_id = config["models"][0]["id"]
        self.bots = config["bots"]

    def default_route(self, prompt):
        candidates = json.loads(prompt.rsplit("\n", 1)[1])["候选人"]
        return self.route_ids(candidates[0]["bot_id"])

    @staticmethod
    def route_ids(*bot_ids, intent="idea"):
        return {"intent": intent, "primary": [{"bot_id": bot_id} for bot_id in bot_ids]}

    @staticmethod
    def skip_aside(prompt):
        return {"bot_id": None}

    async def default_reply(self, settings, prompt):
        return "我会继续追问这个想法。"

    async def default_observe(self, packet):
        return []

    async def fake_complete(self, settings, messages, *, max_tokens=650, json_output=False):
        self.assertTrue(json_output)
        prompt = "\n".join(message["content"] for message in messages)
        task = settings.get("task")
        if task == "route":
            self.route_prompts.append(prompt)
            payload = self.route(prompt)
        elif task == "aside_route":
            payload = self.aside_route(prompt)
        elif task in {"reply", "aside"}:
            handler = self.reply if task == "reply" else self.aside
            prompts = self.chat_prompts if task == "reply" else self.aside_prompts
            prompts.append(prompt)
            answer = await handler(settings, prompt)
            payload = {"reply": None, "inner_thought": None, "memory_to_save": None, "persona_update": None}
            packet = json.loads(messages[-1]["content"])
            payload["address"] = packet["本次发言"]
            target = packet["接这条消息"]
            payload["sources"] = [{"id": target["id"], "from": target["from"], "quote": target["content"][:8]}]
            payload.update(answer if isinstance(answer, dict) else {"reply": answer})
        elif task == "observe":
            payload = {"reply": None, "inner_thought": None,
                       "memory_to_save": await self.observe(json.loads(messages[-1]["content"]))}
        else:
            if self.fail_summary:
                raise self.model_error("测试中的总结模型故障")
            self.assertIsNotNone(self.summary_payload)
            payload = self.summary_payload
        return json.dumps(payload, ensure_ascii=False)

    def checked(self, response):
        self.assertTrue(response.is_success, response.text)
        return response.json()

    def create_session(self, title):
        return self.checked(self.client.post("/api/sessions", json={"title": title}))["id"]

    def session(self, session_id):
        return self.checked(self.client.get(f"/api/sessions/{session_id}"))

    def wait_session(self, session_id, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            detail = self.session(session_id)
            if condition(detail):
                return detail
            time.sleep(0.02)
        self.fail(f"Session {session_id} did not reach the expected background state")

    def send(self, session_id, content, *, discuss=False, bots=None):
        if bots is None:
            bots = self.bots[:1]
        return self.checked(self.client.post(f"/api/sessions/{session_id}/chat", json={
            "content": content,
            "bot_ids": [bot["id"] for bot in bots],
            "discuss": discuss,
        }))

    def mention(self, session_id, bot, content, *, bots=()):
        return self.send(session_id, f"@{bot['name']} {content}", bots=bots)

    def evolution(self, bot):
        return self.checked(self.client.get(f"/api/config/bots/{bot['id']}/evolution"))

    def rewind(self, session_id, message_id):
        return self.client.post(f"/api/sessions/{session_id}/rewind", json={"message_id": message_id})

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

    def test_group_chat_can_save_a_message_without_anyone_replying(self):
        quiet_session = self.create_session("简短问候")
        self.route = lambda prompt: {**self.route_ids(intent="note"), "quiet_quote": "不用回复"}
        result = self.send(quiet_session, "大家好，这条不用回复", bots=self.bots, discuss=True)
        self.assertEqual([message["role"] for message in result["messages"]], ["user"])
        self.assertEqual(result["errors"], [])
        self.assertFalse(self.chat_prompts)

        planning_calls = len(self.route_prompts)
        self.send(quiet_session, "只想把这条记下来", bots=[])
        self.assertEqual(len(self.route_prompts), planning_calls)
        self.assertEqual(
            [message["content"] for message in self.session(quiet_session)["messages"]],
            ["大家好，这条不用回复", "只想把这条记下来"],
        )

        self.route = lambda prompt: {**self.route_ids(intent="note"), "quiet_quote": "只是随便记一句"}
        declined = self.send(quiet_session, "我只是随便记一句。")
        self.assertEqual([message["role"] for message in declined["messages"]], ["user"])
        self.assertEqual(declined["errors"], [])

    def test_followup_survives_empty_route_and_invalid_or_empty_reply(self):
        first, second = self.bots[:2]
        for bad_reply in (None, "invalid_json"):
            with self.subTest(bad_reply=bad_reply):
                session_id = self.create_session("接着聊诗")
                self.mention(session_id, second, "我喜欢这首诗。")
                self.route = lambda prompt: self.route_ids(intent="idea")
                complete = self.fake_complete
                attempts = []

                async def reply_once_invalid(settings, messages, **kwargs):
                    if settings.get("task") == "reply":
                        attempts.append(settings["bot_id"])
                        if len(attempts) == 1:
                            if bad_reply == "invalid_json":
                                return "reply unavailable as JSON"
                            return json.dumps({"reply": None})
                    return await complete(settings, messages, **kwargs)

                with patch("thought_app.llm.complete", reply_once_invalid):
                    result = self.send(session_id, "我不想多解释，倒还想聊聊海。", bots=[first, second])
                self.assertEqual(result["errors"], [])
                user, answer = result["messages"]
                self.assertEqual(attempts, [second["id"], second["id"]])
                self.assertEqual(answer["reply_to"], user["id"])
                self.assertEqual(answer["bot_id"], second["id"])

    def test_opening_sharing_a_poem_gets_a_reply_even_after_empty_route_and_decline(self):
        self.route = lambda prompt: self.route_ids(intent="note")

        async def initially_quiet(settings, prompt):
            return None if len(self.chat_prompts) == 1 else "我喜欢你写的树影。"

        self.reply = initially_quiet
        session_id = self.create_session("诗")
        result = self.send(session_id, "我喜欢这句：地上的树影也在开花。")
        self.assertEqual(result["errors"], [])
        opening, answer = result["messages"]
        self.assertEqual(answer["reply_to"], opening["id"])
        self.assertEqual(answer["content"], "我喜欢你写的树影。")

    def test_configured_router_and_formatter_recover_without_rewriting_the_original_draft(self):
        config = self.checked(self.client.get("/api/config"))
        persona = config["models"][0]
        router, formatter = [self.checked(self.client.post("/api/config/models", json={
            "provider_id": persona["provider_id"], "name": name, "model_code": code,
        })) for name, code in (("路由", "router"), ("格式修复", "formatter"))]
        self.checked(self.client.patch("/api/config/reply-models", json={
            "router_model_id": router["id"], "fallback_model_id": formatter["id"],
        }))
        wording = "今天想先把结构弄清楚，然后再改。"
        calls = []

        async def reply_with_recovery(settings, messages, **options):
            task = settings["task"]
            if task not in {"route", "reply", "reply_repair", "reply_retry"}:
                return await self.fake_complete(settings, messages, **options)
            calls.append((task, settings["id"]))
            if task == "route":
                return await self.fake_complete(settings, messages, **options)
            packet = json.loads(messages[-1]["content"])
            result = {"reply": wording, "address": packet["本次发言"], "sources": []}
            if task == "reply":
                if packet["接这条消息"]["content"] == "结构化草稿":
                    return json.dumps(result, ensure_ascii=False)[:-1]
                return wording
            if task == "reply_repair":
                self.assertEqual(set(packet), {"本次发言", "待修复输出"})
                if packet["待修复输出"] == wording:
                    result["reply"] = "今天"
            if task == "reply_retry":
                self.assertEqual(settings["bot_id"], self.bots[0]["id"])
                self.assertIn("重新回答这一条消息", messages[0]["content"])
                result["reply"] = "可以先保留一份结构，再动正文。"
            return json.dumps(result, ensure_ascii=False)

        with patch("thought_app.llm.complete", reply_with_recovery):
            session_id = self.create_session("格式恢复")
            repaired = self.send(session_id, "结构化草稿")
            self.assertEqual(calls, [("route", router["id"]), ("reply", persona["id"]), ("reply_repair", formatter["id"])])
            self.assertFalse(repaired["errors"])
            self.assertEqual(repaired["messages"][1]["content"], wording)
            self.assertEqual(repaired["messages"][1]["model_code"], persona["model_code"])

            calls.clear()
            reanswered = self.send(session_id, "纯文本草稿")
            self.assertEqual(calls, [("route", router["id"]), ("reply", persona["id"]),
                                     ("reply_repair", formatter["id"]), ("reply_retry", router["id"])])
            self.assertFalse(reanswered["errors"])
            user, answer = reanswered["messages"]
            self.assertEqual(answer["content"], "可以先保留一份结构，再动正文。")
            self.assertEqual(answer["reply_to"], user["id"])
            self.assertEqual(answer["bot_id"], self.bots[0]["id"])
            self.assertEqual(answer["model_code"], router["model_code"])

    def test_one_other_eligible_friend_answers_when_primary_fails(self):
        first, second = self.bots[:2]
        session_id = self.create_session("接力")
        self.route = lambda prompt: self.route_ids(first["id"])

        async def unavailable(settings, prompt):
            if settings["bot_id"] == first["id"]:
                raise self.model_error("模型暂时无法回应")
            return "我也很喜欢那一句。"

        self.reply = unavailable
        result = self.send(session_id, "接着刚才那句说。", bots=[first, second])
        user, answer = result["messages"]
        self.assertEqual(answer["bot_id"], second["id"])
        self.assertEqual(answer["reply_to"], user["id"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(self.chat_prompts), 2)

    def test_old_reply_length_setting_is_ignored_on_restore(self):
        from thought_app.database import connect

        with connect() as db:
            db.execute("INSERT INTO config_settings (key, value) VALUES ('long_replies', '1')")
        exported = self.client.get("/api/backup/export")
        exported.raise_for_status()
        with ZipFile(io.BytesIO(exported.content)) as archive:
            payload = json.loads(archive.read("journal.json"))
        stored = payload["tables"]["config_settings"]
        legacy = {"key": "long_replies", "value": "1"}
        row = [legacy[column] for column in stored["columns"]]
        self.assertNotIn(row, stored["rows"])
        stored["rows"].append(row)

        old_backup = io.BytesIO()
        with ZipFile(old_backup, "w") as archive:
            archive.writestr("journal.json", json.dumps(payload))
        self.checked(self.client.post("/api/backup/import", content=old_backup.getvalue(),
                                      headers={"Content-Type": "application/zip"}))
        self.assertNotIn("long_replies", self.checked(self.client.get("/api/config")))
        with connect() as db:
            self.assertIsNone(db.execute("SELECT value FROM config_settings WHERE key = 'long_replies'").fetchone())

    def test_group_speakers_see_earlier_replies_but_respond_to_the_user(self):
        first, second = self.bots[:2]
        first_reply = "我想问：想到的点子一定要展开吗？"
        session_id = self.create_session("讨论点子")

        def route(prompt):
            self.assertNotIn(first_reply, prompt)
            return self.route_ids(first["id"], second["id"])

        async def reply(settings, prompt):
            if settings["bot_id"] == first["id"]:
                return first_reply
            self.assertEqual(settings["bot_id"], second["id"])
            self.assertIn(first_reply, prompt)
            return "不一定。有些点子可以只留一句原话。"

        self.route = route
        self.reply = reply
        turn = self.send(session_id, "怎样记随手冒出来的点子？", bots=self.bots, discuss=True)
        self.assertEqual(turn["errors"], [])
        user_message, first_message, second_message = turn["messages"]
        self.assertEqual(first_message["bot_id"], first["id"])
        self.assertEqual(second_message["bot_id"], second["id"])
        self.assertEqual(first_message["reply_to"], user_message["id"])
        self.assertEqual(second_message["reply_to"], user_message["id"])
        self.assertEqual([message["channel"] for message in turn["messages"]], ["main"] * 3)
        self.assertEqual(len(self.route_prompts), 1)
        self.assertEqual(self.session(session_id)["messages"], turn["messages"])

    def test_default_input_routes_all_bots_but_caps_replies_by_intent(self):
        for intent, expected_limit in [("idea", 2), ("greeting", 1)]:
            with self.subTest(intent=intent):
                def route_all(prompt):
                    self.assertEqual(len(json.loads(prompt.rsplit("\n", 1)[1])["候选人"]), len(self.bots))
                    return self.route_ids(*(bot["id"] for bot in self.bots), intent=intent)

                self.route = route_all
                session_id = self.create_session("避免刷屏")
                turn = self.checked(self.client.post(f"/api/sessions/{session_id}/chat", json={"content": "这件事"}))
                self.assertEqual(turn["errors"], [])
                speakers = [message["bot_id"] for message in turn["messages"][1:]]
                self.assertEqual(speakers, [bot["id"] for bot in self.bots[:expected_limit]])

    def test_direct_mention_answers_without_asking_other_robots_to_speak(self):
        first, second = self.bots[:2]
        session_id = self.create_session("点名")
        result = self.mention(session_id, second, "你觉得这句话适合继续吗？", bots=[first])
        self.assertFalse(self.route_prompts)
        self.assertEqual(len(self.chat_prompts), 1)
        user, response = result["messages"]
        self.assertEqual(response["bot_id"], second["id"])
        self.assertEqual(response["reply_to"], user["id"])

    def test_friend_can_finish_a_long_reply_to_a_first_time_story(self):
        bot = next(bot for bot in self.bots if bot["name"] == "造梦师")
        story = "爸爸今天自己点开了小电台。"
        answer = "这让我想起给一个人做东西的乐趣。" * 12 + "有一个听众就够了。"

        async def reply(settings, prompt):
            self.assertEqual(prompt.count(story), 1)
            return answer

        self.reply = reply
        session_id = self.create_session("小电台")
        self.mention(session_id, bot, story)
        self.assertEqual(self.session(session_id)["messages"][-1]["content"], answer)

    def test_a_failed_bot_does_not_discard_the_user_or_the_successful_bot(self):
        good_bot, failing_bot = self.bots[:2]
        self.route = lambda prompt: self.route_ids(good_bot["id"], failing_bot["id"])

        async def partially_unavailable(settings, prompt):
            if settings["bot_id"] == failing_bot["id"]:
                raise self.model_error("测试中的机器人故障")
            return "一个正常的回答。"

        self.reply = partially_unavailable
        session_id = self.create_session("部分调用失败")
        result = self.send(session_id, "这个念头应当先保存。", bots=[good_bot, failing_bot])
        self.assertEqual([error["bot_name"] for error in result["errors"]], [failing_bot["name"]])
        saved = self.session(session_id)["messages"]
        self.assertEqual([message["role"] for message in saved], ["user", "assistant"])
        self.assertEqual(saved[0]["content"], "这个念头应当先保存。")
        self.assertEqual(saved[1]["bot_name"], good_bot["name"])

    def test_evolution_is_private_to_its_owner_and_keeps_the_source_of_each_version(self):
        owner, other = self.bots[:2]
        initial = "观鸟时，我更喜欢原地等鸟来。"
        revised = "现在我会先走一段再停下来观鸟。"

        async def first_reply(settings, prompt):
            return {"reply": initial, "persona_update": {
                "kind": "view", "text": initial, "keywords": ["观鸟"], "evidence": initial,
            }}

        self.reply = first_reply
        origin = self.create_session("河边散步")
        first_turn = self.mention(origin, owner, "河边观鸟时你会怎么做？")
        [original] = self.evolution(owner)["notes"]
        self.assertTrue(original["active"])
        self.assertEqual(original["source_session_id"], origin)
        self.assertEqual(original["source_message_id"], first_turn["messages"][1]["id"])

        async def change_reply(settings, prompt):
            if settings["bot_id"] == owner["id"]:
                self.assertIn(initial, prompt)
            else:
                self.assertNotIn(initial, prompt)
            return {"reply": revised, "persona_update": {
                "kind": "view", "text": revised, "keywords": ["观鸟"], "evidence": revised,
                "replaces_id": original["id"],
            }}

        self.reply = change_reply
        self.mention(self.create_session("另一个角色"), other, "观鸟时你会怎么做？")
        self.assertEqual(self.evolution(other)["notes"], [])

        new_session = self.create_session("第二次聊观鸟")
        next_turn = self.mention(new_session, owner, "你对观鸟改主意了吗？")
        new_message = next_turn["messages"][1]
        history = {item["text"]: item for item in self.evolution(owner)["notes"]}
        self.assertFalse(history[initial]["active"])
        self.assertTrue(history[revised]["active"])
        self.assertEqual(history[revised]["replaces_id"], original["id"])
        self.assertEqual(history[revised]["source_session_id"], new_session)
        self.assertEqual(history[revised]["source_message_id"], new_message["id"])

        deleted = self.client.delete(f"/api/sessions/{origin}")
        self.assertEqual(deleted.status_code, 204, deleted.text)
        [remaining] = self.evolution(owner)["notes"]
        self.assertEqual(remaining["id"], history[revised]["id"])
        self.assertIsNone(remaining["replaces_id"])

    def test_evolution_requires_visible_evidence_and_can_be_paused_or_removed(self):
        owner = self.bots[0]
        path = f"/api/config/bots/{owner['id']}/evolution"
        session_id = self.create_session("存下观察或忽略草稿")
        view = "我更喜欢先观察叶片，再猜是哪种树。"
        next_view = "我也对树皮的纹理感兴趣。"
        proposal = {"kind": "interest", "text": view, "keywords": ["树木"], "evidence": view}

        async def invented_evidence(settings, prompt):
            return {"reply": "树木确实适合慢慢看。", "persona_update": proposal}

        self.reply = invented_evidence
        result = self.mention(session_id, owner, "你喜欢怎么观察树木？")
        self.assertEqual(result["messages"][1]["content"], "树木确实适合慢慢看。")
        self.assertEqual(self.evolution(owner)["notes"], [])

        async def supported_evidence(settings, prompt):
            return {"reply": view, "persona_update": proposal}

        self.reply = supported_evidence
        self.mention(session_id, owner, "说说你自己会怎么观察树木。")
        [record] = self.evolution(owner)["notes"]
        self.checked(self.client.patch(path, json={"enabled": False}))

        async def paused_reply(settings, prompt):
            self.assertIn(view, prompt)
            return {"reply": next_view, "persona_update": {
                **proposal, "text": next_view, "evidence": next_view,
            }}

        self.reply = paused_reply
        self.mention(self.create_session("暂停时继续聊天"), owner, "树木还有什么值得看？")
        self.assertEqual(self.evolution(owner), {"enabled": False, "notes": [record]})
        response = self.client.delete(f"{path}/{record['id']}")
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(self.evolution(owner)["notes"], [])

    def test_rewind_keeps_the_prefix_and_lets_a_different_robot_answer_fresh(self):
        first, second = self.bots[:2]
        session_id = self.create_session("山路")
        saved = self.send(session_id, "我第一次走这条山路。", bots=[first])["messages"]
        preserved_summary = self.generate_summary(
            session_id, title="走山路", keyword="山路", view="第一次走", source_id=saved[0]["id"],
        )

        discarded_reply = "这次我们应当夜里从岔道走。"

        async def old_reply(settings, prompt):
            return discarded_reply

        self.reply = old_reply
        draft = "你觉得我应该继续走这条山路吗？"
        cut = self.send(session_id, draft, bots=[first])["messages"]
        self.send(session_id, "第二轮已经走到半山。", bots=[first])
        other_session = self.create_session("另一个会话")
        other_saved = self.send(other_session, "另一段独立记录。", bots=[])["messages"]

        self.assertFalse(self.rewind(session_id, saved[1]["id"]).is_success)
        self.assertFalse(self.rewind(session_id, other_saved[0]["id"]).is_success)
        self.assertEqual(len(self.session(session_id)["messages"]), 6)
        self.assertIsNone(self.session(session_id)["rewind_draft"])
        rewind = self.checked(self.rewind(session_id, cut[0]["id"]))
        self.assertEqual(rewind["draft"], draft)
        self.assertEqual(rewind["removed_count"], 4)
        self.assertFalse(rewind["summary_removed"])
        reverted = self.session(session_id)
        self.assertEqual(reverted["messages"], saved)
        self.assertEqual(reverted["summary"], preserved_summary)
        self.assertEqual(reverted["rewind_draft"], draft)
        self.assertEqual(self.session(other_session)["messages"], other_saved)

        async def new_reply(settings, prompt):
            self.assertIn(saved[0]["content"], prompt)
            self.assertNotIn(discarded_reply, prompt)
            self.assertNotIn("第二轮已经走到半山", prompt)
            return "我会等明早再出发。"

        self.reply = new_reply
        self.route = lambda prompt: self.route_ids(second["id"])
        replay = self.send(session_id, rewind["draft"], bots=[second])["messages"]
        self.assertNotEqual(replay[0]["id"], cut[0]["id"])
        self.assertEqual(replay[1]["bot_id"], second["id"])
        self.assertEqual(replay[1]["reply_to"], replay[0]["id"])
        replayed = self.session(session_id)
        self.assertEqual(replayed["messages"], saved + replay)
        self.assertIsNone(replayed["rewind_draft"])

    def test_rewind_invalidates_affected_summaries_and_retains_the_sharing_preference(self):
        for manually_linked in (False, True):
            with self.subTest(manually_linked=manually_linked):
                self.reply = self.default_reply
                origin = self.create_session("山谷的黄鹂")
                prefix = self.send(origin, "过去我只在山谷听过黄鹂。", bots=[])["messages"][0]
                summary_path = f"/api/sessions/{origin}/summary"

                if manually_linked:
                    initial = self.generate_summary(
                        origin, title="山谷的黄鹂", keyword="黄鹂", view="山谷的叫声", source_id=prefix["id"],
                    )
                    self.assertEqual(initial["through_message_id"], prefix["id"])

                cut = self.send(origin, "今天在窗外也听见黄鹂。", bots=[])["messages"][0]
                later_view = {"text": "窗外也有黄鹂", "source_ids": [cut["id"]]}
                if manually_linked:
                    self.checked(self.client.patch(summary_path, json={"views": [later_view]}))
                else:
                    self.generate_summary(
                        origin, title="窗外的黄鹂", keyword="黄鹂", view=later_view["text"], source_id=cut["id"],
                    )

                async def recall_original(settings, prompt):
                    token = f'[M{cut["id"]}]'
                    self.assertIn(token, prompt)
                    return f"你以前听到了同一种鸟。{token}"

                self.reply = recall_original
                reader = self.create_session("再聊黄鹂")
                cited = self.send(reader, "洪水之后还有黄鹂吗？")["messages"][1]
                self.assertTrue(any(ref.get("message_id") == cut["id"] for ref in cited["references"]))
                self.checked(self.client.patch(summary_path, json={"share_enabled": False}))

                rolled_back = self.checked(self.rewind(origin, cut["id"]))
                self.assertTrue(rolled_back["summary_removed"])
                self.assertIsNone(self.session(origin)["summary"])
                persisted = self.session(reader)["messages"][-1]
                self.assertEqual(persisted["content"], cited["content"])
                self.assertFalse(any(ref.get("message_id") == cut["id"] for ref in persisted["references"]))

                regenerated = self.generate_summary(
                    origin, title="山谷的黄鹂", keyword="黄鹂", view="山谷的叫声", source_id=prefix["id"],
                )
                self.assertFalse(regenerated["share_enabled"])
                self.assertEqual(regenerated["through_message_id"], prefix["id"])

    def test_rewind_restores_only_notes_without_a_surviving_successor(self):
        bot = self.bots[0]
        origin = self.create_session("下班后的兴趣")

        def say_note(session_id, keyword, view, replaces_id=None):
            async def reply(settings, prompt):
                return {"reply": view, "persona_update": {
                    "kind": "view", "text": view, "keywords": [keyword], "evidence": view,
                    "replaces_id": replaces_id,
                }}

            self.reply = reply
            user, assistant = self.mention(session_id, bot, f"你怎么想{keyword}？")["messages"]
            note = next(item for item in self.evolution(bot)["notes"] if item["source_message_id"] == assistant["id"])
            return user, note

        _, original_bird = say_note(origin, "观鸟", "观鸟最好站在同一个地方慢慢等。")
        _, original_flute = say_note(origin, "竹笛", "学竹笛先练呼吸比较有用。")
        cut, _ = say_note(origin, "观鸟", "观鸟之前我想先走一段。", original_bird["id"])
        _, revised_flute = say_note(origin, "竹笛", "学竹笛可以从一个短谱开始。", original_flute["id"])
        survivor_session = self.create_session("独立的竹笛讨论")
        _, surviving_flute = say_note(
            survivor_session, "竹笛", "现在我想找个伙伴一起练竹笛。", revised_flute["id"],
        )

        rewind = self.checked(self.rewind(origin, cut["id"]))
        self.assertEqual(rewind["restored_note_count"], 1)
        notes = {item["id"]: item for item in self.evolution(bot)["notes"]}
        self.assertEqual(set(notes), {original_bird["id"], original_flute["id"], surviving_flute["id"]})
        self.assertTrue(notes[original_bird["id"]]["active"])
        self.assertFalse(notes[original_flute["id"]]["active"])
        self.assertTrue(notes[surviving_flute["id"]]["active"])
        self.assertEqual(notes[surviving_flute["id"]]["replaces_id"], original_flute["id"])
        self.assertEqual(notes[surviving_flute["id"]]["source_session_id"], survivor_session)

    def test_sidebar_replies_to_real_messages_and_has_a_separate_budget(self):
        first, second, third = self.bots[:3]
        session_id = self.create_session("侧栏接话")
        claim = "点子必须写成完整文章才值得考虑。"
        correction = "我会保留半句话，因为以后才知道要不要展开。"

        async def reply(settings, prompt):
            return {"reply": claim, "inner_thought": "我习惯先看成品。"}

        def choose_challenger(prompt):
            target = json.loads(prompt.rsplit("\n", 1)[1])["只接这条发言"]
            self.assertEqual(target["content"], claim)
            return {"bot_id": second["id"], "reason": "半句话本身也值得留"}

        async def aside(settings, prompt):
            packet = json.loads(prompt.rsplit("\n", 1)[1])
            target = packet["接这条消息"]
            address = packet["本次发言"]
            self.assertEqual(address["from"], f"bot:{settings['bot_id']}")
            self.assertEqual((address["to"], address["reply_to"]), (target["from"], target["id"]))
            self.assertEqual({p["id"] for p in packet["群成员"]}, {"user", *(f"bot:{b['id']}" for b in self.bots)})
            [opening] = [m for m in packet["聊天历史"] if m["id"] == packet["本轮用户消息id"]]
            self.assertEqual((opening["from"], opening["content"]), ("user", "随手记下这半句话。"))
            if settings["bot_id"] == second["id"]:
                self.assertEqual(target["content"], claim)
                return correction
            self.assertEqual(target["to"], [address["from"]])
            self.assertEqual(target["content"], correction)
            return "那我改口，先留一句备忘就够了。"

        self.reply, self.aside_route, self.aside = reply, choose_challenger, aside
        turn = self.send(session_id, "随手记下这半句话。", bots=[first, second])
        user, primary = turn["messages"]
        self.assertEqual(primary["channel"], "main")
        self.assertEqual(primary["inner_thought"], "我习惯先看成品。")
        settled = self.wait_session(session_id, lambda detail: not detail["side_active"])
        self.assertIsNone(settled["side_error"])
        self.assertEqual(len(settled["messages"]), 4)
        challenge, response = settled["messages"][2:]
        self.assertEqual([message["channel"] for message in (challenge, response)], ["aside", "aside"])
        self.assertEqual([message["turn_id"] for message in (challenge, response)], [user["id"]] * 2)
        self.assertEqual(challenge["reply_to"], primary["id"])
        self.assertEqual(response["reply_to"], challenge["id"])
        self.assertEqual(len(self.route_prompts), 1)

        async def manual_aside(settings, prompt):
            self.assertEqual(settings["bot_id"], third["id"])
            return "也可以给这半句话加个适合回看的日期。"

        self.aside = manual_aside
        interjection = self.client.post(f"/api/sessions/{session_id}/side", json={
            "bot_id": third["id"], "message_id": primary["id"],
        })
        self.assertEqual(interjection.status_code, 202, interjection.text)
        settled = self.wait_session(session_id, lambda detail: not detail["side_active"])
        manual = settled["messages"][-1]
        self.assertEqual(manual["bot_id"], third["id"])
        self.assertEqual(manual["channel"], "aside")
        self.assertEqual(manual["reply_to"], primary["id"])

        async def respond_to_user(settings, prompt):
            packet = json.loads(prompt.rsplit("\n", 1)[1])
            self.assertEqual(packet["本次发言"]["to"], "user")
            self.assertIn("朋友聊天", prompt)
            history = {m["id"]: m for m in packet["聊天历史"]}
            self.assertEqual(history[response["id"]]["reply_to"], challenge["id"])
            self.assertEqual(history[challenge["id"]]["to"], [f"bot:{first['id']}"])
            return "可以只记半句话。"

        self.aside = respond_to_user
        self.checked(self.client.post(f"/api/sessions/{session_id}/side", json={
            "bot_id": third["id"], "message_id": user["id"],
        }))
        settled = self.wait_session(session_id, lambda detail: not detail["side_active"])
        self.assertEqual(settled["messages"][-1]["reply_to"], user["id"])

    def test_manual_sidebar_invitation_retries_or_reports_an_empty_reply(self):
        from thought_app.conversation import Conversation

        session_id = self.create_session("请接我的原话")
        [user] = self.send(session_id, "这句先记下来。", bots=[])["messages"]
        invite = {"bot_id": self.bots[1]["id"], "message_id": user["id"]}

        async def silent(settings, prompt):
            packet = json.loads(prompt.rsplit("\n", 1)[1])
            self.assertEqual(packet["本次发言"], {
                "from": f"bot:{invite['bot_id']}", "to": "user", "reply_to": user["id"],
            })
            self.assertEqual(packet["接这条消息"]["content"], user["content"])
            return None

        self.aside = silent
        self.checked(self.client.post(f"/api/sessions/{session_id}/side", json=invite))
        settled = self.wait_session(session_id, lambda detail: not detail["side_active"])
        self.assertEqual(len(self.aside_prompts), 2)
        self.assertEqual(len(settled["messages"]), 1)
        self.assertIn("可显示", settled["side_error"])

        self.aside = self.default_reply
        self.checked(self.client.post(f"/api/sessions/{session_id}/side", json=invite))
        settled = self.wait_session(session_id, lambda detail: not detail["side_active"])
        self.assertIsNone(settled["side_error"])
        [_, interjection] = settled["messages"]
        self.assertEqual((interjection["channel"], interjection["reply_to"]), ("aside", user["id"]))
        self.assertEqual(Conversation(settled["messages"]).event(interjection)["to"], ["user"])
        self.assertEqual(len(self.route_prompts), 0)

    def test_wrong_recipient_is_retried_and_never_posted(self):
        async def misaddressed(settings, prompt):
            packet = json.loads(prompt.rsplit("\n", 1)[1])
            return {"address": {**packet["本次发言"], "to": "another_bot"}, "reply": "给错人的回复。"}

        self.reply = misaddressed
        session_id = self.create_session("核对收件人")
        result = self.mention(session_id, self.bots[0], "这句话请你接。")
        self.assertEqual(len(self.chat_prompts), 2)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(self.session(session_id)["messages"]), 1)
        self.assertEqual(result["messages"][0]["audience"], [f"bot:{self.bots[0]['id']}"])

    def test_invented_quote_is_rewritten_before_posting(self):
        async def cite(settings, prompt):
            if len(self.chat_prompts) == 1:
                target = json.loads(prompt.rsplit("\n", 1)[1])["接这条消息"]
                return {"reply": "你以前这么说过。", "sources": [
                    {"id": target["id"], "from": target["from"], "quote": "不存在的往事"},
                ]}
            return "这个名字值得留着。"

        self.reply = cite
        session_id = self.create_session("核对出处")
        result = self.mention(session_id, self.bots[0], "路边拾念这个名字怎么样？")
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(self.chat_prompts), 2)
        self.assertEqual(self.session(session_id)["messages"][-1]["content"], "这个名字值得留着。")

    def test_recalls_earlier_peer_exchange_outside_recent_window(self):
        first, second = self.bots[:2]
        session_id = self.create_session("想法明信片")

        async def card(settings, prompt):
            return "把它印成明信片，收到时再拆。"

        self.reply = card
        opening, original = self.mention(session_id, first, "想让点子过几天回来。")["messages"]
        self.checked(self.client.post(f"/api/sessions/{session_id}/side", json={
            "bot_id": second["id"], "message_id": original["id"],
        }))
        side = self.wait_session(session_id, lambda detail: not detail["side_active"])["messages"][-1]
        for i in range(17):
            self.send(session_id, f"记一笔气温 {i}", bots=[])
        self.mention(session_id, second, "上回是谁先提出明信片的？")
        packet = json.loads(self.chat_prompts[-1].rsplit("\n", 1)[1])
        history = {m["id"]: m for m in packet["聊天历史"]}
        self.assertEqual(history[original["id"]]["from"], f"bot:{first['id']}")
        self.assertEqual(history[side["id"]]["reply_to"], original["id"])
        self.assertEqual(history[opening["id"]]["from"], "user")

    def test_new_main_message_interrupts_inflight_sidebar_reply(self):
        session_id = self.create_session("可以随时打断")
        started = threading.Event()
        cancelled = threading.Event()

        async def blocked_aside(settings, prompt):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.aside = blocked_aside
        _, primary = self.send(session_id, "我想先写这段话。")["messages"]
        interjection = self.client.post(f"/api/sessions/{session_id}/side", json={
            "bot_id": self.bots[1]["id"], "message_id": primary["id"],
        })
        self.assertEqual(interjection.status_code, 202, interjection.text)
        self.assertTrue(started.wait(3))
        self.assertTrue(self.session(session_id)["side_active"])

        turn = self.send(session_id, "等一下，我有另一个想法。", bots=[])
        self.assertEqual(len(turn["messages"]), 1)
        self.assertTrue(cancelled.wait(3))
        detail = self.session(session_id)
        self.assertFalse(detail["side_active"])
        self.assertEqual([item["channel"] for item in detail["messages"]], ["main"] * 3)

        started.clear()
        cancelled.clear()
        interjection = self.client.post(f"/api/sessions/{session_id}/side", json={
            "bot_id": self.bots[1]["id"], "message_id": turn["messages"][0]["id"],
        })
        self.assertEqual(interjection.status_code, 202, interjection.text)
        self.assertTrue(started.wait(3))
        stopped = self.client.post(f"/api/sessions/{session_id}/side/stop")
        self.assertEqual(stopped.status_code, 204, stopped.text)
        self.assertTrue(cancelled.wait(3))
        self.assertFalse(self.session(session_id)["side_active"])

    def test_observer_runs_independently_and_grounds_recall_and_mirror_in_user_sources(self):
        origin = self.create_session("骑车与自由")
        raw = "我喜欢一个人骑车，感觉自己说走就走。"
        observation = "重视能够自行决定出发的自由"
        assistant_only = "助手发言中的独有字符串"
        invalid_observation = "来自前台机器人的切片不应该写入"
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        async def observe(packet):
            serialized = json.dumps(packet, ensure_ascii=False)
            self.assertNotIn(assistant_only, serialized)
            self.assertNotIn("前台角色旁白也不应注入", serialized)
            if packet["当前用户原话"]["content"] != raw:
                return []
            started.set()
            self.assertTrue(await asyncio.to_thread(release.wait, 3))
            return [
                {"kind": "value", "text": observation, "certainty": "inferred",
                 "evidence": "感觉自己说走就走", "keywords": ["骑车", "自由"]},
                {"kind": "mood", "text": "捏造的情绪", "certainty": "inferred",
                 "evidence": "感觉自己说走就走", "keywords": ["骑车"]},
                {"kind": "stance", "text": "无来源的看法", "certainty": "explicit",
                 "evidence": assistant_only, "keywords": ["骑车"]},
            ]

        async def initial_reply(settings, prompt):
            return {"reply": assistant_only, "inner_thought": "前台角色旁白也不应注入",
                    "memory_to_save": [{"kind": "value", "text": invalid_observation, "certainty": "explicit",
                                       "evidence": "感觉自己说走就走", "keywords": ["骑车"]}]}

        self.observe, self.reply = observe, initial_reply
        user, primary = self.send(origin, raw)["messages"]
        self.assertEqual(primary["content"], assistant_only)
        self.assertTrue(started.wait(3))
        self.assertEqual(self.session(origin)["observer_pending"], 1)
        release.set()
        detail = self.wait_session(origin, lambda item: item["observer_pending"] == 0)
        [saved] = detail["observations"]
        self.assertEqual(saved["text"], observation)
        self.assertEqual(saved["source_message_id"], user["id"])
        self.assertEqual(saved["certainty"], "inferred")
        self.assertEqual(detail["observer_failed"], 0)

        token = f"[M{user['id']}]"
        mirror = next(bot for bot in self.bots if bot["role_mode"] == "mirror")

        async def grounded_reply(settings, prompt):
            self.assertIn(observation, prompt)
            self.assertIn(token, prompt)
            if settings["role_mode"] == "mirror":
                self.assertIn(raw, prompt)
                self.assertNotIn(assistant_only, prompt)
                self.assertNotIn(invalid_observation, prompt)
            return f"我想保留自己决定什么时候出发的空间。{token}"

        self.reply = grounded_reply
        friend_session = self.create_session("回看朋友")
        friend_reply = self.send(friend_session, "骑车的自由感是什么？")["messages"][1]
        self.assertEqual(friend_reply["references"][0]["message_id"], user["id"])

        quiet_session = self.create_session("影子自我平时保持安静")
        self.route = lambda prompt: self.route_ids(mirror["id"], intent="idea")
        quiet = self.send(quiet_session, "我又想到骑车了。", bots=self.bots)
        self.assertEqual(quiet["errors"], [])
        self.assertTrue(all(m["bot_id"] != mirror["id"] for m in quiet["messages"]))

        mirror_session = self.create_session("回看自己")
        mirror_reply = self.mention(mirror_session, mirror, "骑车对我有什么吸引力？")["messages"][1]
        self.assertEqual(mirror_reply["references"][0]["message_id"], user["id"])
        self.assertEqual(mirror_reply["bot_id"], mirror["id"])

        self.checked(self.rewind(origin, user["id"]))
        self.assertEqual(self.session(origin)["observations"], [])
        self.assertEqual(self.session(mirror_session)["messages"][-1]["references"], [])


if __name__ == "__main__":
    unittest.main()
