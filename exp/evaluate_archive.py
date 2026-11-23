"""Run paired history archive experiments with the official DeepSeek API."""

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from .evaluate import snapshot
from .harness import Harness, PROFILES, Store
from .model import DeepSeek


EARLIER = "我一开始赞成拆掉旧电影院。那时最担心的是消防通道太窄，继续用风险太大。"
LATER = "看了改造图之后，我改变了对旧电影院的看法。我想留下旧电影院的门厅做社区放映，再把新的消防出口做出来。"
PRIVATE = "我对旧电影院还有个私下的备选意见，只留招牌，把门厅改成仓库。"
DISTRACTOR = "我看了新菜市场的改造图，最喜欢保留原来的门厅。"
LOCAL_SOURCE = "白松桥修完后，我最喜欢的是桥头三盏橘灯。"
LOCAL_FILLERS = (
    "早上蒸了红薯。",
    "下午读完了十页书。",
    "最近跑步比之前早一点。",
    "水杯盖弄丢了。",
    "猫睡在毯子上。",
    "阳台种了一盆葱。",
    "袜子洗好了。",
    "明天想做番茄炒蛋。",
)
QUESTION = "@拾荒者 翻一下之前的旧聊天，我对旧电影院的看法前后怎么变的？分别是为什么？"
LOCAL_QUESTION = "@拾荒者 查一下这场之前的聊天，我说喜欢白松桥的哪个细节？"
CASUAL = "@拾荒者 晚饭吃完了，先歇会儿。"
INITIATIVE = "@拾荒者 今天路过旧电影院的放映厅，心里还挺高兴的。"
ARCHIVE_SKILL = "当用户提及旧电影院放映厅时，先主动翻查以前的聊天记录，找出改建细节，再决定怎么接话。说话像自然聊天。"
CONDITIONS = ("full", "off", "unshared", "rewound", "same_session", "casual", "skill_initiative")


def add_event(store, session, text):
    previous = [item["id"] for item in store.history(session)]
    return store.save_turn(session, text, previous, None, None, None, {}, set())


def seed(store):
    ids = {
        "earlier": add_event(store, "早期讨论", EARLIER),
        "later": add_event(store, "看完图纸", LATER),
        "private": add_event(store, "私密草稿", PRIVATE),
        "distractor": add_event(store, "另一个项目", DISTRACTOR),
        "local": add_event(store, "白松桥随记", LOCAL_SOURCE),
    }
    for session in ("早期讨论", "看完图纸", "另一个项目"):
        store.set_archive_sharing(session, True)
    for line in LOCAL_FILLERS:
        add_event(store, "白松桥随记", line)
    return ids


async def run_turn(store, model, condition):
    session, text, mode = {
        "full": ("以后再聊", QUESTION, "auto"),
        "off": ("以后再聊", QUESTION, "off"),
        "unshared": ("以后再聊", QUESTION, "lookup"),
        "rewound": ("以后再聊", QUESTION, "lookup"),
        "same_session": ("白松桥随记", LOCAL_QUESTION, "lookup"),
        "casual": ("晚饭后", CASUAL, "auto"),
        "skill_initiative": ("碰到电影院", INITIATIVE, "auto"),
    }[condition]
    started = len(model.calls)
    result = await Harness(store, model).turn(session, text, history_mode=mode, observe=False)
    return {**result, "calls": model.calls[started:]}


def archive_ids(turn):
    archive = turn.get("archive") or {}
    evidence = {record["id"] for record in archive.get("evidence", [])}
    used = set(archive.get("used_source_ids", []))
    summary = {
        source_id
        for point in archive.get("summary", [])
        for source_id in point["source_ids"]
    }
    return evidence, used, summary


def checks(run):
    ids = run["fixtures"]
    scenarios = run["scenarios"]
    full, off, unshared, rewound, local, casual = (scenarios[name] for name in CONDITIONS[:6])
    important = {ids["earlier"], ids["later"]}
    evidence, used, summary = archive_ids(full)
    seen = {
        name: {item["id"] for item in turn["short_term"]} for name, turn in scenarios.items()
    }
    unshared_evidence, unshared_used, unshared_summary = archive_ids(unshared)
    rewound_evidence, rewound_used, rewound_summary = archive_ids(rewound)
    local_evidence, local_used, local_summary = archive_ids(local)
    order = [item["id"] for item in full["archive"]["evidence"]]
    summary_order = [min(point["source_ids"]) for point in full["archive"]["summary"]]
    return {
        "private_memory_empty": run["private_memory_empty"],
        "new_session_short_term_empty": not seen["full"] and not seen["off"],
        "auto_finds_both_originals": full["archive"]["mode"] == "explicit" and important <= evidence,
        "summary_covers_both_originals": important <= summary <= evidence,
        "evidence_and_summary_in_source_order": order == sorted(order) and summary_order == sorted(summary_order),
        "reply_cites_both_originals": bool(full["reply"]) and important <= used <= evidence,
        "different_topic_not_cited": ids["distractor"] not in (used | summary),
        "private_session_never_enters_evidence": all(
            ids["private"] not in set.union(*archive_ids(turn)) for turn in scenarios.values()
        ),
        "disabled_archive_has_no_evidence": not any(archive_ids(off)),
        "unshared_source_disappears": (
            ids["earlier"] in unshared_evidence
            and ids["later"] not in (unshared_evidence | unshared_used | unshared_summary)
        ),
        "removed_source_disappears": (
            ids["earlier"] in rewound_evidence
            and ids["later"] not in (rewound_evidence | rewound_used | rewound_summary)
        ),
        "rewind_retracts_cited_reply_only": (
            bool(run["rewind_reply_ids"])
            and not run["rewind_reply_ids_after"]
            and run["rewind_question_survived"]
        ),
        "same_session_finds_older_than_window": (
            ids["local"] not in seen["same_session"]
            and ids["local"] in local_evidence
            and ids["local"] in local_used
        ),
        "single_episode_uses_raw_evidence": not local_summary,
        "casual_turn_skips_archive": casual["archive"]["mode"] == "none" and not any(archive_ids(casual)),
    }


async def trial(model, directory):
    seed_path = directory / "seed.sqlite3"
    store = Store(seed_path)
    initial = len(model.calls)
    await Harness(store, model).publish("keeper", PROFILES["keeper"])
    ids = seed(store)
    run = {
        "fixtures": ids,
        "private_memory_empty": all(not store.memories(agent) for agent in PROFILES),
        "setup_calls": model.calls[initial:],
        "scenarios": {},
    }
    for condition in CONDITIONS:
        source_path = directory / "full.sqlite3" if condition == "rewound" else seed_path
        db = snapshot(source_path, directory / f"{condition}.sqlite3")
        if condition == "unshared":
            db.set_archive_sharing("看完图纸", False)
        if condition == "skill_initiative":
            started = len(model.calls)
            await Harness(db, model).publish("keeper", ARCHIVE_SKILL)
            run["initiative_profile"] = db.profile("keeper")
            run["initiative_compile_calls"] = model.calls[started:]
        if condition == "rewound":
            original = run["scenarios"]["full"]
            before = db.history("以后再聊")
            reply_ids = {item["id"] for item in before if item["author"] != "user"}
            db.rewind(ids["later"])
            after_ids = {item["id"] for item in db.history("以后再聊")}
            run["rewind_reply_ids"] = sorted(reply_ids)
            run["rewind_reply_ids_after"] = sorted(reply_ids & after_ids)
            run["rewind_question_survived"] = original["event_id"] in after_ids
            db = snapshot(seed_path, directory / "rewound-fresh.sqlite3")
            db.rewind(ids["later"])
        turn = await run_turn(db, model, condition)
        run["scenarios"][condition] = turn
        _, used, _ = archive_ids(turn)
        print(f"  {condition} · 引用 {sorted(used)} · {turn['reply'] or '无发言'}", flush=True)
        if condition == "skill_initiative":
            run["skill_initiative_observed"] = turn["archive"]["mode"] == "skill" and bool(archive_ids(turn)[0])
    run["checks"] = checks(run)
    return run


def summarize(runs):
    return {
        "passed": all(all(run["checks"].values()) for run in runs),
        "checks_passed": {
            name: sum(run["checks"][name] for run in runs) for name in runs[0]["checks"]
        },
        "skill_initiative_observed": sum(run["skill_initiative_observed"] for run in runs),
        "scenarios": {
            name: {
                "median_elapsed_ms": round(median(
                    run["scenarios"][name]["elapsed_ms"] for run in runs
                )),
                "call_count": sum(len(run["scenarios"][name]["calls"]) for run in runs),
            }
            for name in CONDITIONS
        },
    }


async def run_experiment(repeats, output):
    runs = []
    output.parent.mkdir(parents=True, exist_ok=True)
    for number in range(1, repeats + 1):
        print(f"开始第 {number}/{repeats} 组历史库对照", flush=True)
        with TemporaryDirectory(prefix="archive-exp-") as directory:
            runs.append(await trial(DeepSeek(), Path(directory)))
        summary = summarize(runs)
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "requested_model": DeepSeek.model,
            "requested_repeats": repeats,
            "completed_repeats": len(runs),
            "inputs": {
                "earlier": EARLIER,
                "later": LATER,
                "private": PRIVATE,
                "distractor": DISTRACTOR,
                "question": QUESTION,
                "same_session_source": LOCAL_SOURCE,
                "same_session_question": LOCAL_QUESTION,
                "casual": CASUAL,
                "initiative": INITIATIVE,
                "initiative_skill": ARCHIVE_SKILL,
            },
            "runs": runs,
            "summary": summary,
        }
        pending = output.with_suffix(output.suffix + ".tmp")
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pending.replace(output)
        failed = [name for name, passed in runs[-1]["checks"].items() if not passed]
        print("  本组通过" if not failed else f"  未通过 {'、'.join(failed)}", flush=True)
    print(f"报告 {output.resolve()}", flush=True)
    return 0 if summary["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("exp/results/archive-latest.json"))
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats 必须大于零")
    return asyncio.run(run_experiment(args.repeats, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
