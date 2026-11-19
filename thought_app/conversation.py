"""The public conversation as seen by a speaker, router or summarizer."""
from .memory import conversation_terms, overlap_score


def sender(message):
    if message["role"] == "user":
        return "user"
    if message["bot_id"] is None:
        # Old installations sometimes have only the speaker's name.
        return "legacy:" + (message["bot_name"] or "unknown")
    return f"bot:{message['bot_id']}"


class Conversation:
    def __init__(self, messages):
        self.messages = messages
        self.by_id = {message["id"]: message for message in messages}

    def event(self, message, *, limit=None):
        parent = self.by_id.get(message["reply_to"])
        audience = [sender(parent)] if parent else message.get("audience")
        content = message["content"]
        if parent:
            recipient = "用户" if parent["role"] == "user" else parent["bot_name"]
        else:
            recipient = "全群" if audience == ["group"] else None
        return {
            "id": message["id"], "from": sender(message),
            "name": message["bot_name"] if message["role"] == "assistant" else "用户",
            "to": audience, "所接发言人": recipient, "reply_to": message["reply_to"],
            "turn_id": message["turn_id"] or (message["id"] if message["role"] == "user" else None),
            "channel": message["channel"], "content": content if limit is None else content[:limit],
        }

    def recent(self):
        return [self.event(message, limit=3500) for message in self.messages[-16:]]

    def history(self, turn, target, *, user_only=False):
        """Keep the current thread, recent dialogue and up to two relevant older exchanges."""
        eligible = [m for m in self.messages if m["id"] != target["id"] and (not user_only or m["role"] == "user")]
        selected = {m["id"] for m in eligible[-16:]}
        terms = conversation_terms(turn["content"] + " " + target["content"])
        older = []
        for m in eligible[:-16]:
            score = overlap_score(terms, m["content"][:3500])
            if score:
                older.append((score, m["id"]))
        for _, mid in sorted(older, reverse=True)[:2]:
            selected.add(mid)
            # Keep a concrete response even when only its parent matches the query.
            child = next((m for m in reversed(eligible) if m["reply_to"] == mid), None)
            if child:
                selected.add(child["id"])
        selected.add(turn["id"])
        selected.add(target["id"])
        # Recover the actual parent and initiating user across the window boundary.
        for mid in list(selected):
            message = self.by_id[mid]
            selected.update((message["turn_id"], message["reply_to"]))
        history = [m for m in eligible if m["id"] in selected]
        limit = min(3500, 24000 // max(1, len(history)))
        return [self.event(m, limit=limit) for m in history]
