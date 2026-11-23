"""Run the staged persona decisions against the existing journal."""

import asyncio
from dataclasses import dataclass
import logging

from fastapi import HTTPException

from . import llm
from .agent_config import AgentModel, effective_agent_settings, ensure_profile
from .agent_runtime import AgentDecisionLoop, asks_archive, persona_source_hash
from .agent_store import (
    AgentStore, apply_agent_change, store_agent_metadata, store_agent_sources, validate_agent_sources,
    visible_agent_message,
)
from .conversation import Conversation
from .database import connect
from .evolution import active_traits, get_evolution, remember_evolution
from .journal import add_message


_LOG = logging.getLogger(__name__)


@dataclass
class AgentPlan:
    bot: dict
    profile: dict
    decision: dict
    short_term: list
    archive: dict | None
    preferences: dict
    evolution: dict

    @property
    def memory(self):
        key = self.decision["memory_id"]
        return next((note for note in self.decision["retrieved"] if note["id"] == key), None)

    @property
    def archive_ids(self):
        return {row["id"] for row in self.archive["evidence"]} if self.archive else set()

    @property
    def priority(self):
        return bool(self.archive_ids or self.memory)


async def plan_agents(bots, messages, turn, target, preferences, *, directed, observe=True, response_directions):
    if not bots:
        return [], []
    conversation = Conversation(messages)
    recent = [message for message in messages if message["id"] != target["id"]]
    short = [conversation.event(message, limit=3500) for message in recent[-8:]]
    names = {bot["bot_id"]: bot["bot_name"] for bot in bots}
    store = AgentStore()
    text = target["content"]
    evolution_query = "\n".join([turn["content"], text] + [
        message["content"] for message in messages[-8:] if message["role"] == "user"
    ][-2:])
    archive_bot = next((bot for bot in bots if bot["role_mode"] != "mirror"), None)
    explicit = archive_bot is not None and target["role"] == "user" and asks_archive(text)
    archive = None
    if explicit:
        loop = AgentDecisionLoop(store, AgentModel(archive_bot, effective_agent_settings(archive_bot, preferences)), names)
        try:
            archive = await loop.archive_context(
                turn["session_id"], text, [*short, {"id": target["id"]}, {"id": turn["id"]}],
                mode="explicit",
            )
        except llm.ModelError as exc:
            return [], [{"bot_name": archive_bot["bot_name"], "round": 1, "detail": str(exc)}]

    async def decide(bot):
        profile = await ensure_profile(bot, preferences)
        settings = effective_agent_settings(bot, preferences)
        mirror = bot["role_mode"] == "mirror"
        scoped_short = short
        scoped_profile = profile
        evolution = {"enabled": False, "notes": []}
        if mirror:
            scoped_short = [conversation.event(message, limit=3500)
                            for message in [row for row in recent if row["role"] == "user"][-8:]]
            scoped_profile = {**profile, "skills": [
                {**skill, "reads": [], "writes": []} for skill in profile["skills"]
            ]}
        elif not explicit:
            with connect() as db:
                evolution = get_evolution(db, bot["bot_id"], evolution_query, base_hash=persona_source_hash(bot["prompt"]))
            scoped_profile = {**profile, "learned_skills": [
                note["skill"] for note in evolution["notes"] if note.get("skill")
            ]}
        loop = AgentDecisionLoop(store, AgentModel(bot, settings), names)
        decision = await loop.decide(
            bot["bot_id"], scoped_profile, text, scoped_short, directed, False, observe and not mirror,
            archive=None if mirror else archive, allow_lookup=not explicit and not mirror,
            response_direction=response_directions[bot["bot_id"]],
        )
        return AgentPlan(bot, scoped_profile, decision, scoped_short, None if mirror else archive, settings, evolution)

    results = await asyncio.gather(*(decide(bot) for bot in bots), return_exceptions=True)
    plans, errors = [], []
    for bot, outcome in zip(bots, results):
        if isinstance(outcome, (llm.ModelError, ValueError, HTTPException)):
            detail = str(outcome.detail if isinstance(outcome, HTTPException) else outcome)
            errors.append({"bot_name": bot["bot_name"], "round": 1, "detail": detail})
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            plans.append(outcome)
    for plan in plans:
        query = plan.decision.get("archive_query")
        if not query:
            continue
        loop = AgentDecisionLoop(store, AgentModel(plan.bot, plan.preferences), names)
        try:
            recalled = await loop.archive_context(
                turn["session_id"], query, [*short, {"id": target["id"]}, {"id": turn["id"]}],
                mode="skill", requested_by=plan.bot["bot_id"],
            )
            plan.decision = await loop.decide(
                plan.bot["bot_id"], plan.profile, text, short, directed, False, observe, archive=recalled,
                response_direction=response_directions[plan.bot["bot_id"]],
            )
            plan.archive = recalled
        except (llm.ModelError, ValueError) as exc:
            _LOG.info("Optional agent history lookup failed for message %s: %s", turn["id"], exc)
        break
    return plans, errors


def save_agent_turn(session_id, turn, target, plans, replies, *, channel="main"):
    """Persist decisions and their source-backed effects as a unit."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("""
            SELECT s.archived, (SELECT MAX(id) FROM messages WHERE session_id = s.id AND role = 'user') AS latest
            FROM sessions s WHERE s.id = ?
        """, (session_id,)).fetchone()
        if active is None or active["archived"] or active["latest"] != turn["id"]:
            return []
        if not db.execute(
            f"SELECT 1 FROM messages m WHERE m.id = ? AND m.session_id = ? AND {visible_agent_message}",
            (target["id"], session_id),
        ).fetchone():
            return []
        accepted = set()
        for plan in plans:
            archive_ids = plan.archive_ids
            memory = plan.memory
            change = plan.decision["change"]
            if archive_ids and not validate_agent_sources(db, session_id, archive_ids):
                continue
            memory_ids = {memory["id"]} if memory else set()
            if change and change["op"] == "resolve":
                memory_ids.add(change["memory_id"])
            bot_id = plan.bot["bot_id"]
            if not db.execute("""
                SELECT 1 FROM bots b JOIN personas p ON p.id = b.persona_id
                JOIN agent_persona_programs compiled ON compiled.persona_id = p.id
                WHERE b.id = ? AND b.enabled = 1 AND p.id = ? AND p.prompt = ? AND compiled.version = ?
                  AND b.role_mode = ?
            """, (bot_id, plan.bot["persona_id"], plan.bot["prompt"], plan.profile["version"],
                  plan.bot["role_mode"])).fetchone():
                continue
            learned_ids = {note["id"] for note in plan.evolution["notes"] if note.get("skill")}
            if learned_ids and not learned_ids.issubset(
                trait["note_id"] for trait in active_traits(db, bot_id, persona_source_hash(plan.bot["prompt"]))
            ):
                continue
            if any(not db.execute("""
                SELECT 1 FROM agent_memories WHERE id = ? AND bot_id = ? AND resolved_by IS NULL
            """, (mid, bot_id)).fetchone() for mid in memory_ids):
                continue
            apply_agent_change(db, bot_id, turn["id"], change)
            accepted.add(bot_id)
        saved = []
        for plan, reply in replies:
            bot = plan.bot
            if bot["bot_id"] not in accepted:
                continue
            message = add_message(
                db, session_id, "assistant", reply.content, bot_id=bot["bot_id"], bot_name=bot["bot_name"],
                round_number=1, reply_to=target["id"], turn_id=turn["id"], channel=channel,
                inner_thought=reply.inner_thought, references=reply.references, model=reply.model,
            )
            archive_ids = {ref["message_id"] for ref in reply.references
                           if ref.get("message_id") in plan.archive_ids}
            memory = plan.memory
            store_agent_sources(db, message["id"], archive_ids, [memory["source_id"]] if memory else [])
            store_agent_metadata(db, message["id"], plan.profile["version"])
            message["agent"] = {"mode": "agent", "version": plan.profile["version"],
                                "archive_used": bool(archive_ids)}
            if reply.proposal is not None:
                remember_evolution(db, bot["bot_id"], message["id"], reply.proposal,
                                   seen_note_ids=reply.seen_note_ids, base_hash=persona_source_hash(bot["prompt"]))
            saved.append(message)
        return saved
