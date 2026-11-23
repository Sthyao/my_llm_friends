"""Run paired live trials of the persona harness against synthetic conversations."""

import argparse
import asyncio
from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sqlite3
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from .harness import Harness, PROFILES, Store
from .model import DeepSeek


TOPIC = "旧电影院"
PROMISE = f"@拾荒者 我在写一篇关于{TOPIC}的文章，写完给你看。帮我记住就好，这条不用回。"
BOUNDARY = f"@刺头 关于{TOPIC}的文章我想自己写，等我问你再给意见。"
PROBE = "写完了"
INVITATION = "@刺头 我现在想听你的意见。你会先看这篇文章的哪一点？"
UNRELATED = "晚饭吃完了"
DELIVERY = f"@拾荒者 给你看我之前说的那篇{TOPIC}的文章。正文：售票窗合上了，玻璃后还贴着昨晚的场次。"
UNSUPPORTED_PROMISE = re.compile(r"旧电影院|说好|约好|答应|约定|上次说|之前说")
CONDITIONS = ("full", "skill_off", "rewound", "prompt_only", "prompt_with_memory", "invited",
              "unrelated", "delivered")


def snapshot(source, destination):
    with closing(sqlite3.connect(source)) as current, closing(sqlite3.connect(destination)) as copy:
        current.backup(copy)
    return Store(destination)


def matching_memory(memories, kind, source_id):
    return next((item for item in memories if
                 item["kind"] == kind and item["source_id"] == source_id), None)


async def prompt_baseline(model, condition, memories):
    start = perf_counter()
    result = await model.ask(condition, """
你在模拟一个中文朋友群聊。根据完整人设、当前会话和各角色各自的记忆，选至多一人自然接话。
没有合适的话就可以都安静。可以问是哪一件事。每个角色只能依据分配给自己的记忆，
把实际出现的往事当事实。返回 JSON 对象
{"winner":"keeper|critic 或 null","reply":"一句自然接话或 null","source_id":null}。
如果这句话用到自己的记忆，填写该记忆的整数 source_id。否则保持 null。
winner 为 null 时 reply 也为 null。
""", {"personas": PROFILES, "recent": [], "current": PROBE, "memories": memories})
    winner, reply, source_id = result.get("winner"), result.get("reply"), result.get("source_id")
    if (winner is None and reply is not None) or (
        winner is not None and (
            not isinstance(winner, str) or winner not in PROFILES
            or not isinstance(reply, str) or not reply.strip()
        )
    ):
        raise ValueError(f"{condition} 返回的发言人与正文不匹配")
    return {
        "winner": winner,
        "reply": reply,
        "source_id": source_id,
        "source_valid": source_id is None or (
            type(source_id) is int
            and any(item["source_id"] == source_id for item in memories.get(winner, []))
        ),
        "elapsed_ms": round((perf_counter() - start) * 1000),
    }


def evaluate(run):
    promise, boundary = run["promise"], run["boundary"]
    full, off, rewound = (run["scenarios"][condition] for condition in CONDITIONS[:3])
    invite = run["scenarios"]["invited"]
    keeper = full["decisions"]["keeper"]
    critic = full["decisions"]["critic"]
    off_keeper = off["decisions"]["keeper"]
    rewound_keeper = rewound["decisions"]["keeper"]
    return {
        "promise_written": (
            promise is not None and promise["text"] in PROMISE and TOPIC in promise["source"]
        ),
        "listening_role_still_records_promise": (
            promise is not None and run["seed_turns"][0]["winner"] is None
            and run["seed_turns"][0]["decisions"]["keeper"]["action"] == "listen"
        ),
        "boundary_written": (
            boundary is not None and boundary["text"] in BOUNDARY
        ),
        "full_keeper_acts_on_promise": (
            promise is not None
            and keeper["action"] == "speak"
            and keeper["memory_id"] == promise["id"]
            and any(item["id"] == promise["id"] for item in keeper["retrieved"])
            and bool(keeper["skills"])
            and full["winner"] == "keeper"
        ),
        "full_critic_listens_with_boundary_available": (
            boundary is not None
            and critic["action"] == "listen"
            and critic["memory_id"] == boundary["id"]
            and any(item["id"] == boundary["id"] for item in critic["retrieved"])
        ),
        "progress_does_not_complete_promise": full["promise_still_stored"],
        "skill_toggle_retains_stored_promise": off["promise_still_stored"],
        "skill_toggle_removes_targeted_recall": (
            not off_keeper["retrieved"] and off_keeper["memory_id"] is None
        ),
        "rewind_removes_promise_from_store_and_recall": (
            not rewound["promise_still_stored"]
            and rewound["boundary_still_stored"]
            and rewound_keeper["memory_id"] is None
            and all(item["source_id"] != run["promise_event_id"] for item in rewound_keeper["retrieved"])
        ),
        "no_invented_promise_without_evidence": all(
            not UNSUPPORTED_PROMISE.search(run["scenarios"][case]["reply"] or "")
            for case in ("skill_off", "rewound", "unrelated")
        ),
        "cross_session_starts_with_empty_short_term": all(
            not run["scenarios"][condition]["short_term"] for condition in CONDITIONS[:3]
        ),
        "same_session_reads_previous_exchange": (
            any(item["id"] == full["event_id"] for item in invite["short_term"])
            and isinstance(full["reply"], str)
            and any(item["text"] == full["reply"] for item in invite["short_term"])
        ),
        "invited_critic_speaks": invite["winner"] == "critic" and bool(invite["reply"]),
        "rewind_invalidates_cross_session_sourced_reply": (
            bool(run["rewind_after_reply"]["sourced_replies_before"])
            and not run["rewind_after_reply"]["surviving_sourced_replies"]
            and run["rewind_after_reply"]["user_event_survives"]
        ),
        "unrelated_progress_does_not_use_promise": (
            run["scenarios"]["unrelated"]["decisions"]["keeper"]["memory_id"] is None
            and run["scenarios"]["unrelated"]["decisions"]["keeper"]["matched_memory_id"] is None
        ),
        "actual_delivery_resolves_promise_and_rewind_restores_it": (
            promise is not None and run["delivery_state"]["resolved"]
            and run["delivery_state"]["restored_after_rewind"]
        ),
    }


def show(condition, result):
    who = {"keeper": "拾荒者", "critic": "刺头", None: "无人发言"}[result["winner"]]
    quote = f" · {result['reply']}" if result["reply"] else ""
    print(f"  {condition} · {who}{quote}", flush=True)


async def trial(model, directory):
    seed_path = directory / "seed.sqlite3"
    store = Store(seed_path)
    harness = Harness(store, model)
    setup_start = len(model.calls)
    for agent, profile in PROFILES.items():
        await harness.publish(agent, profile)

    promise_turn = await harness.turn("draft", PROMISE)
    boundary_turn = await harness.turn("feedback", BOUNDARY)
    promise_id = promise_turn["event_id"]
    boundary_id = boundary_turn["event_id"]
    run = {
        "promise_event_id": promise_id,
        "boundary_event_id": boundary_id,
        "promise": matching_memory(store.memories("keeper"), "promise", promise_id),
        "boundary": matching_memory(store.memories("critic"), "boundary", boundary_id),
        "profiles": {agent: store.profile(agent) for agent in PROFILES},
        "seed_turns": [promise_turn, boundary_turn],
        "setup_calls": model.calls[setup_start:],
        "scenarios": {},
    }
    scenarios = run["scenarios"]

    for condition in CONDITIONS[:3]:
        db = snapshot(seed_path, directory / f"{condition}.sqlite3")
        if condition == "rewound":
            db.rewind(promise_id)
        check = Harness(db, model)
        start = len(model.calls)
        result = await check.turn(
            "later",
            PROBE,
            disabled={"keeper"} if condition == "skill_off" else frozenset(),
        )
        result["calls"] = model.calls[start:]
        result["promise_still_stored"] = matching_memory(db.memories("keeper"), "promise", promise_id) is not None
        result["boundary_still_stored"] = matching_memory(db.memories("critic"), "boundary", boundary_id) is not None
        scenarios[condition] = result
        show(condition, result)

    baseline_memories = {
        agent: decision["retrieved"] for agent, decision in scenarios["full"]["decisions"].items()
    }
    for condition, memories in (("prompt_only", {}), ("prompt_with_memory", baseline_memories)):
        start = len(model.calls)
        scenarios[condition] = await prompt_baseline(model, condition, memories)
        scenarios[condition]["calls"] = model.calls[start:]
        show(condition, scenarios[condition])

    rewind_full = snapshot(directory / "full.sqlite3", directory / "rewind-after-reply.sqlite3")
    reply_ids = {item["id"] for item in rewind_full.history("later") if item["evidence_id"] == promise_id}
    rewind_full.rewind(promise_id)
    surviving_ids = {item["id"] for item in rewind_full.history("later")}
    run["rewind_after_reply"] = {
        "sourced_replies_before": sorted(reply_ids),
        "surviving_sourced_replies": sorted(reply_ids & surviving_ids),
        "user_event_survives": scenarios["full"]["event_id"] in surviving_ids,
    }

    start = len(model.calls)
    full_store = Store(directory / "full.sqlite3")
    scenarios["invited"] = await Harness(full_store, model).turn("later", INVITATION, observe=False)
    scenarios["invited"]["calls"] = model.calls[start:]
    show("invited", scenarios["invited"])

    for condition, text in (("unrelated", UNRELATED), ("delivered", DELIVERY)):
        db = snapshot(seed_path, directory / f"{condition}.sqlite3")
        start = len(model.calls)
        result = await Harness(db, model).turn("later", text)
        result["calls"] = model.calls[start:]
        scenarios[condition] = result
        show(condition, result)
        if condition == "delivered":
            resolved = matching_memory(db.memories("keeper"), "promise", promise_id) is None
            db.rewind(result["event_id"])
            run["delivery_state"] = {
                "resolved": resolved,
                "restored_after_rewind": matching_memory(
                    db.memories("keeper"), "promise", promise_id
                ) is not None,
            }
    run["checks"] = evaluate(run)
    return run


def summarize(runs):
    return {
        "passed": all(all(run["checks"].values()) for run in runs),
        "checks_passed": {key: sum(run["checks"][key] for run in runs) for key in runs[0]["checks"]},
        "scenarios": {
            condition: {
                "median_elapsed_ms": round(median(run["scenarios"][condition]["elapsed_ms"] for run in runs)),
                "call_count": sum(len(run["scenarios"][condition]["calls"]) for run in runs),
                "prompt_tokens": sum(call.get("prompt_tokens") or 0 for run in runs
                                     for call in run["scenarios"][condition]["calls"]),
                "completion_tokens": sum(call.get("completion_tokens") or 0 for run in runs
                                         for call in run["scenarios"][condition]["calls"]),
            }
            for condition in CONDITIONS
        },
    }


async def run_experiment(repeats, output):
    runs = []
    output.parent.mkdir(parents=True, exist_ok=True)
    for number in range(1, repeats + 1):
        print(f"开始第 {number}/{repeats} 组对照", flush=True)
        with TemporaryDirectory(prefix="persona-exp-") as temp:
            runs.append(await trial(DeepSeek(), Path(temp)))
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "requested_model": "deepseek-flash",
            "requested_repeats": repeats,
            "completed_repeats": len(runs),
            "inputs": {"promise": PROMISE, "boundary": BOUNDARY, "probe": PROBE,
                       "invitation": INVITATION, "unrelated": UNRELATED, "delivery": DELIVERY},
            "runs": runs,
            "summary": summarize(runs),
        }
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        failed = [key for key, passed in runs[-1]["checks"].items() if not passed]
        print("  本组验收通过" if not failed else f"  未通过 {'、'.join(failed)}", flush=True)
    print(f"报告 {output.resolve()}", flush=True)
    return 0 if report["summary"]["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("exp/results/latest.json"))
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats 必须大于零")
    return asyncio.run(run_experiment(args.repeats, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
