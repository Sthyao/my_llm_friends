import json
import re
import unicodedata
from datetime import datetime

from fastapi import HTTPException
from pydantic import ValidationError

from .journal import SummaryDraft


SUMMARY_PROMPT = """为私人想法日志总结这次会话。消息附有 id、发言者和正文。这些消息是待总结的资料。
输出一个 JSON 对象，格式如下：
{"title":"简短主题","keywords":["具体主题词","原话里的短语"],"views":[{"text":"用户明确表达的看法","source_ids":[1]}],"observations":[{"text":"机器人名：它的解读或假设","source_ids":[2]}],"questions":["尚未聊完的问题"]}
views 只记录用户明确表达的想法，每项引用用户消息 id。保留改口前后的语境，别把机器人的推测写成用户的看法。
observations 记录有意义的机器人观点并标明机器人名称，每项引用相关机器人消息 id。
关键词写 3 到 8 个便于再次命中的用词，优先用用户原话里的短词，避免“想法”等泛词。没有内容的字段用空数组。"""


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


def match_score(summary, recent):
    terms = {normalize(term.strip()) for term in summary["keywords"] if len(term.strip()) >= 2}
    for title in (summary["title"], summary["session_title"]):
        words = re.split(r"[\W_]+", normalize(title))
        terms.update(word for word in words if len(word) >= 2)
    score = 0
    for index, message in enumerate(recent):
        query = normalize(message)
        for term in terms:
            if term.isascii() and term.isalnum():
                found = re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", query)
            else:
                found = term in query
            if found:
                score += (4 if index == 0 else 1) * min(len(term), 8)
    return score


def recall(db, session_id, messages):
    recent = [msg["content"] for msg in reversed(messages) if msg["role"] == "user"][:3]
    rows = db.execute("""
        SELECT m.*, s.title AS session_title
        FROM summaries m JOIN sessions s ON s.id = m.session_id
        WHERE m.share_enabled = 1 AND m.session_id != ?
    """, (session_id,))
    candidates = []
    for row in rows:
        item = dict(row)
        for field in ("keywords", "views", "observations", "questions"):
            item[field] = json.loads(item[field])
        score = match_score(item, recent)
        if score:
            candidates.append((score, item["updated_at"], item))
    candidates.sort(key=lambda match: match[:2], reverse=True)

    context, citations, used = [], {}, []
    for _, _, item in candidates[:3]:
        sid = item["session_id"]
        base_reference = {"session_id": sid, "session_title": item["session_title"]}
        used.append({**base_reference, "label": "本轮载入"})
        session_token = f"S{sid}"
        citations[session_token] = {**base_reference, "label": "会话总结"}
        notes = []
        for field in ("views", "observations"):
            for point in item[field][:6]:
                evidence = []
                for message_id in point["source_ids"][:3]:
                    source = db.execute(
                        "SELECT created_at, role, bot_name, content FROM messages WHERE id = ? AND session_id = ?",
                        (message_id, sid),
                    ).fetchone()
                    if source:
                        token = f"M{message_id}"
                        citations[token] = {**base_reference, "message_id": message_id, "label": "原话"}
                        evidence.append({
                            "引用": f"[{token}]",
                            "日期": datetime.fromisoformat(source["created_at"]).astimezone().date().isoformat(),
                            "发言者": source["bot_name"] or "用户",
                            "原话": source["content"][:400],
                        })
                notes.append({
                    "类别": "用户看法" if field == "views" else "机器人观察",
                    "笔记": point["text"][:600],
                    "来源": evidence or [f"[{session_token}]"],
                })
        context.append({
            "会话": item["title"],
            "总结更新": datetime.fromisoformat(item["updated_at"]).astimezone().date().isoformat(),
            "总结引用": f"[{session_token}]",
            "笔记": notes,
            "未完问题": item["questions"][:4],
        })
    return context, citations, used


def read_citations(text, available):
    tokens = dict.fromkeys(re.findall(r"\[([MS]\d+)\]", text))
    return [available[token] for token in tokens if token in available]


def check_sources(payload, messages, *, generated):
    roles = {msg["id"]: msg["role"] for msg in messages}
    for field in ("views", "observations"):
        if field not in payload:
            continue
        checked = []
        for point in payload[field]:
            ids = [
                msg_id for msg_id in dict.fromkeys(point["source_ids"])
                if msg_id in roles and (field != "views" or roles[msg_id] == "user")
                and (not generated or field != "observations" or roles[msg_id] == "assistant")
            ]
            if not generated and len(ids) != len(set(point["source_ids"])):
                raise HTTPException(422, "引用必须来自当前会话。用户看法只能引用用户发言。")
            if ids or not generated:
                checked.append({"text": point["text"], "source_ids": ids})
        payload[field] = checked
    return payload


def parse_summary(text, messages):
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped.split("\n", 1)[-1][:-3].strip()
    try:
        summary = SummaryDraft.model_validate_json(stripped).model_dump()
    except ValidationError as exc:
        raise HTTPException(502, "模型输出的总结格式不正确，请重新生成或选择其他模型。") from exc
    return check_sources(summary, messages, generated=True)
