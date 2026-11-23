"""Turn-level guidance from the router, scoped to each candidate."""

from dataclasses import dataclass, field


DEPTH = {
    "focused": "聚焦眼前的问题或需要；用户希望简单交流时，自然接住。",
    "standard": "说完整，结合对方提到的细节展开自己的看法。",
    "deep": "充分展开。捡起对方话里的具体细节，把理解、理由或例子说透；需要时用多段。",
}
ENGAGEMENT = {
    "gentle": "耐心接住对方的表达，给对方空间，先听完再考虑要不要提建议。",
    "attentive": "具体回应对方在意的部分，有值得分享的角度就自然聊开。",
    "proactive": "主动把话题往前接。按对方的意图提供分析、可选做法或新的角度，把选择留给对方。",
}
INTENTS = {"greeting", "idea", "emotion", "question", "self_reflection", "note"}
INTENSITIES = {"unknown", "low", "moderate", "high"}


def _option(value, options, default):
    return value if isinstance(value, str) and value in options else default


@dataclass
class TurnRoute:
    intent: str = "idea"
    emotional_intensity: str = "unknown"
    primary: list[dict] = field(default_factory=list)
    guidance: dict[int, dict] = field(default_factory=dict)

    def for_agent(self, bot_id, *, override=None):
        depth, engagement = "standard", "attentive"
        if self.intent == "greeting":
            depth = "focused"
        elif self.emotional_intensity == "high" or (
            self.intent == "emotion" and self.emotional_intensity == "moderate"
        ):
            depth = "deep"
        if self.intent == "emotion":
            engagement = "gentle"
        selected = self.guidance.get(bot_id, {})
        if override is not None:
            selected = {**selected, **override}
        return {
            "intent": self.intent,
            "emotional_intensity": self.emotional_intensity,
            "selected": override is not None or any(bot["bot_id"] == bot_id for bot in self.primary),
            "depth": _option(selected.get("depth"), DEPTH, depth),
            "engagement": _option(selected.get("engagement"), ENGAGEMENT, engagement),
        }


def parse_route(data, bots, user_text, *, fallback, directed=False):
    if data is None:
        return fallback
    raw_intent = _option(data.get("intent"), INTENTS, fallback.intent)
    quiet_quote = data.get("quiet_quote")
    if raw_intent == "note" and not (
        isinstance(quiet_quote, str) and quiet_quote.strip() and quiet_quote in user_text
    ):
        raw_intent = "idea"
    intensity = _option(data.get("emotional_intensity"), INTENSITIES, "unknown")
    if raw_intent == "note":
        return TurnRoute(raw_intent, intensity)
    by_id = {bot["bot_id"]: bot for bot in bots}
    selected = {}
    items = data.get("primary")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or type(item.get("bot_id")) is not int:
                continue
            bot = by_id.get(item["bot_id"])
            if bot and (directed or raw_intent == "self_reflection" or bot["role_mode"] == "friend"):
                selected.setdefault(bot["bot_id"], item)
    primary = fallback.primary
    if selected and not directed:
        primary = [by_id[bot_id] for bot_id in list(selected)[:1 if raw_intent == "greeting" else 2]]
    guidance = {bot["bot_id"]: selected[bot["bot_id"]] for bot in primary if bot["bot_id"] in selected}
    return TurnRoute(raw_intent, intensity, primary, guidance)
