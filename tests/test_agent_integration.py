import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class AgentIntegrationTests(unittest.TestCase):
    CONFIG = "/api/config/agents"
    PERSONA = "默默记住用户说要分享的东西，日后可接着问。"
    PROMISE = "港口旧钟的画画完以后我给你看"
    REVIEW = "翻一下之前的聊天，我对旧电影院的看法是怎么变的？"

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict(os.environ, {
            "THOUGHT_DB": str(directory / "agent.sqlite3"),
            "DEEPSEEK_API_KEY": "synthetic-agent-key",
        }))
        from thought_app.app import app

        self.calls = []
        self.expressions = []
        self.structured_archive = False
        stack.enter_context(patch("thought_app.llm.complete", self.fake_complete))
        stack.enter_context(patch("thought_app.sides.SIDE_PROBABILITY", 0))
        self.client = stack.enter_context(TestClient(app))
        config = self.checked(self.client.get("/api/config"))
        model = next(model for model in config["models"] if model["model_code"] == "deepseek-flash")
        self.persona = self.checked(self.client.post("/api/config/personas", json={
            "name": "长记性", "prompt": self.PERSONA,
        }))
        self.bot = self.checked(self.client.post("/api/config/bots", json={
            "name": "测试拾荒者", "persona_id": self.persona["id"], "model_id": model["id"],
        }))

    def checked(self, response):
        self.assertTrue(response.is_success, response.text)
        return response.json() if response.content else None

    def session(self, title):
        return self.checked(self.client.post("/api/sessions", json={"title": title}))

    def detail(self, session):
        return self.checked(self.client.get(f"/api/sessions/{session['id']}"))

    def send(self, session, text, *, mention=True):
        if mention:
            text = f"@{self.bot['name']} {text}"
        path = f"/api/sessions/{session['id']}/chat"
        return self.checked(self.client.post(path, json={
            "content": text, "use_agent": True, "bot_ids": [self.bot["id"]] if mention else [],
        }))

    async def fake_complete(self, settings, messages, **options):
        task = settings["task"]
        packet = json.loads(messages[-1]["content"])
        self.calls.append((task, packet, settings.get("thinking_effort")))
        if task == "observe":
            result = {"memory_to_save": []}
        elif task == "route":
            result = {"intent": "idea", "primary": [{"bot_id": bot["bot_id"]} for bot in packet["候选人"]]}
        elif task == "agent_compile":
            result = {"voice": "自然说话。", "skills": [{
                "name": "记住分享约定", "source": packet["source"],
                "attention": "注意将来分享的约定。", "recall": "查找未完成的分享约定。",
                "decision": "合适时接上约定。", "reflect": "记下明确的分享约定。",
                "express": "交代依据和不确定的部分。",
                "reads": ["promise"], "writes": ["promise"], "archive": "",
            }]}
        elif task == "agent_recall":
            result = {"id": None, "cue": None, "resolved": False}
            if "画完了" in packet["new_message"]:
                result.update(id=packet["candidates"][0]["id"], cue="画完了")
        elif task == "agent_archive_summarize":
            result = {"points": [{"text": row["text"], "source_ids": [row["id"]]}
                                 for row in packet["evidence"]]}
        elif task == "agent_decide":
            result = {"action": "speak", "reason": "接上话题。",
                      "uses_memory": bool(packet.get("memory")), "change": None}
            if self.PROMISE in packet["new_message"]:
                result.update(action="listen", change={
                    "op": "remember", "kind": "promise", "topic": "旧钟的画", "quote": self.PROMISE,
                })
        elif task in {"agent_express", "reply"}:
            self.expressions.append((settings["bot_id"], messages[0]["content"]))
            target = packet["接这条消息"]
            text = "收到。"
            sources = [{"id": target["id"], "from": target["from"], "quote": target["content"][:8]}]
            if historical := packet.get("历史查询"):
                evidence = historical["证据"]
                if self.structured_archive and evidence:
                    text = " ".join(row["text"] for row in evidence)
                    sources.extend({"id": row["id"], "from": row["from"], "quote": row["text"]}
                                   for row in evidence)
                else:
                    text = " ".join(f"{row['text']} [M{row['id']}]" for row in evidence) or "还没查到可用的历史。"
            elif memory := packet.get("角色记忆"):
                text = f"画好啦？下次让我看看。 {memory['引用']}"
            result = {
                "address": packet["本次发言"], "reply": text, "inner_thought": None,
                "sources": sources,
                "memory_to_save": None, "persona_update": None,
            }
        else:
            self.fail(f"Unexpected model task: {task}")
        return json.dumps(result, ensure_ascii=False)

    def test_router_gives_agents_separate_directions_without_capping_their_replies(self):
        first = self.bot
        second = self.checked(self.client.post(f"{self.CONFIG}", json={
            "name": "测试陪聊者", "model_id": first["model_id"], "prompt": "照顾用户谈到的感受。",
        }))
        directions = {first["id"]: ("deep", "gentle"), second["id"]: ("standard", "proactive")}
        seen = {}
        long_reply = "\n\n".join(f"第 {i} 段，回到用户刚提到的社区访谈和感受。" for i in range(350))

        async def responses(settings, messages, **options):
            task = settings["task"]
            packet = json.loads(messages[-1]["content"])
            if task == "route":
                self.assertEqual({bot["bot_id"] for bot in packet["候选人"]}, set(directions))
                self.assertIn("听我说", packet["最新用户原话"]["content"])
                return json.dumps({
                    "intent": "emotion", "emotional_intensity": "high", "primary": [
                        {"bot_id": bot_id, "depth": depth, "engagement": engagement}
                        for bot_id, (depth, engagement) in directions.items()
                    ],
                })
            if task in {"agent_decide", "agent_express"}:
                bot_id = settings["bot_id"]
                direction = packet["response_direction" if task == "agent_decide" else "本轮路由"]
                seen[task, bot_id] = direction
                self.assertEqual((direction["depth"], direction["engagement"]), directions[bot_id])
                self.assertEqual((direction["intent"], direction["emotional_intensity"]), ("emotion", "high"))
                if task == "agent_express":
                    self.assertIsNone(options["max_tokens"])
                    self.assertIn("固定句数、字数、段落数", messages[0]["content"])
                    return json.dumps({"address": packet["本次发言"], "sources": [], "reply": long_reply})
            return await self.fake_complete(settings, messages, **options)

        with patch("thought_app.llm.complete", responses):
            session = self.session("两种回应方向")
            turn = self.checked(self.client.post(f"/api/sessions/{session['id']}/chat", json={
                "content": "聊完访谈我还是很委屈。想先有人听我说，再听一个实际办法。",
                "use_agent": True, "bot_ids": [first["id"], second["id"]],
            }))
        self.assertEqual(turn["errors"], [])
        self.assertFalse(turn["side_active"])
        self.assertEqual([msg["bot_id"] for msg in turn["messages"][1:]], [first["id"], second["id"]])
        self.assertEqual([msg["content"] for msg in turn["messages"][1:]], [long_reply, long_reply])
        self.assertEqual(set(seen), {(stage, bot_id) for stage in ("agent_decide", "agent_express") for bot_id in directions})

    def test_router_cannot_replace_mentions_and_quiet_intent_skips_expression(self):
        mode = "question"

        async def responses(settings, messages, **options):
            if settings["task"] == "route":
                packet = json.loads(messages[-1]["content"])
                self.assertTrue(packet["已点名"])
                self.assertEqual([item["bot_id"] for item in packet["候选人"]], [self.bot["id"]])
                return json.dumps({
                    "intent": mode, "emotional_intensity": [], "quiet_quote": "这条不用回复",
                    "primary": [{"bot_id": 999999, "depth": "deep", "engagement": "proactive"}],
                })
            return await self.fake_complete(settings, messages, **options)

        with patch("thought_app.llm.complete", responses):
            turn = self.send(self.session("点名保留"), "我想问一下访谈怎么做？")
            self.assertEqual([msg["bot_id"] for msg in turn["messages"][1:]], [self.bot["id"]])
            reply = next(packet for task, packet, _ in self.calls if task == "agent_express")
            self.assertEqual(reply["本轮路由"]["depth"], "standard")
            self.assertEqual(reply["本轮路由"]["emotional_intensity"], "unknown")
            mode = "note"
            self.calls.clear()
            turn = self.send(self.session("只记录"), "今天访谈结束了，这条不用回复。")
            self.assertEqual([msg["role"] for msg in turn["messages"]], ["user"])
            self.assertFalse(any(task == "agent_express" for task, _, _ in self.calls))

    def test_agent_settings_apply_to_stages_and_compilation_is_reused(self):
        initial = self.checked(self.client.get(self.CONFIG))
        self.assertTrue(initial["enabled"])
        self.assertEqual(initial["compile_thinking"], "low")
        self.assertEqual(initial["decision_thinking"], "off")
        self.assertEqual(initial["reply_thinking"], "off")
        saved = self.checked(self.client.patch(self.CONFIG, json={
            "compile_thinking": "high", "decision_thinking": "low", "reply_thinking": "max",
        }))
        self.assertEqual(saved["reply_thinking"], "max")
        self.assertEqual(self.client.patch(self.CONFIG, json={"reply_thinking": "invalid"}).status_code, 422)

        compile_path = f"{self.CONFIG}/personas/{self.persona['id']}/compile"
        built = self.checked(self.client.post(compile_path))
        self.assertEqual(built["status"], "ready")
        self.assertEqual(built["version"], 1)
        self.assertEqual(built["skills"][0]["source"], self.PERSONA)

        chat = self.session("设置")
        answer = self.send(chat, "今天聊聊窗边的光。")["messages"][1]
        self.assertEqual(answer["agent"], {"mode": "agent", "version": 1, "archive_used": False})
        self.assertEqual([(task, effort) for task, _, effort in self.calls if task.startswith("agent_")], [
            ("agent_compile", "high"), ("agent_decide", "low"), ("agent_express", "max"),
        ])

        edited = self.PERSONA + "留意细节。"
        self.checked(self.client.patch(f"/api/config/personas/{self.persona['id']}", json={
            "name": self.persona["name"], "prompt": edited,
        }))
        statuses = self.checked(self.client.get(self.CONFIG))["personas"]
        self.assertEqual(next(row for row in statuses if row["id"] == self.persona["id"])["status"], "stale")
        updated = self.send(chat, "下午光线变了。")["messages"][1]
        self.assertEqual(updated["agent"]["version"], 2)
        self.assertEqual([data["source"] for task, data, _ in self.calls if task == "agent_compile"],
                         [self.PERSONA, edited])
        self.checked(self.client.patch(self.CONFIG, json={"enabled": False}))
        ordinary = self.send(chat, "天快黑了。")["messages"][1]
        self.assertNotIn("agent", ordinary)
        self.assertEqual([task for task, _, _ in self.calls if task != "observe"][-1], "reply")

    def test_silent_private_memory_changes_a_new_session_and_rewind_retracts_reply(self):
        origin = self.session("随手画画")
        reader = self.session("另开的话题")
        self.assertFalse(origin["archive_share_enabled"])
        source_turn = self.send(origin, self.PROMISE + "，先不用回。")
        self.assertEqual([message["role"] for message in source_turn["messages"]], ["user"])
        self.assertFalse(source_turn["errors"])
        source = source_turn["messages"][0]
        self.assertFalse(any(task == "agent_express" for task, _, _ in self.calls))

        answer = self.send(reader, "画完了。")["messages"][1]
        recalled = next(data for task, data, _ in self.calls if task == "agent_recall")
        self.assertEqual(recalled["short_term"], [])
        self.assertEqual(recalled["candidates"][0]["source_id"], source["id"])
        decision = [data for task, data, _ in self.calls if task == "agent_decide"][-1]
        self.assertEqual(decision["memory"]["id"], recalled["candidates"][0]["id"])
        self.assertIn(f"[M{source['id']}]", answer["content"])
        self.assertFalse(answer["agent"]["archive_used"])
        self.assertEqual({ref["message_id"] for ref in answer["references"]}, {source["id"]})

        self.checked(self.client.post(f"/api/sessions/{origin['id']}/rewind", json={"message_id": source["id"]}))
        self.assertEqual([row["role"] for row in self.detail(reader)["messages"]], ["user"])
        recall_calls = sum(task == "agent_recall" for task, _, _ in self.calls)
        fresh = self.send(self.session("第三场"), "画完了。")["messages"][1]
        self.assertEqual(sum(task == "agent_recall" for task, _, _ in self.calls), recall_calls)
        self.assertEqual(fresh["references"], [])

    def test_archive_opt_in_summary_citations_and_source_retraction(self):
        archive = self.session("旧电影院札记")
        private = self.session("未共享的电影院想法")
        probe = self.session("回看")
        first = self.send(archive, "想拆旧电影院，因为消防通道太窄。", mention=False)["messages"][0]
        second = self.send(archive, "看完改造图后，想保留旧电影院门厅，增加消防出口。", mention=False)["messages"][0]
        self.send(private, "私下想把旧电影院用作布景仓库。", mention=False)
        denied = self.send(probe, self.REVIEW)["messages"][1]
        self.assertEqual(denied["references"], [])
        self.assertFalse(denied["agent"]["archive_used"])

        share_path = f"/api/sessions/{archive['id']}"
        shared = self.checked(self.client.patch(share_path, json={"archive_share_enabled": True}))
        self.assertTrue(shared["archive_share_enabled"])
        self.assertEqual(shared["summary_share_enabled"], archive["summary_share_enabled"])
        self.structured_archive = True
        answered = self.send(probe, self.REVIEW)
        question, reply = answered["messages"]
        evidence = [row["id"] for stage, packet, _ in self.calls if stage == "agent_archive_summarize"
                    for row in packet["evidence"]]
        self.assertEqual(evidence, [first["id"], second["id"]])
        self.assertEqual({item["message_id"] for item in reply["references"]}, set(evidence))
        self.assertTrue(reply["agent"]["archive_used"])
        self.assertNotIn("[M", reply["content"])
        self.assertEqual(self.detail(probe)["messages"][-1]["agent"], reply["agent"])

        self.checked(self.client.patch(share_path, json={"archive_share_enabled": False}))
        self.assertNotIn(reply["id"], {item["id"] for item in self.detail(probe)["messages"]})
        self.checked(self.client.patch(share_path, json={"archive_share_enabled": True}))
        self.assertIn(reply["id"], {item["id"] for item in self.detail(probe)["messages"]})
        self.checked(self.client.post(f"{share_path}/rewind", json={"message_id": second["id"]}))
        remaining = {item["id"] for item in self.detail(probe)["messages"]}
        self.assertIn(question["id"], remaining)
        self.assertNotIn(reply["id"], remaining)

    def test_backup_restores_agent_configuration_and_memory(self):
        origin = self.session("原话")
        self.checked(self.client.patch(f"/api/sessions/{origin['id']}", json={"archive_share_enabled": True}))
        self.checked(self.client.patch(self.CONFIG, json={"reply_thinking": "high"}))
        self.checked(self.client.patch(f"{self.CONFIG}/{self.bot['id']}", json={"thinking_effort": "low"}))
        source = self.send(origin, self.PROMISE + "，先不用回。")["messages"][0]
        archive = self.client.get("/api/backup/export")
        self.assertTrue(archive.is_success)
        self.checked(self.client.patch(self.CONFIG, json={"reply_thinking": "off"}))
        self.checked(self.client.delete(f"/api/sessions/{origin['id']}"))
        self.checked(self.client.post("/api/backup/import", content=archive.content,
                                      headers={"Content-Type": "application/zip"}))
        self.assertEqual(self.checked(self.client.get(self.CONFIG))["reply_thinking"], "high")
        agents = self.checked(self.client.get(self.CONFIG))["agents"]
        self.assertEqual(next(item for item in agents if item["id"] == self.bot["id"])["thinking_effort"], "low")
        self.assertTrue(self.detail(origin)["session"]["archive_share_enabled"])
        answer = self.send(self.session("回来聊"), "画完了。")["messages"][1]
        self.assertEqual({ref["message_id"] for ref in answer["references"]}, {source["id"]})

    def test_manual_side_invite_uses_the_agent_path(self):
        chat = self.session("侧栏")
        user = self.send(chat, "今天聊聊窗边的光。")["messages"][0]
        self.checked(self.client.post(f"/api/sessions/{chat['id']}/side", json={
            "bot_id": self.bot["id"], "message_id": user["id"], "use_agent": True,
        }))
        deadline = time.monotonic() + 2
        while (detail := self.detail(chat))["side_active"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(detail["side_active"])
        self.assertIsNone(detail["side_error"])
        reply = next(message for message in detail["messages"] if message["channel"] == "aside")
        self.assertEqual(reply["reply_to"], user["id"])
        self.assertEqual(reply["agent"]["mode"], "agent")

    def test_learned_preference_changes_agent_stages_across_sessions_and_rewinds(self):
        path = f"/api/config/bots/{self.bot['id']}/evolution"
        initial = "观鸟时，我偏向先听声音。"
        revised = "现在我聊观鸟，喜欢把听见的节奏说出来。"
        decision_skill = {"name": "先听声音", "phase": "decision", "instruction": "聊到观鸟，优先接住声音的细节。"}
        express_skill = {"name": "描述节奏", "phase": "express", "instruction": "对方提到观鸟时，用节奏来形容鸟鸣。"}
        learned_reason = "先接住鸟鸣的声音。"
        learned_reply = "像两短一长的鼓点。"

        async def model(settings, messages, **options):
            result = json.loads(await self.fake_complete(settings, messages, **options))
            packet = json.loads(messages[-1]["content"])
            if settings["task"] == "agent_decide":
                rules = packet["persona_phases"]["decision"]
                if any(rule.get("origin") == "evolution" and rule["instruction"] == decision_skill["instruction"]
                       for rule in rules):
                    result["reason"] = learned_reason
            elif settings["task"] == "agent_express":
                question = packet["接这条消息"]["content"]
                preferences = packet.get("本轮人格偏好", [])
                for skill in (decision_skill, express_skill):
                    self.assertNotIn(skill["instruction"], messages[0]["content"])
                if "第一次观鸟" in question:
                    result.update(reply=initial, persona_update={
                        "kind": "view", "text": initial, "evidence": initial,
                        "keywords": ["观鸟"], "skill": decision_skill,
                    })
                elif "换个做法" in question:
                    self.assertEqual(preferences[0]["skill"], decision_skill)
                    result.update(reply=revised, persona_update={
                        "kind": "view", "text": revised, "evidence": revised, "keywords": ["观鸟"],
                        "replaces_id": preferences[0]["id"], "skill": express_skill,
                    })
                elif any(item["skill"] == express_skill for item in preferences):
                    result["reply"] = learned_reply
                elif packet.get("可选的话头") == learned_reason:
                    self.assertEqual([item["text"] for item in preferences], [initial])
                    result["reply"] = learned_reason
            return json.dumps(result, ensure_ascii=False)

        def answer(text):
            turn = self.send(self.session(text), text)
            self.assertEqual(turn["errors"], [])
            return turn["messages"][-1]["content"]

        with patch("thought_app.llm.complete", model):
            self.assertEqual(answer("第一次观鸟，你看重什么？"), initial)
            self.assertEqual(answer("早上想喝茶。"), "收到。")
            self.assertEqual(answer("观鸟有哪些细节？"), learned_reason)
            answer("翻一下之前的聊天，我对观鸟是什么看法？")
            archived = next(packet for task, packet, _ in reversed(self.calls) if task == "agent_express")
            self.assertIn("历史查询", archived)
            self.assertNotIn("本轮人格偏好", archived)
            revision_session = self.session("新的观鸟习惯")
            change = self.send(revision_session, "观鸟时你想换个做法吗？")
            self.assertEqual(change["messages"][-1]["content"], revised)
            self.assertEqual(answer("你会怎么描述观鸟时的体验？"), learned_reply)

            current = self.checked(self.client.get(path + "/path"))
            self.assertEqual([row["description"] for row in current["traits"]], [revised])
            self.assertEqual(current["traits"][0]["phase"], "express")
            self.checked(self.client.post(f"/api/sessions/{revision_session['id']}/rewind", json={
                "message_id": change["messages"][0]["id"],
            }))
            self.assertEqual(answer("现在观鸟看重什么？"), learned_reason)
            restored = self.checked(self.client.get(path + "/path"))["traits"]
            self.assertEqual([row["description"] for row in restored], [initial])
            self.checked(self.client.delete(f"{path}/{restored[0]['note_id']}"))
            self.assertEqual(answer("现在观鸟看重什么？"), "收到。")
            self.assertEqual(sum(task == "agent_compile" for task, _, _ in self.calls), 1)

    def test_inflight_reply_cannot_publish_an_evolution_after_author_rewrite(self):
        from thought_app.database import connect

        async def rewrite_while_generating(settings, messages, **options):
            result = json.loads(await self.fake_complete(settings, messages, **options))
            if settings["task"] == "agent_express":
                with connect() as db:
                    db.execute("UPDATE personas SET prompt = ? WHERE id = ?", ("新的作者设定。", self.persona["id"]))
                evidence = "我喜欢先看看云的方向。"
                result.update(reply=evidence, persona_update={
                    "kind": "habit", "text": evidence, "evidence": evidence, "keywords": ["云层"],
                    "skill": {"phase": "attention", "name": "留意云层", "instruction": "先观察云层方向。"},
                })
            return json.dumps(result, ensure_ascii=False)

        with patch("thought_app.llm.complete", rewrite_while_generating):
            outcome = self.send(self.session("作者改写"), "云会往哪边走？")
        self.assertEqual([row["role"] for row in outcome["messages"]], ["user"])
        path = self.checked(self.client.get(f"/api/config/bots/{self.bot['id']}/evolution/path"))
        self.assertEqual(path["events"], [])

    def test_poe_experts_recover_from_incomplete_decisions_in_chat_and_aside(self):
        from thought_app.llm import ModelError

        provider = self.checked(self.client.post("/api/config/providers", json={
            "name": "测试 Poe", "kind": "poe", "base_url": "https://poe.test/v1",
            "api_key": "synthetic-poe-agent-key",
        }))
        model = self.checked(self.client.post("/api/config/models", json={
            "name": "测试 Gemini Flash", "provider_id": provider["id"], "model_code": "gemini-3.8-flash",
        }))
        experts = [agent for agent in self.checked(self.client.get(self.CONFIG))["agents"]
                   if agent["name"].endswith("专家")]
        self.assertEqual(len(experts), 2)
        for expert in experts:
            self.checked(self.client.patch(f"{self.CONFIG}/{expert['id']}", json={"model_id": model["id"]}))
        main_expert, side_expert = experts
        decisions = {expert["id"]: [] for expert in experts}

        async def incomplete_first_decision(settings, messages, **options):
            task = settings["task"]
            if task.startswith("agent_"):
                self.assertEqual(settings["kind"], "poe")
                self.assertFalse(options["json_output"])
                self.assertTrue(options["require_complete"])
                self.assertIsNone(options["max_tokens"])
            if task == "agent_decide":
                bot_id = settings["bot_id"]
                attempts = decisions[bot_id]
                attempts.append(messages[0]["content"])
                if len(attempts) == 1:
                    if bot_id == main_expert["id"]:
                        return '{"action":"speak","reason":"输出被截断'
                    raise ModelError("模型输出超出长度限制。")
            return await self.fake_complete(settings, messages, **options)

        with patch("thought_app.llm.complete", incomplete_first_decision):
            session = self.session("专家接话")
            path = f"/api/sessions/{session['id']}"
            turn = self.checked(self.client.post(path + "/chat", json={
                "content": f"@{main_expert['name']} 这个现象可以怎么解释？",
                "use_agent": True,
            }))
            self.assertEqual(turn["errors"], [])
            question, answer = turn["messages"]
            self.assertEqual(answer["bot_id"], main_expert["id"])
            self.assertEqual(answer["agent"]["mode"], "agent")
            self.checked(self.client.post(path + "/side", json={
                "bot_id": side_expert["id"], "message_id": answer["id"], "use_agent": True,
            }))
            deadline = time.monotonic() + 2
            while (detail := self.detail(session))["side_active"] and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(detail["side_active"])
        self.assertIsNone(detail["side_error"])
        self.assertEqual(len(detail["messages"]), 3)
        self.assertEqual([row["id"] for row in detail["messages"][:2]], [question["id"], answer["id"]])
        aside = detail["messages"][-1]
        self.assertEqual((aside["channel"], aside["bot_id"]), ("aside", side_expert["id"]))
        self.assertEqual((aside["turn_id"], aside["reply_to"]), (question["id"], answer["id"]))
        self.assertEqual(aside["agent"]["mode"], "agent")
        for attempts in decisions.values():
            self.assertEqual(len(attempts), 2)
            self.assertIn("重新生成一个完整 JSON 对象", attempts[1])

    def test_agent_creation_owns_persona_and_applies_thinking_after_recompile(self):
        new = self.checked(self.client.post(self.CONFIG, json={
            "name": "临时分析员", "model_id": self.bot["model_id"],
            "thinking_effort": "high", "prompt": "用可证伪的解释回答。",
        }))
        self.assertNotEqual(new["persona_id"], self.persona["id"])
        self.assertTrue(new["thinking_supported"])
        path = f"{self.CONFIG}/{new['id']}"
        first = self.checked(self.client.post(path + "/compile"))
        self.checked(self.client.patch(path, json={"prompt": "对照样本后再给结论。"}))
        configuration = self.checked(self.client.get(self.CONFIG))
        self.assertEqual(len(configuration["starters"]), 7)
        status = next(row for row in configuration["personas"] if row["id"] == new["persona_id"])
        self.assertEqual(status["status"], "stale")
        self.assertEqual(self.checked(self.client.post(path + "/compile"))["version"], first["version"] + 1)
        session = self.session("有新证据")
        result = self.checked(self.client.post(f"/api/sessions/{session['id']}/chat", json={
            "content": "@临时分析员 这说明什么？", "use_agent": True,
        }))
        self.assertEqual(result["messages"][1]["agent"]["version"], 2)
        self.assertIn("交代依据和不确定的部分。", self.expressions[-1][1])
        self.assertEqual([(task, effort) for task, _, effort in self.calls if task.startswith("agent_")],
                         [("agent_compile", "low"), ("agent_compile", "low"),
                          ("agent_decide", "high"), ("agent_express", "high")])

    def test_editing_legacy_shared_persona_only_changes_selected_agent(self):
        original = self.checked(self.client.post("/api/config/bots", json={
            "name": "旧版共用角色", "persona_id": self.persona["id"], "model_id": self.bot["model_id"],
        }))
        updated = self.checked(self.client.patch(f"{self.CONFIG}/{self.bot['id']}",
                                                 json={"prompt": "改写才会看到的独立人格。"}))
        self.assertNotEqual(updated["persona_id"], original["persona_id"])
        roster = self.checked(self.client.get(self.CONFIG))["agents"]
        alias = next(row for row in roster if row["id"] == original["id"])
        self.assertEqual(alias["prompt"], self.PERSONA)
        self.checked(self.client.delete(f"{self.CONFIG}/{updated['id']}"))
        self.assertIn(original["id"], [row["id"] for row in self.checked(self.client.get(self.CONFIG))["agents"]])

    def test_mirror_uses_compiled_persona_with_user_only_history(self):
        session = self.session("用户自己的原话")
        self.send(session, "下雨以后我更容易安静下来。")
        result = self.checked(self.client.post(f"/api/sessions/{session['id']}/chat", json={
            "content": "@影子自我 用我的话试写一句。", "use_agent": True,
        }))
        self.assertEqual(result["messages"][1]["agent"]["mode"], "agent")
        mirror_call = [packet for task, packet, _ in self.calls if task == "agent_decide"][-1]
        self.assertTrue(mirror_call["short_term"])
        self.assertTrue(all(message["from"] == "user" for message in mirror_call["short_term"]))
        self.assertEqual(mirror_call["writable_memory_types"], [])
        self.assertNotIn("archive_instructions", mirror_call)
        self.assertIn("交代依据和不确定的部分。", self.expressions[-1][1])


if __name__ == "__main__":
    unittest.main()
