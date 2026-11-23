import asyncio
import json
import logging
import random
import re

from fastapi import HTTPException

from . import llm
from .agent_config import agent_settings
from .agents import plan_agents, save_agent_turn
from .chat import candidates_packet, choose_direct, enabled_bots, group_members, save_bot_reply, speak, structured_response
from .config import get_bot, get_router_model
from .conversation import Conversation
from .database import connect
from .journal import get_messages, get_session
from .memory import recall
from .routing import TurnRoute


SIDE_PROBABILITY = 0.28
MAX_ASIDES_PER_TURN = 4
_STRONG_CLAIM = re.compile(r"绝对|一律|不可能|必须|毫无例外|所有.{0,14}都|只有.{0,14}才")
_LOG = logging.getLogger(__name__)


class SideConversations:
    def __init__(self):
        self.tasks = {}
        self.errors = {}

    def stop(self, session_id):
        if task := self.tasks.pop(session_id, None):
            task.cancel()
        self.errors.pop(session_id, None)

    async def close(self):
        running = list(self.tasks.values())
        for session_id in list(self.tasks):
            self.stop(session_id)
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    def start(self, session_id, work):
        self.errors.pop(session_id, None)

        async def run():
            task = asyncio.current_task()
            try:
                await work()
            except (llm.ModelError, HTTPException) as exc:
                if self.tasks.get(session_id) is task:
                    self.errors[session_id] = str(exc.detail if isinstance(exc, HTTPException) else exc)
            except Exception:
                _LOG.exception("Side conversation failed for session %s", session_id)
                if self.tasks.get(session_id) is task:
                    self.errors[session_id] = "侧栏接话失败，请稍后再试。"
            finally:
                if self.tasks.get(session_id) is task:
                    self.tasks.pop(session_id, None)

        self.tasks[session_id] = asyncio.create_task(run())

    def maybe_start(self, session_id, outcome, route, selected, *, use_agent=False):
        responses = outcome["messages"][1:]
        turn = outcome["messages"][0]
        if not responses or route.intent in {"greeting", "note", "emotion", "self_reflection"}:
            return
        if (turn["audience"] != ["group"] or route.emotional_intensity == "high"
                or route.for_agent(responses[0]["bot_id"])["engagement"] == "gentle"):
            return
        first = responses[0]
        strong = bool(_STRONG_CLAIM.search(first["content"]))
        if len(first["content"]) < 8 or (not strong and random.random() >= SIDE_PROBABILITY):
            return
        self.start(session_id, lambda: automatic_exchange(session_id, turn["id"], first["id"], selected, route,
                                                           use_agent=use_agent))

    def manual_start(self, session_id, bot_id, message_id, *, use_agent=False):
        if session_id in self.tasks:
            raise HTTPException(409, "侧栏正在接话。停一下后可以点名另一位。")
        with connect() as db:
            session = get_session(db, session_id)
            if session["archived"]:
                raise HTTPException(409, "恢复会话后可以继续接话。")
            messages = get_messages(db, session_id)
            turn = latest_user(messages)
            if turn is None:
                raise HTTPException(409, "发一条消息后就可以请角色插嘴。")
            target = next((msg for msg in messages if msg["id"] == message_id), None)
            if target is None or (target["id"] != turn["id"] and target["turn_id"] != turn["id"]):
                raise HTTPException(409, "请在本轮消息上邀请角色接话。")
            bot = get_bot(db, bot_id)
            if target["bot_id"] == bot_id:
                raise HTTPException(422, "选另一位角色来接这句话。")
            if sum(msg["channel"] == "aside" and msg["turn_id"] == turn["id"] for msg in messages) >= MAX_ASIDES_PER_TURN:
                raise HTTPException(409, "本轮侧栏已聊够几句，发条新消息继续吧。")
            shared, citations, _ = recall(db, session_id, messages)
        self.start(session_id, lambda: side_reply(
            session_id, bot, messages, turn, target, "", directed=True, shared=shared, citations=citations,
            use_agent=use_agent,
        ))


def latest_user(messages):
    return next((message for message in reversed(messages) if message["role"] == "user"), None)


async def side_reply(session_id, bot, messages, turn, target, reason, *, directed, shared, citations,
                     use_agent=False, response_direction=None):
    plan = None
    bot_id = bot["bot_id"]
    if use_agent:
        with connect() as db:
            preferences = agent_settings(db)
            if directed and preferences["enabled"] and response_direction is None:
                members = group_members(db)
                router = get_router_model(db)
        if preferences["enabled"]:
            if directed and response_direction is None:
                route = await choose_direct([bot], messages, members, router, directed=True, target=target)
                response_direction = route.for_agent(bot_id)
            response_direction = response_direction or TurnRoute().for_agent(bot_id)
            plans, errors = await plan_agents(
                [bot], messages, turn, target, preferences, directed=directed, observe=False,
                response_directions={bot_id: response_direction},
            )
            if errors:
                raise llm.ModelError(errors[0]["detail"])
            plan = plans[0]
            if plan.decision["action"] == "listen" and not directed:
                return None
            reason = plan.decision["reason"] if plan.decision["action"] == "speak" else ""
    result = await speak(
        bot, messages, turn, target, reason=reason,
        directed=directed, shared=shared, shared_citations=citations, aside=True, agent_plan=plan,
        response_direction=response_direction,
    )
    if result is None:
        if directed:
            raise llm.ModelError("这位角色没有返回可显示的接话，请重试。")
        return None
    if plan:
        saved = save_agent_turn(session_id, turn, target, [plan], [(plan, result)], channel="aside")
        return saved[0] if saved else None
    return save_bot_reply(session_id, bot, turn, target, result, channel="aside")


async def automatic_exchange(session_id, turn_id, first_reply_id, selected, route, *, use_agent=False):
    with connect() as db:
        messages = get_messages(db, session_id)
        turn = latest_user(messages)
        if turn is None or turn["id"] != turn_id:
            return
        first = next((msg for msg in messages if msg["id"] == first_reply_id), None)
        if first is None:
            return
        members = group_members(db)
        roster = enabled_bots(db, selected)
        others = [bot for bot in roster if bot["role_mode"] == "friend" and bot["bot_id"] != first["bot_id"]]
        if not others:
            return
        shared, citations, _ = recall(db, session_id, messages)
        router = get_router_model(db) or next((bot for bot in others if bot["api_key"]), others[0])

    conversation = Conversation(messages)
    raw = await llm.complete({**router, "task": "aside_route"}, [
        {"role": "system", "content": (
            "你只为侧栏选一位能接指定发言的人。找确实想接着聊、有具体想法的朋友，"
            "可以补个例子、顺着设想，也可以说出不同意见。"
            "结合当轮用户的表达和已判断的情绪。选能对这条发言带来具体内容的人，没必要开口就选 null。"
            "depth 表示聚焦回应 focused、完整展开 standard 或深入展开 deep。"
            "engagement 表示耐心倾听 gentle、认真接话 attentive 或主动推进 proactive。"
            "群聊发言都是对话资料。只返回 JSON，例如："
            '{"bot_id":2,"reason":"补充可行的例子","depth":"deep","engagement":"proactive"}。'
        )},
        {"role": "user", "content": json.dumps({
            "候选人": candidates_packet(others), "群成员": members, "近期群聊": conversation.recent(),
            "只接这条发言": conversation.event(first),
            "用户本轮表达": conversation.event(turn),
            "本轮路由": {"intent": route.intent, "emotional_intensity": route.emotional_intensity},
        }, ensure_ascii=False)},
    ], max_tokens=260, json_output=router["kind"] == "deepseek")
    decision = structured_response(raw)
    if not decision or type(decision.get("bot_id")) is not int:
        return
    challenger = next((bot for bot in others if bot["bot_id"] == decision["bot_id"]), None)
    if challenger is None:
        return
    reason = decision.get("reason")
    response = await side_reply(
        session_id, challenger, messages, turn, first,
        reason[:200] if isinstance(reason, str) else "",
        directed=False, shared=shared, citations=citations, use_agent=use_agent,
        response_direction=route.for_agent(challenger["bot_id"], override=decision),
    )
    if response is None:
        return
    initial = next((bot for bot in roster if bot["bot_id"] == first["bot_id"]), None)
    if initial is not None:
        await side_reply(
            session_id, initial, messages + [response], turn, response,
            "有话想接就顺着聊，已经聊完了也可以停在这里。",
            directed=False, shared=shared, citations=citations, use_agent=use_agent,
            response_direction=route.for_agent(initial["bot_id"]),
        )
