import json
import re
import unicodedata
from datetime import datetime

from fastapi import HTTPException
from pydantic import ValidationError

from .journal import SummaryDraft
from .prompts import load_prompt


SUMMARY_PROMPT = load_prompt("summary")


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


GENERIC_TERMS = frozenset({
    "这个", "那种", "这样", "觉得", "感觉", "真的", "什么", "自己", "现在", "以前",
    "还是", "然后", "如果", "所以", "的话", "可能", "一个", "一些", "我想", "我是",
    "真的很", "有时候", "我觉得", "我感觉", "我自己", "猜想", "想法", "你好", "聊天", "东西",
    "事情", "有点", "但是", "的时候", "的时候我", "这件事", "那个", "一下", "大家",
    "the", "and", "that", "this", "with", "you", "about", "have", "not", "just",
})

def conversation_terms(value):
    terms = set()
    for part in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", normalize(value)):
        if part.isascii():
            if len(part) >= 3 and not part.isdigit():
                terms.add(part)
            continue
        for width in (2, 3):
            terms.update(part[index:index + width] for index in range(len(part) - width + 1))
    return terms - GENERIC_TERMS


def overlap_score(query_terms, value):
    overlap = query_terms & conversation_terms(value)
    if len(overlap) < 2 and not any(len(token) >= 3 for token in overlap):
        return 0
    return sum(min(len(token), 5) for token in overlap)


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
                        """SELECT m.created_at, m.role, m.bot_name, m.content, m.reply_to, p.role AS parent_role,
                                   p.bot_name AS parent_name FROM messages m LEFT JOIN messages p ON p.id = m.reply_to
                                   WHERE m.id = ? AND m.session_id = ?""",
                        (message_id, sid),
                    ).fetchone()
                    if source:
                        token = f"M{message_id}"
                        citations[token] = {**base_reference, "message_id": message_id, "label": "原话"}
                        evidence.append({
                            "引用": f"[{token}]",
                            "日期": datetime.fromisoformat(source["created_at"]).astimezone().date().isoformat(),
                            "发言者": source["bot_name"] or "用户",
                            "接谁的话": "用户" if source["parent_role"] == "user" else source["parent_name"],
                            "所接消息id": source["reply_to"], "原话": source["content"][:400],
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
