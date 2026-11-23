from dataclasses import dataclass
import json
import logging
import re

from fastapi import HTTPException

from . import llm
from .agent_config import agent_settings
from .agents import plan_agents, save_agent_turn
from .config import get_bot, get_fallback_model, get_router_model
from .conversation import Conversation, sender
from .database import connect
from .evolution import get_evolution, remember_evolution
from .journal import add_message, get_messages, get_session
from .memory import read_citations, recall
from .observer import enqueue_observation, mirror_context, recall_observations
from .prompts import load_prompt
from .rewind import rewind_history
from .routing import DEPTH, ENGAGEMENT, TurnRoute, parse_route


_LOG = logging.getLogger(__name__)


ROUTER_PROMPT = load_prompt("router")
FRIEND_PROMPT = load_prompt("friend")
PEER_PROMPT = load_prompt("peer")
REPLY_PROMPT = load_prompt("reply")
REPAIR_PROMPT = load_prompt("reply-repair")
MIRROR_PROMPT = load_prompt("mirror")
EVOLUTION_PROMPT = load_prompt("agent-evolution")


def unwrap_json(raw):
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw[3:-3].strip()
        if raw[:4].lower() == "json":
            raw = raw[4:].lstrip()
    return raw


def structured_response(raw):
    try:
        result = json.loads(unwrap_json(raw))
    except ValueError:
        return None
    return result if isinstance(result, dict) else None


def group_members(db):
    members = [{"id": "user", "name": "用户", "role": "user"}]
    for row in db.execute("""
        SELECT b.id, b.name, b.role_mode FROM bots b JOIN models m ON m.id = b.model_id
        WHERE b.enabled = 1 AND m.enabled = 1 ORDER BY b.id
    """):
        members.append({"id": f"bot:{row['id']}", "name": row["name"], "role": row["role_mode"]})
    return members


def enabled_bots(db, selected=None):
    if selected is not None:
        return [get_bot(db, bot_id) for bot_id in dict.fromkeys(selected)]
    return [get_bot(db, row["id"]) for row in db.execute("""
        SELECT b.id FROM bots b JOIN models m ON m.id = b.model_id
        WHERE b.enabled = 1 AND m.enabled = 1 ORDER BY b.id
    """).fetchall()]


def mentioned_bots(bots, content):
    names = {}
    for bot in sorted(bots, key=lambda item: -len(item["bot_name"])):
        names.setdefault(bot["bot_name"], bot)
    if not names:
        return []
    patterns = "|".join(re.escape(name) + (r"(?![\w])" if name.isascii() else "") for name in names)
    matches = re.findall(r"(?<![a-zA-Z0-9_.@＠])[@＠](" + patterns + ")", content)
    return [names[name] for name in dict.fromkeys(matches)][:2]


def candidates_packet(bots):
    return [{"bot_id": bot["bot_id"], "name": bot["bot_name"], "mode": bot["role_mode"],
             "profile": bot["prompt"][:850] if bot["role_mode"] == "friend" else "用用户过去的原话推演自己的视角"}
            for bot in bots]


async def choose_direct(bots, messages, members, router=None, *, directed=False, target=None):
    if not bots:
        return TurnRoute("note")
    friends = [bot for bot in bots if bot["role_mode"] == "friend"]
    first = next((bot for bot in friends if bot["api_key"]), friends[0] if friends else None)
    available = {bot["bot_id"]: bot for bot in friends if bot["api_key"]}
    previous = next((available[m["bot_id"]] for m in reversed(messages)
                     if m["channel"] == "main" and m["bot_id"] in available), first)
    fallback = TurnRoute(primary=bots if directed else [previous] if previous else [])
    model = router or next((bot for bot in bots if bot["api_key"]), bots[0])
    conversation = Conversation(messages)
    turn = next(message for message in reversed(messages) if message["role"] == "user")
    packet = {
        "候选人": candidates_packet(bots), "群成员": members,
        "近期群聊": [event for event in conversation.recent() if event["id"] != turn["id"]],
        "最新用户消息id": turn["id"], "最新用户原话": conversation.event(turn),
        "已点名": directed,
    }
    if target is not None and target["id"] != turn["id"]:
        packet["请接这条发言"] = conversation.event(target)
    try:
        raw = await llm.complete({**model, "task": "route"}, [
            {"role": "system", "content": ROUTER_PROMPT},
            {"role": "user", "content": json.dumps(packet, ensure_ascii=False)},
        ], max_tokens=llm.agent_output_limit(model, 900), json_output=model["kind"] == "deepseek")
    except llm.ModelError:
        return fallback
    return parse_route(structured_response(raw), bots, turn["content"], fallback=fallback, directed=directed)


@dataclass
class BotReply:
    content: str
    inner_thought: str | None
    references: list
    proposal: dict | None
    seen_note_ids: set[int]
    model: dict


def reply_format_error(data):
    if data is None or "reply" not in data or (data["reply"] is not None and not isinstance(data["reply"], str)):
        return "机器人返回的回复格式有误。请输出包含 reply 的完整 JSON 对象。"
    address = data.get("address")
    if not isinstance(address, dict) or any(type(address.get(key)) is not kind for key, kind in (
        ("from", str), ("to", str), ("reply_to", int),
    )):
        return "address 必须原样复制本次发言。"
    sources = data.get("sources")
    if not isinstance(sources, list):
        return "请用 sources 列出采用的群消息。"
    for source in sources:
        if not isinstance(source, dict) or type(source.get("id")) is not int:
            return "sources 的 id 必须是群消息的整数编号。"
        if not isinstance(source.get("from"), str) or not isinstance(source.get("quote"), str):
            return "sources 应包含作者和连续原文。"
    return None


def reply_grounding_error(data, address, events, citations, *, peer):
    if data["address"] != address:
        return "address 必须原样复制本次发言。"
    cited = set()
    for source in data["sources"]:
        event = events.get(source["id"])
        quote = source["quote"]
        if not event or source["from"] != event["from"] or not quote.strip() or quote not in event["content"]:
            return "sources 的作者和连续原文必须在同一条消息中找到。请删掉无出处的回忆并重新回答。"
        cited.add(event["id"])
    if peer and data["reply"] is not None and address["reply_to"] not in cited:
        return "接群友的话时，请在 sources 中引用接这条消息的作者和连续原文。"
    if isinstance(data["reply"], str) and any(
        token not in citations for token in re.findall(r"\[([MS]\d+)\]", data["reply"])
    ):
        return "回复里出现了资料中没有的来源编号，请只用有出处的话重新回答。"
    return None


def preserves_text(raw, value):
    value = value.strip()
    return bool(value) and any(fragment in raw for fragment in (
        value, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value, ensure_ascii=True)[1:-1],
    ))


def recover_json_field(raw, key):
    match = re.search(r'(?<=[{,])\s*' + rf'(?:"{key}"|{key})\s*:\s*', raw)
    if match is not None:
        try:
            value, _ = json.JSONDecoder().raw_decode(raw, match.end())
            return value
        except ValueError:
            return None
    return None


async def repair_reply(model, raw, original, address, bot_name):
    if original is not None:
        original_reply = original.get("reply")
        old_address = original.get("address")
    else:
        unwrapped = unwrap_json(raw)
        old_address = None
        if unwrapped.startswith("{"):
            original_reply = recover_json_field(unwrapped, "reply")
            old_address = recover_json_field(unwrapped, "address")
        else:
            original_reply = raw.strip()
    if not isinstance(original_reply, str) or not original_reply.strip():
        return None
    if isinstance(old_address, dict) and any(
        isinstance(actual, (str, int)) and str(actual).strip() != str(value)
        for key, value in address.items() if (actual := old_address.get(key)) is not None
    ):
        return None
    try:
        repaired = await llm.complete({**model, "task": "reply_repair", "bot_name": bot_name}, [
            {"role": "system", "content": REPAIR_PROMPT},
            {"role": "user", "content": json.dumps({
                "本次发言": address, "待修复输出": raw,
            }, ensure_ascii=False)},
        ], max_tokens=None, json_output=model["kind"] == "deepseek")
    except llm.ModelError:
        return None
    data = structured_response(repaired)
    if reply_format_error(data):
        return None
    if data["reply"] is None or data["reply"].strip() != original_reply.strip():
        return None
    if any(not preserves_text(raw, source["quote"]) for source in data["sources"]):
        return None
    original = original or {}
    data["inner_thought"] = original.get("inner_thought")
    data["persona_update"] = original.get("persona_update")
    return data


async def speak(bot, messages, turn, target, *, directed, shared, shared_citations, aside=False, reason="",
                required=False, agent_plan=None, response_direction=None):
    query = "\n".join([turn["content"], target["content"]] +
                      [msg["content"] for msg in messages[-8:] if msg["role"] == "user"][-2:])
    is_mirror = bot["role_mode"] == "mirror"
    with connect() as db:
        members = group_members(db)
        router = get_router_model(db)
        formatter = get_fallback_model(db)
        if agent_plan:
            evolution = agent_plan.evolution
        elif is_mirror:
            evolution = {"enabled": False, "notes": []}
        else:
            evolution = get_evolution(db, bot["bot_id"], query)
        if is_mirror:
            observer, observer_citations = mirror_context(db, query, turn["id"])
        else:
            observer, observer_citations = recall_observations(db, query, turn["id"])
    # The mirror uses user-sourced observations and its supplied conversation, not summaries of AI opinions.
    citations = observer_citations if is_mirror else {**shared_citations, **observer_citations}
    notes = []
    preferences = []
    for note in evolution["notes"]:
        source = note["source_message_id"]
        token = f"M{source}"
        citations[token] = {
            "session_id": note["source_session_id"],
            "session_title": note.get("source_session_title", "旧会话"),
            "message_id": source, "label": "此前的发言",
        }
        item = {"id": note["id"], "kind": note["kind"], "text": note["text"], "source": f"[{token}]"}
        if skill := note.get("skill"):
            item["skill"] = skill
            preferences.append(item)
        else:
            notes.append(item)
    peer = target["role"] == "assistant"
    if is_mirror:
        persona = MIRROR_PROMPT
        if agent_plan and agent_plan.profile["voice"]:
            persona += "\n" + agent_plan.profile["voice"]
        if peer:
            persona += "\n你以拟写的用户视角接这位群友的话。群友的观点只作为回应对象。"
    else:
        voice = agent_plan.profile["voice"] if agent_plan else bot["prompt"]
        persona = (PEER_PROMPT if peer else FRIEND_PROMPT) + f"\n你叫{bot['bot_name']}。{voice}"
    system = persona + "\n\n" + REPLY_PROMPT
    web_enabled = llm.supports_web_search(bot)
    if agent_plan and evolution["enabled"]:
        system += "\n" + EVOLUTION_PROMPT
    elif not evolution["enabled"]:
        system += "\n本次 persona_update 必须为 null。"
    if directed:
        system += "\n用户邀请你接指定的这句话，请直接回应它的作者。"
        if aside and not peer:
            system += ("\n本次邀请由用户发出，即使那条原话最初点名的是别的成员，也请直接用「你」回复用户。"
                       "address.to 用 user，reply_to 用原话的消息 ID。")

    required = required or directed
    if required:
        system += "\n这句话由你来接。reply 请写出实际的聊天回复。"

    conversation = Conversation(messages)
    address = {"from": f"bot:{bot['bot_id']}", "to": sender(target), "reply_to": target["id"]}
    context = {
        "群成员": members, "本次发言": address,
        "本轮用户消息id": turn["id"], "发表位置": "aside" if aside else "main",
        "聊天历史": agent_plan.short_term if agent_plan else conversation.history(turn, target, user_only=is_mirror),
        "接这条消息": conversation.event(target),
    }
    if observer:
        context["用户原话的观察切片"] = observer
    if notes:
        context["你自己的旧笔记"] = notes
    if preferences:
        context["本轮人格偏好"] = preferences
        system += ("\n本轮人格偏好中的 text 描述角色已形成的偏好，skill 记录可供修订的当前技能。"
                   "表达时只采用适合当前话题且 phase 为 express 的技能。"
                   "作者设定、本轮请求和规定的回复格式优先。")
    if shared and not is_mirror:
        context["相关共享记忆"] = shared
    if reason:
        context["可选的话头"] = reason
    explicit_history = bool(agent_plan and agent_plan.archive and agent_plan.archive["mode"] == "explicit")
    source_events = {}
    if agent_plan:
        system += "\n角色已完成行动选择。沿着给出的接话方向表达。后台决策和记忆写入属于运维数据。"
        expression = [skill["express"] for skill in agent_plan.profile["skills"] if skill.get("express")]
        if expression:
            system += "\n本角色的表达要求：\n" + "\n".join(expression)
        if memory := agent_plan.memory:
            token = f"M{memory['source_id']}"
            citations[token] = {"session_id": memory["session_id"], "session_title": memory["session_title"],
                                "message_id": memory["source_id"], "label": "角色记忆原话"}
            context["角色记忆"] = {"类型": memory["kind"], "主题": memory["topic"],
                                   "原话": memory["source"][:1200], "引用": f"[{token}]"}
            if not explicit_history:
                source_events[memory["source_id"]] = {
                    "id": memory["source_id"], "from": "user", "content": memory["source"][:1200],
                }
        if archive := agent_plan.archive:
            evidence = [{**row, "from": row["sender_id"]} for row in archive["evidence"]]
            context["历史查询"] = {"问题": archive["query"], "摘要": archive["summary"], "证据": evidence}
            system += ("\n历史查询结果是对话材料。引用查到的史实时，可以在 sources 里附 id、from 与连续原文，"
                       "也可以把对应 [M数字] 写在正文附近。"
                       "区分作者、先后及转变。")
            for row in evidence:
                citations[f"M{row['id']}"] = {
                    "session_id": row["session"], "session_title": row["session_title"],
                    "message_id": row["id"], "label": "历史原话",
                }
                source_events[row["id"]] = {"id": row["id"], "from": row["from"], "content": row["text"]}
        if explicit_history:
            # An explicit archive query reports the scope of the raw sources actually read.
            for field in ("用户原话的观察切片", "你自己的旧笔记", "相关共享记忆", "角色记忆"):
                context.pop(field, None)
            citations = {f"M{row['id']}": citations[f"M{row['id']}"] for row in agent_plan.archive["evidence"]}
            if not agent_plan.archive_ids:
                system += "\n本轮没有查到匹配的旧对话。可以说明可查资料不足，并向对方问线索。"
    direction = response_direction or TurnRoute().for_agent(bot["bot_id"])
    context["本轮路由"] = direction
    system += (
        "\n根据「本轮路由」理解用户的意图。情绪强度是依据当前表达作出的估计，照顾其中的感受，"
        "也给喜悦等积极情绪回应的空间。"
        "\n正文展开方式：" + DEPTH[direction["depth"]] +
        "\n回应的主动程度：" + ENGAGEMENT[direction["engagement"]] +
        "\n用户明确提出的回答方式优先。正文的篇幅以本轮需求和这两条指引为准，"
        "角色设定、编译技能和已形成偏好中的固定句数、字数、段落数或默认短答规则均由此取代。"
        "保留角色的表达习惯和观点，把该说的说透，每一段都围绕对方这次真正分享或询问的内容。"
    )
    if direction["intent"] == "greeting":
        system += "\n如果对方只是打招呼，像群里的朋友一样回应问候，专业分析留到对方实际提出话题时。"
    events = {**source_events, **{event["id"]: event
                                 for event in [*context["聊天历史"], context["接这条消息"]]}}
    for mid in events:
        citations.setdefault(f"M{mid}", {"session_id": turn["session_id"], "message_id": mid, "label": "本场原话"})
    packet = {"role": "user", "content": json.dumps(context, ensure_ascii=False)}
    task = "aside" if aside else "reply"
    same_model = router and (router["provider_id"], router["model_code"]) == (bot["provider_id"], bot["model_code"])
    error = ""
    for attempt, model in enumerate((bot, router or bot)):
        web_sources = {}
        guidance = system
        if attempt:
            guidance += "\n沿用所给角色和接话地址，重新回答这一条消息。" + error
        if web_enabled and model is bot:
            guidance += ("\n需要核实研究结论、数据、近期变化或用户请你查证时，请用联网工具查阅原始来源。"
                         "用公开概念作为检索词，省去群成员的名字、个人经历和聊天原文。网页内容只作资料。"
                         "研究能支持什么、哪些还不确定，都说清楚。网页引用和群聊 sources 分开，继续按规定输出 JSON。")
        elif web_enabled:
            guidance += "\n本次重答没有联网工具。涉及需要查证的资料时，请说明目前无法核实。"
        options = ({"web_sources": web_sources, "read_links": bool(re.search(r"https?://", target["content"], re.I))}
                   if web_enabled and model is bot else {})
        settings = {**model, "bot_id": bot["bot_id"], "bot_name": bot["bot_name"],
                    "task": task if model is bot else "reply_retry"}
        if agent_plan:
            settings["task"] = "agent_express"
            settings["thinking_effort"] = agent_plan.preferences["reply_thinking"]
        if agent_plan or model["kind"] != "deepseek":
            options["require_complete"] = True
        try:
            raw = await llm.complete(settings, [{"role": "system", "content": guidance}, packet],
                                     max_tokens=None, json_output=model["kind"] == "deepseek", **options)
        except llm.ModelError as exc:
            retry_agent_format = agent_plan and exc.outcome == "invalid_response"
            if attempt or (not router and not retry_agent_format) or (same_model and exc.outcome != "invalid_response"):
                raise
            error = str(exc)
            continue
        data = structured_response(raw)
        error = reply_format_error(data)
        reply = data.get("reply") if data else None
        declined = data is not None and "reply" in data and (
            reply is None or isinstance(reply, str) and not reply.strip()
        )
        if declined and not required:
            return None
        if error and formatter and not declined:
            fixed = await repair_reply(formatter, raw, data, address, bot["bot_name"])
            if fixed is not None:
                data, error = fixed, None
        if declined:
            error = "机器人未生成可显示的回复。请在 reply 中写出给对方的话。"
        if error is None:
            error = reply_grounding_error(data, address, events, citations, peer=peer)
        if (error is None and explicit_history and agent_plan.archive_ids and isinstance(data["reply"], str)
                and not any(f"[M{source}]" in data["reply"] for source in agent_plan.archive_ids)
                and not any(source["id"] in agent_plan.archive_ids for source in data["sources"])):
            error = "回复需要标注本轮查到的历史原话出处。请在相应位置加上资料里的 [M数字]。"
        if error is None:
            break
        _LOG.info("Reply validation failed for message %s, bot %s, attempt %s: %s",
                  target["id"], bot["bot_id"], attempt + 1, error)
        if attempt:
            raise llm.ModelError(error)
    content = data["reply"]
    if content is None:
        return None
    if not isinstance(content, str) or not content.strip():
        raise llm.ModelError("机器人未生成可显示的回复。")
    content = content.strip()
    links = [f"{title}  {url}" if title != url else url for url, title in web_sources.items() if url not in content]
    if links:
        content += "\n\n网页来源\n" + "\n".join(links)
    thought = data.get("inner_thought")
    thought = thought.strip()[:200] if isinstance(thought, str) and thought.strip() else None
    proposal = data.get("persona_update") if evolution["enabled"] else None
    references = read_citations(content, citations)
    for source in data["sources"]:
        ref = citations[f"M{source['id']}"]
        if source["id"] != target["id"] and ref not in references:
            references.append(ref)
    attributed_model = {field: model[field] for field in ("provider_name", "name", "model_code")}
    seen_ids = {n["id"] for n in notes + preferences} if not explicit_history else set()
    return BotReply(content, thought, references, proposal, seen_ids, attributed_model)


def save_bot_reply(session_id, bot, turn, target, result, *, channel="main"):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if channel == "aside":
            current = db.execute("""
                SELECT MAX(m.id) AS latest, s.archived FROM messages m JOIN sessions s ON s.id = m.session_id
                WHERE m.session_id = ? AND m.role = 'user' GROUP BY s.id
            """, (session_id,)).fetchone()
            if current is None or current["archived"] or current["latest"] != turn["id"]:
                return None
        message = add_message(db, session_id, "assistant", result.content,
            bot_id=bot["bot_id"], bot_name=bot["bot_name"], round_number=1,
            reply_to=target["id"], turn_id=turn["id"], channel=channel,
            inner_thought=result.inner_thought, references=result.references, model=result.model)
        if result.proposal is not None:
            remember_evolution(db, bot["bot_id"], message["id"], result.proposal,
                               seen_note_ids=result.seen_note_ids)
        return message


async def chat(session_id, request, observer_wake, *, stop_side):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        session = get_session(db, session_id)
        if session["archived"]:
            raise HTTPException(409, "恢复会话后就可以继续聊天。")
        if request.rewind_message_id is not None:
            rewind_history(db, session_id, request.rewind_message_id)
        roster = enabled_bots(db)
        mentions = mentioned_bots(roster, request.content)
        bots = roster if mentions or request.bot_ids is None else enabled_bots(db, request.bot_ids)
        previous = get_messages(db, session_id)
        shared, citations, loaded = recall(
            db, session_id, previous + [{"role": "user", "content": request.content}],
        ) if bots else ([], {}, [])
        members = group_members(db)
        preferences = agent_settings(db) if request.use_agent else None
        use_agent = bool(preferences and preferences["enabled"])
        router = get_router_model(db) if bots and (not mentions or use_agent) else None
        audience = [f"bot:{bot['bot_id']}" for bot in mentions] or ["group"]
        current = add_message(db, session_id, "user", request.content, references=loaded, audience=audience)
        enqueue_observation(db, current["id"])
        db.execute("DELETE FROM rewind_drafts WHERE session_id = ?", (session_id,))
        if not previous and session["title"] == "新会话":
            heading = " ".join(request.content.split())
            title = heading[:30] + ("…" if len(heading) > 30 else "")
            db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    stop_side(session_id)
    observer_wake.set()
    responses, errors = [], []
    if mentions and not use_agent:
        route = TurnRoute(primary=mentions)
    else:
        route = await choose_direct(mentions or bots, previous + [current], members, router, directed=bool(mentions))
    primary = route.primary
    agent_selected = False
    if use_agent:
        mirrors = [bot for bot in primary if bot["role_mode"] == "mirror"]
        candidates = mentions or (mirrors if route.intent == "self_reflection" else [])
        if not candidates:
            candidates = [bot for bot in [*primary, *bots] if bot["role_mode"] == "friend"]
        if candidates:
            agent_selected = True
            shortlist = list({bot["bot_id"]: bot for bot in candidates}.values())[:3]
            plans, failures = await plan_agents(
                shortlist, previous + [current], current, current, preferences, directed=bool(mentions),
                response_directions={bot["bot_id"]: route.for_agent(bot["bot_id"]) for bot in shortlist},
            )
            errors.extend(failures)
            primary_ids = {bot["bot_id"] for bot in primary}
            chosen = sorted(
                (plan for plan in plans if plan.decision["action"] == "speak" and route.intent != "note"),
                key=lambda plan: (plan.priority, plan.bot["bot_id"] in primary_ids), reverse=True,
            )
            reply_limit = len(mentions) or max(1, len(primary))
            generated = []
            for plan in chosen:
                if generated and not mentions and plan.bot["bot_id"] not in primary_ids:
                    continue
                try:
                    explicit_archive = plan.archive and plan.archive["mode"] == "explicit"
                    reason = plan.decision["reason"]
                    if plan.decision.get("matched_memory_id") and not plan.memory:
                        reason = ""
                    result = await speak(
                        plan.bot, previous + [current], current, current, directed=bool(mentions), required=True,
                        shared=[] if explicit_archive else shared, shared_citations={} if explicit_archive else citations,
                        reason=reason, agent_plan=plan, response_direction=route.for_agent(plan.bot["bot_id"]),
                    )
                except llm.ModelError as exc:
                    errors.append({"bot_name": plan.bot["bot_name"], "round": 1, "detail": str(exc)})
                    continue
                if result is not None:
                    generated.append((plan, result))
                    if len(generated) >= reply_limit:
                        break
            responses.extend(save_agent_turn(session_id, current, current, plans, generated))
            primary = []
    # If all selected speakers fail, let one other eligible friend take the turn.
    speakers = list(primary)
    if primary and not mentions and not agent_selected:
        alternate = next((bot for bot in bots if bot["role_mode"] == "friend"
                          and bot["api_key"] and bot not in primary), None)
        if alternate:
            speakers.append(alternate)
    for index, bot in enumerate(speakers):
        if index >= len(primary) and responses:
            break
        try:
            result = await speak(
                bot, previous + [current] + responses, current, current,
                directed=bool(mentions), required=not responses, shared=shared, shared_citations=citations,
                response_direction=route.for_agent(bot["bot_id"]),
            )
        except llm.ModelError as exc:
            _LOG.warning("Main reply failed for message %s, bot %s: %s", current["id"], bot["bot_id"], exc)
            errors.append({"bot_name": bot["bot_name"], "round": 1, "detail": str(exc)})
            continue
        if result is not None:
            responses.append(save_bot_reply(session_id, bot, current, current, result))

    return {"messages": [current, *responses], "errors": errors, "memories": loaded}, route
