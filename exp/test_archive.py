"""Deterministic tests for the experiment's archive and its role in a chat turn."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .archive import accessible_evidence, retrieve
from .harness import Harness, Store


TOPIC = "旧电影院"
QUERY = f"之前关于{TOPIC}聊过什么？"


def add_turn(store, session, text, *, reply=None, archive_source_ids=(), evidence=None):
    return store.save_turn(
        session,
        text,
        [event["id"] for event in store.history(session)],
        "keeper" if reply else None,
        reply,
        evidence,
        {},
        set(),
        archive_source_ids=archive_source_ids,
    )


class ArchiveStorageTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.store = Store(Path(directory) / "archive.sqlite3")

    def search(self, session, query=QUERY, *, recent_ids=()):
        with closing(self.store.connect()) as db:
            return retrieve(db, session, query, recent_ids=recent_ids)

    def can_read(self, session, *source_ids):
        with closing(self.store.connect()) as db:
            return accessible_evidence(db, session, source_ids)

    def test_same_session_history_is_readable_and_cross_session_is_opt_in(self):
        same = add_turn(self.store, "here", f"我想保留{TOPIC}的招牌。")
        other = add_turn(self.store, "elsewhere", f"{TOPIC}的放映机也值得留。")
        self.assertEqual([row["id"] for row in self.search("here")], [same])
        self.assertFalse(self.can_read("here", same, other))
        self.assertTrue(self.can_read("elsewhere", other))

        self.store.set_archive_sharing("elsewhere", True)
        self.assertEqual([row["id"] for row in self.search("here")], [same, other])
        self.assertTrue(self.can_read("here", same, other))
        self.assertEqual([row["id"] for row in self.search("here", recent_ids=[same])], [other])

        self.store.set_archive_sharing("elsewhere", False)
        self.assertEqual([row["id"] for row in self.search("here")], [same])
        self.assertFalse(self.can_read("here", other))

    def test_cjk_search_expands_a_hit_to_its_original_turn(self):
        unrelated = add_turn(self.store, "notes", "新体育馆改造的方案刚写完。")
        parent = add_turn(
            self.store, "notes", "我同意保留它的招牌。", reply=f"那就保留{TOPIC}的招牌吧。"
        )
        self.store.set_archive_sharing("notes", True)
        reply = self.store.history("notes")[-1]["id"]

        found = self.search("probe")
        self.assertEqual([row["id"] for row in found], [parent, reply])
        self.assertEqual(found[1]["turn_id"], parent)
        self.assertEqual([row["author"] for row in found], ["user", "keeper"])
        self.assertNotIn(unrelated, {row["id"] for row in found})

    def test_direct_archive_reply_is_hidden_when_sharing_is_revoked(self):
        source = add_turn(self.store, "source", f"{TOPIC}有一扇蓝色的门。")
        self.store.set_archive_sharing("source", True)
        question = add_turn(
            self.store, "current", "查一下门的颜色。", reply=f"{TOPIC}的门是蓝色的。",
            archive_source_ids=[source],
        )
        reply = self.store.history("current")[-1]["id"]
        self.store.set_archive_sharing("current", True)
        self.assertFalse(self.can_read("another", reply))
        self.assertNotIn(reply, {row["id"] for row in self.search("another")})

        self.store.set_archive_sharing("source", False)
        self.assertEqual([row["id"] for row in self.store.history("current")], [question])
        with self.assertRaises(RuntimeError):
            add_turn(
                self.store, "current", "再试一次。", reply="蓝色。",
                archive_source_ids=[source],
            )
        self.assertEqual([row["id"] for row in self.store.history("current")], [question])

    def test_removing_either_source_removes_the_multi_source_reply(self):
        for deleted_index in (0, 1):
            with self.subTest(deleted_index=deleted_index):
                sources = [
                    add_turn(self.store, f"old-{deleted_index}-{i}", f"{TOPIC}的原话 {i}")
                    for i in range(2)
                ]
                for i in range(2):
                    self.store.set_archive_sharing(f"old-{deleted_index}-{i}", True)
                session = f"now-{deleted_index}"
                question = add_turn(
                    self.store, session, "那两条旧话讲了什么？", reply="分别有两个想法。",
                    archive_source_ids=sources,
                )
                self.assertEqual(len(self.store.history(session)), 2)
                self.store.rewind(sources[deleted_index])
                self.assertEqual([row["id"] for row in self.store.history(session)], [question])
                self.assertFalse(self.can_read(session, sources[deleted_index]))
                self.assertTrue(self.can_read(session, sources[1 - deleted_index]))


class ScriptedArchiveModel:
    def __init__(self, source_ids, *, points=(), proposal=None, before_reply=None,
                 reply="我找到了旧会话里的原话。"):
        self.source_ids = source_ids
        self.points = list(points)
        self.proposal = proposal
        self.before_reply = before_reply
        self.reply = reply
        self.calls = []

    async def ask(self, stage, instruction, data, *, thinking=False):
        self.calls.append((stage, data))
        if stage == "decide":
            output = {
                "action": "speak", "reason": "回答历史问题", "uses_memory": False, "change": None,
            }
            if self.proposal and not (data.get("archive") or {}).get("evidence"):
                output["archive_query"] = self.proposal
            return output
        if stage == "archive_summarize":
            return {"points": self.points}
        if stage == "express":
            if self.before_reply:
                self.before_reply()
            return {"reply": self.reply, "archive_sources": self.source_ids}
        if stage == "check_reply":
            return {"grounded": True}
        raise AssertionError(f"Unexpected stage {stage}")


class ArchiveTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.store = Store(Path(directory) / "archive.sqlite3")
        self.store.publish("keeper", "拾荒者", {"voice": "自然", "skills": []})

    async def test_explicit_single_quote_reaches_action_and_reply_without_private_write(self):
        source = add_turn(self.store, "old", f"我想保留{TOPIC}的招牌。")
        self.store.set_archive_sharing("old", True)
        model = ScriptedArchiveModel([source])
        turn = await Harness(self.store, model).turn(
            "now", f"@拾荒者 {QUERY}", history_mode="lookup",
        )
        self.assertEqual(turn["short_term"], [])
        self.assertEqual(turn["archive"]["mode"], "explicit")
        self.assertEqual([event["id"] for event in turn["archive"]["evidence"]], [source])
        self.assertEqual(turn["archive"]["summary"], [])
        self.assertEqual(turn["archive"]["used_source_ids"], [source])
        self.assertEqual(turn["winner"], "keeper")
        self.assertEqual(self.store.memories("keeper"), [])
        stages = dict(model.calls)
        self.assertNotIn("archive_summarize", stages)
        for stage in ("decide", "express"):
            self.assertEqual([event["id"] for event in stages[stage]["archive"]["evidence"]], [source])
        self.store.rewind(source)
        self.assertEqual([event["id"] for event in self.store.history("now")], [turn["event_id"]])

    async def test_off_does_not_supply_shared_history(self):
        add_turn(self.store, "old", f"我想保留{TOPIC}的招牌。")
        self.store.set_archive_sharing("old", True)
        model = ScriptedArchiveModel([], reply="你指的是哪次聊天？")
        turn = await Harness(self.store, model).turn("now", f"@拾荒者 {QUERY}", history_mode="off")
        self.assertEqual(turn["archive"]["mode"], "off")
        self.assertEqual(turn["archive"]["evidence"], [])
        self.assertTrue(all(not (data.get("archive") or {}).get("evidence") for _, data in model.calls))
        self.assertIn("我这轮没查历史", turn["reply"])
        self.assertNotIn("express", dict(model.calls))

    async def test_summary_keeps_only_points_citing_the_shared_evidence(self):
        earlier = add_turn(self.store, "earlier", f"{TOPIC}的电气线路太老，我担心消防。")
        later = add_turn(self.store, "later", f"我改主意了，{TOPIC}的招牌还是应该保留。")
        private = add_turn(self.store, "private", f"{TOPIC}的票根也值得收藏。")
        for session in ("earlier", "later"):
            self.store.set_archive_sharing(session, True)
        supported = {"text": "先担心消防，后来想保留招牌。", "source_ids": [earlier, later]}
        model = ScriptedArchiveModel([earlier, later], points=[
            supported,
            {"text": "用户想收藏票根。", "source_ids": [private]},
            {"text": "没有来源的观察。", "source_ids": []},
        ])
        turn = await Harness(self.store, model).turn(
            "now", f"@拾荒者 {QUERY}", history_mode="lookup",
        )
        self.assertEqual(turn["archive"]["summary"], [supported])
        self.assertEqual(turn["archive"]["used_source_ids"], [earlier, later])
        summary_input = dict(model.calls)["archive_summarize"]
        self.assertEqual([event["id"] for event in summary_input["evidence"]], [earlier, later])
        self.assertEqual(self.store.memories("keeper"), [])

    async def test_unavailable_reply_citation_prevents_publication(self):
        shared = add_turn(self.store, "shared", f"我想保留{TOPIC}的招牌。")
        private = add_turn(self.store, "private", f"{TOPIC}的票根也值得收藏。")
        self.store.set_archive_sharing("shared", True)
        model = ScriptedArchiveModel([shared, private])
        turn = await Harness(self.store, model).turn(
            "now", f"@拾荒者 {QUERY}", history_mode="lookup",
        )
        self.assertEqual([event["id"] for event in turn["archive"]["evidence"]], [shared])
        self.assertEqual(turn["archive"]["used_source_ids"], [])
        self.assertIsNone(turn["winner"])
        self.assertTrue(turn["rejected_reply"])
        self.assertEqual([event["id"] for event in self.store.history("now")], [turn["event_id"]])

    async def test_inline_citation_must_match_the_declared_sources(self):
        source = add_turn(self.store, "old", f"我想保留{TOPIC}的招牌。")
        self.store.set_archive_sharing("old", True)
        model = ScriptedArchiveModel([source], reply="你当时想保留招牌 [M999]。")
        turn = await Harness(self.store, model).turn("now", f"@拾荒者 {QUERY}", history_mode="auto")
        self.assertEqual(turn["archive"]["mode"], "explicit")
        self.assertIsNone(turn["reply"])
        self.assertEqual(turn["archive"]["used_source_ids"], [])
        self.assertIn("[M999]", turn["rejected_reply"])

    async def test_mentions_of_past_are_not_automatically_a_request(self):
        source = add_turn(self.store, "old", f"我想保留{TOPIC}的招牌。")
        self.store.set_archive_sharing("old", True)
        model = ScriptedArchiveModel([])
        turn = await Harness(self.store, model).turn(
            "now", f"@拾荒者 上次说去看{TOPIC}的招牌，今天总算去了。", history_mode="auto",
        )
        self.assertEqual(turn["archive"]["mode"], "none")
        self.assertTrue(all(not data.get("archive") for _, data in model.calls))

    async def test_revocation_during_model_call_aborts_publication(self):
        source = add_turn(self.store, "old", f"我想保留{TOPIC}的招牌。")
        self.store.set_archive_sharing("old", True)
        model = ScriptedArchiveModel(
            [source], before_reply=lambda: self.store.set_archive_sharing("old", False),
        )
        with self.assertRaisesRegex(RuntimeError, "历史来源"):
            await Harness(self.store, model).turn(
                "now", f"@拾荒者 {QUERY}", history_mode="lookup",
            )
        self.assertEqual(self.store.history("now"), [])
        self.assertEqual([event["id"] for event in self.store.history("old")], [source])

    async def test_skill_lookup_uses_an_exact_cue_and_can_be_disabled(self):
        source = add_turn(self.store, "old", f"我想保留{TOPIC}的招牌。")
        self.store.set_archive_sharing("old", True)
        self.store.publish("keeper", "想起有证据的旧谈话", {"voice": "自然", "skills": [{
            "name": "查旧话", "source": "想起有证据的旧谈话", "attention": "", "recall": "",
            "decision": "", "reflect": "", "reads": [], "writes": [],
            "archive": "话题接回旧事时查一次历史，确定是否值得接话。",
        }]})
        message = "@拾荒者 那块招牌应该留着。"
        query = f"{TOPIC}招牌"
        model = ScriptedArchiveModel([source], proposal={"query": query, "cue": "那块招牌"})
        turn = await Harness(self.store, model).turn("now", message, history_mode="auto")
        self.assertEqual(turn["archive"]["mode"], "skill")
        self.assertEqual(turn["archive"]["query"], query)
        self.assertEqual(turn["archive"]["used_source_ids"], [source])
        self.assertEqual(sum(stage == "decide" for stage, _ in model.calls), 2)

        bad_cue = ScriptedArchiveModel([], proposal={"query": query, "cue": "蓝色招牌"})
        bad_turn = await Harness(self.store, bad_cue).turn("bad-cue", message, history_mode="auto")
        self.assertEqual(bad_turn["archive"]["mode"], "none")
        self.assertEqual(sum(stage == "decide" for stage, _ in bad_cue.calls), 1)

        disabled = ScriptedArchiveModel([], proposal={"query": query, "cue": "那块招牌"})
        disabled_turn = await Harness(self.store, disabled).turn(
            "disabled", message, history_mode="auto", disabled={"keeper"},
        )
        self.assertEqual(disabled_turn["archive"]["mode"], "none")
        self.assertEqual(disabled_turn["archive"]["evidence"], [])


if __name__ == "__main__":
    unittest.main()
