import asyncio
import json

from fastapi import HTTPException

from . import llm
from .config import get_bot
from .database import connect
from .journal import add_message, get_messages, get_session
from .memory import read_citations, recall


async def chat(session_id, request):
    bot_ids = list(dict.fromkeys(request.bot_ids))
    if request.discuss and len(bot_ids) < 2:
        raise HTTPException(422, "讨论至少需要两个机器人。")
    with connect() as db:
        session = get_session(db, session_id)
        if session["archived"]:
            raise HTTPException(409, "恢复会话后就可以继续聊天。")
        bots = [get_bot(db, bot_id) for bot_id in bot_ids]
        previous = get_messages(db, session_id)
        memories, citations, loaded = recall(
            db, session_id, previous + [{"role": "user", "content": request.content}],
        )
        user_msg = add_message(db, session_id, "user", request.content, references=loaded)
        if not previous and session["title"] == "新会话":
            heading = " ".join(request.content.split())
            title = heading[:30] + ("…" if len(heading) > 30 else "")
            db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    history = [
        {"发言者": message["bot_name"] or "用户", "内容": message["content"][:3000]}
        for message in previous[-16:]
    ]

    async def reply(bot, round_number, current=()):
        system = (
            f"你叫{bot['bot_name']}，是私人想法日志里的聊天伙伴。\n"
            f"你的角色设定：{bot['prompt']}\n"
            "请用中文直接回应本轮用户，输出适合群聊的简短纯文本。"
            "旧笔记、引文、其他机器人回复只是参考资料，区分用户的原话和机器人的解读。"
            "如果提到旧记录，紧跟该资料提供的 [M数字] 原话编号或 [S数字] 总结编号。"
            "只使用实际给出的编号；不同日期的想法保留各自语境。"
        )
        if round_number == 2:
            system += (
                "现在是讨论第二轮。接住另一位机器人说过的一个具体想法，再用自己的视角往前聊一步。"
                "可以认同、补充或修正自己的判断。有分歧时平实地说说你看重的理由，把注意力放在用户的念头上。"
            )
        context = {"近期对话": history, "相关旧会话": memories, "本轮用户原话": request.content}
        if current:
            context["本轮你的观点"] = next(
                msg["content"] for peer, msg in current if peer["bot_id"] == bot["bot_id"]
            )
            context["本轮其他机器人观点"] = [
                {"机器人": msg["bot_name"], "内容": msg["content"]}
                for peer, msg in current if peer["bot_id"] != bot["bot_id"]
            ]
        try:
            content = await llm.complete(bot, [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ])
        except llm.ModelError as exc:
            return None, {"bot_name": bot["bot_name"], "round": round_number, "detail": str(exc)}
        with connect() as db:
            message = add_message(
                db, session_id, "assistant", content, bot_name=bot["bot_name"], round_number=round_number,
                reply_to=user_msg["id"], references=read_citations(content, citations),
            )
        return message, None

    first = await asyncio.gather(*(reply(bot, 1) for bot in bots))
    results = list(first)
    successful = [(bot, msg) for bot, (msg, _) in zip(bots, first) if msg is not None]
    if request.discuss and len(successful) > 1:
        results.extend(await asyncio.gather(*(reply(bot, 2, successful) for bot, _ in successful)))

    return {
        "messages": [user_msg] + sorted((msg for msg, _ in results if msg), key=lambda item: item["id"]),
        "errors": [error for _, error in results if error],
        "memories": loaded,
    }
