from dataclasses import dataclass
import json
import logging
import re

from fastapi import HTTPException

from . import llm
from .config import get_bot, long_replies_enabled
from .conversation import Conversation, sender
from .database import connect
from .evolution import get_evolution, remember_evolution
from .journal import add_message, get_messages, get_session
from .memory import read_citations, recall
from .observer import enqueue_observation, mirror_context, recall_observations
from .prompts import load_prompt


_LOG = logging.getLogger(__name__)


ROUTER_PROMPT = load_prompt("router")
FRIEND_PROMPT = load_prompt("friend")
PEER_PROMPT = load_prompt("peer")
REPLY_PROMPT = load_prompt("reply")
MIRROR_PROMPT = load_prompt("mirror")


def structured_response(raw):
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[-1][:-3].strip()
    try:
        result = json.loads(raw)
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


async def choose_direct(bots, messages, members):
    if not bots:
        return "note", []
    friends = [bot for bot in bots if bot["role_mode"] == "friend"]
    first = next((bot for bot in friends if bot["api_key"]), friends[0] if friends else None)
    available = {bot["bot_id"]: bot for bot in friends if bot["api_key"]}
    previous = next((available[m["bot_id"]] for m in reversed(messages)
                     if m["channel"] == "main" and m["bot_id"] in available), first)
    fallback = ("idea", [previous]) if previous else ("note", [])
    model = next((bot for bot in bots if bot["api_key"]), bots[0])
    try:
        raw = await llm.complete({**model, "task": "route"}, [
            {"role": "system", "content": ROUTER_PROMPT},
            {"role": "user", "content": json.dumps({
                "候选人": candidates_packet(bots), "群成员": members, "近期群聊": Conversation(messages).recent(),
                "最新用户消息id": messages[-1]["id"],
            }, ensure_ascii=False)},
        ], max_tokens=350, json_output=model["kind"] == "deepseek")
    except llm.ModelError:
        return fallback
    plan = structured_response(raw)
    if plan is None or not isinstance(plan.get("primary"), list):
        return fallback
    intent = plan.get("intent")
    if intent not in ("greeting", "idea", "emotion", "question", "self_reflection", "note"):
        intent = "idea"
    quiet_quote = plan.get("quiet_quote")
    if intent == "note" and isinstance(quiet_quote, str) and quiet_quote.strip() and quiet_quote in messages[-1]["content"]:
        return "note", []
    by_id = {bot["bot_id"]: bot for bot in bots}
    selected = {}
    for item in plan["primary"]:
        if not isinstance(item, dict) or type(item.get("bot_id")) is not int:
            continue
        bot = by_id.get(item["bot_id"])
        if bot and (bot["role_mode"] == "friend" or intent == "self_reflection"):
            selected.setdefault(bot["bot_id"], bot)
    if not selected:
        return fallback
    return ("idea" if intent == "note" else intent), list(selected.values())[:1 if intent == "greeting" else 2]


@dataclass
class BotReply:
    content: str
    inner_thought: str | None
    references: list
    proposal: dict | None
    seen_note_ids: set[int]


def reply_grounding_error(data, address, events, citations, *, peer):
    returned = data.get("address")
    if returned != address or type(returned["reply_to"]) is not int:
        return "address 必须原样复制本次发言。"
    sources = data.get("sources")
    if not isinstance(sources, list):
        return "请用 sources 列出采用的群消息。"
    cited = set()
    for source in sources:
        if not isinstance(source, dict) or type(source.get("id")) is not int:
            return "sources 的 id 必须是群消息的整数编号。"
        event = events.get(source["id"])
        quote = source.get("quote")
        if (not event or source.get("from") != event["from"] or not isinstance(quote, str)
                or not quote.strip() or quote not in event["content"]):
            return "sources 的作者和连续原文必须在同一条消息中找到。请删掉无出处的回忆并重新回答。"
        cited.add(event["id"])
    if peer and data["reply"] is not None and address["reply_to"] not in cited:
        return "接群友的话时，请在 sources 中引用接这条消息的作者和连续原文。"
    if isinstance(data["reply"], str) and any(
        token not in citations for token in re.findall(r"\[([MS]\d+)\]", data["reply"])
    ):
        return "回复里出现了资料中没有的来源编号，请只用有出处的话重新回答。"
    return None


async def speak(bot, messages, turn, target, *, directed, shared, shared_citations, aside=False, reason="", required=False):
    query = "\n".join([turn["content"], target["content"]] +
                      [msg["content"] for msg in messages[-8:] if msg["role"] == "user"][-2:])
    is_mirror = bot["role_mode"] == "mirror"
    with connect() as db:
        members = group_members(db)
        long_replies = long_replies_enabled(db)
        evolution = {"enabled": False, "notes": []} if is_mirror else get_evolution(db, bot["bot_id"], query)
        if is_mirror:
            observer, observer_citations = mirror_context(db, query, turn["id"])
        else:
            observer, observer_citations = recall_observations(db, query, turn["id"])
    # The mirror uses user-sourced observations and its supplied conversation, not summaries of AI opinions.
    citations = observer_citations if is_mirror else {**shared_citations, **observer_citations}
    notes = []
    for note in evolution["notes"]:
        source = note["source_message_id"]
        token = f"M{source}"
        citations[token] = {
            "session_id": note["source_session_id"],
            "session_title": note.get("source_session_title", "旧会话"),
            "message_id": source, "label": "此前的发言",
        }
        notes.append({"id": note["id"], "kind": note["kind"], "text": note["text"], "source": f"[{token}]"})
    peer = target["role"] == "assistant"
    if is_mirror:
        persona = MIRROR_PROMPT
        if peer:
            persona += "\n你以拟写的用户视角接这位群友的话。群友的观点只作为回应对象。"
    else:
        persona = (PEER_PROMPT if peer else FRIEND_PROMPT) + f"\n你叫{bot['bot_name']}。{bot['prompt']}"
    system = persona + "\n\n" + REPLY_PROMPT
    if long_replies:
        system += ("\n用户开启了长回答。遇到有内容的话题，像一对一深聊一样用几段把自己的想法说透。"
                   "抓住对方说的具体细节，说说你怎么读、为什么这么想，再沿着它聊到一个新发现、场景或例子。"
                   "值得聊时可以写三五百字或更多，写到意思完整。保留朋友的语气。"
                   "简单寒暄自然简短，对方当下明确要求简短或只想听着时以那句话为准。")
    else:
        system += "\n回答长度随话题调整。寒暄可以很短；读到一段诗、一个复杂的念头或问题时，顺着细节多聊几段。"
    if not evolution["enabled"]:
        system += "\n本次 persona_update 必须为 null。"
    if directed:
        system += "\n用户邀请你接指定的这句话，请直接回应它的作者。"

    required = required or directed
    if required:
        system += "\n这句话由你来接。reply 请写出实际的聊天回复。对方想保留一点余味时，顺着聊一句也可以。"

    conversation = Conversation(messages)
    address = {"from": f"bot:{bot['bot_id']}", "to": sender(target), "reply_to": target["id"]}
    context = {
        "群成员": members, "本次发言": address,
        "本轮用户消息id": turn["id"], "发表位置": "aside" if aside else "main",
        "聊天历史": conversation.history(turn, target, user_only=is_mirror),
        "接这条消息": conversation.event(target),
    }
    if observer:
        context["用户原话的观察切片"] = observer
    if notes:
        context["你自己的旧笔记"] = notes
    if shared and not is_mirror:
        context["相关共享记忆"] = shared
    if reason:
        context["可选的话头"] = reason
    events = {event["id"]: event for event in [*context["聊天历史"], context["接这条消息"]]}
    for mid in events:
        citations.setdefault(f"M{mid}", {"session_id": turn["session_id"], "message_id": mid, "label": "本场原话"})
    prompt = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]
    max_tokens = (2600 if aside else 3600) if long_replies else (900 if aside else 1800)
    for attempt in range(2):
        raw = await llm.complete({**bot, "task": "aside" if aside else "reply"}, prompt,
                                 max_tokens=max_tokens, json_output=bot["kind"] == "deepseek")
        data = structured_response(raw)
        if not data or "reply" not in data:
            error = "机器人返回的回复格式有误。请输出包含 reply 的完整 JSON 对象。"
        elif required and (not isinstance(data["reply"], str) or not data["reply"].strip()):
            error = "机器人未生成可显示的回复。请在 reply 中写出给对方的话。"
        else:
            error = reply_grounding_error(data, address, events, citations, peer=peer)
        if error is None:
            break
        _LOG.info("Reply validation failed for message %s, bot %s, attempt %s: %s",
                  target["id"], bot["bot_id"], attempt + 1, error)
        if attempt:
            raise llm.ModelError(error)
        prompt[0]["content"] += "\n重新生成本次回复。" + error
    content = data["reply"]
    if content is None:
        return None
    if not isinstance(content, str) or not content.strip():
        raise llm.ModelError("机器人未生成可显示的回复。")
    content = content.strip()
    thought = data.get("inner_thought")
    thought = thought.strip()[:200] if isinstance(thought, str) and thought.strip() else None
    proposal = data.get("persona_update") if evolution["enabled"] else None
    references = read_citations(content, citations)
    for source in data["sources"]:
        ref = citations[f"M{source['id']}"]
        if source["id"] != target["id"] and ref not in references:
            references.append(ref)
    return BotReply(content, thought, references, proposal, {n["id"] for n in notes})


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
            inner_thought=result.inner_thought, references=result.references, model=bot)
        if result.proposal is not None:
            remember_evolution(db, bot["bot_id"], message["id"], result.proposal,
                               seen_note_ids=result.seen_note_ids)
        return message


async def chat(session_id, request, observer_wake):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        session = get_session(db, session_id)
        if session["archived"]:
            raise HTTPException(409, "恢复会话后就可以继续聊天。")
        roster = enabled_bots(db)
        mentions = mentioned_bots(roster, request.content)
        bots = roster if mentions or request.bot_ids is None else enabled_bots(db, request.bot_ids)
        previous = get_messages(db, session_id)
        shared, citations, loaded = recall(
            db, session_id, previous + [{"role": "user", "content": request.content}],
        ) if bots else ([], {}, [])
        members = group_members(db)
        audience = [f"bot:{bot['bot_id']}" for bot in mentions] or ["group"]
        current = add_message(db, session_id, "user", request.content, references=loaded, audience=audience)
        enqueue_observation(db, current["id"])
        db.execute("DELETE FROM rewind_drafts WHERE session_id = ?", (session_id,))
        if not previous and session["title"] == "新会话":
            heading = " ".join(request.content.split())
            title = heading[:30] + ("…" if len(heading) > 30 else "")
            db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    observer_wake.set()
    responses, errors = [], []
    if mentions:
        intent, primary = "directed", mentions
    else:
        intent, primary = await choose_direct(bots, previous + [current], members)
    # If all selected speakers fail, let one other eligible friend take the turn.
    speakers = list(primary)
    if primary and not mentions:
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
            )
        except llm.ModelError as exc:
            _LOG.warning("Main reply failed for message %s, bot %s: %s", current["id"], bot["bot_id"], exc)
            errors.append({"bot_name": bot["bot_name"], "round": 1, "detail": str(exc)})
            continue
        if result is not None:
            responses.append(save_bot_reply(session_id, bot, current, current, result))

    return {"messages": [current, *responses], "errors": errors, "memories": loaded}, intent
