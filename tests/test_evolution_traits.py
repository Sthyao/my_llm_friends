import sqlite3
import unittest

from exp.harness import persona_source_hash
from thought_app.agent_store import init_agent_store, set_archive_shared, store_agent_sources
from thought_app.config import init_config
from thought_app.database import init_journal
from thought_app.evolution import active_traits, get_evolution, init_evolution, remember_evolution
from thought_app.journal import add_message, timestamp


class EvolutionTraitTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        init_config(self.db)
        init_journal(self.db)
        init_agent_store(self.db)
        init_evolution(self.db)
        bot = self.db.execute("""
            SELECT b.id, p.prompt FROM bots b JOIN personas p ON p.id = b.persona_id
            WHERE b.role_mode = 'friend' ORDER BY b.id LIMIT 1
        """).fetchone()
        self.bot = bot["id"]
        self.base = persona_source_hash(bot["prompt"])
        self.session = self.new_session()
        self.evidence = "我喜欢先把纸张摊开再说。"
        self.skill = {"name": "留意纸张", "phase": "attention", "instruction": "先留意纸张的尺寸。"}

    def new_session(self):
        return self.db.execute("INSERT INTO sessions(title,created_at,updated_at) VALUES (?,?,?)",
                               ("手稿", timestamp(), timestamp())).lastrowid

    def record(self, text, skill, *, replaces=None, base=None, seen=()):
        message = add_message(self.db, self.session, "assistant", self.evidence, bot_id=self.bot)
        proposal = {"kind": "habit", "text": text, "evidence": self.evidence, "keywords": ["纸张"],
                    "replaces_id": replaces, "skill": skill}
        return remember_evolution(self.db, self.bot, message["id"], proposal,
                                  base_hash=self.base if base is None else base, seen_note_ids=seen)

    def traits(self):
        return active_traits(self.db, self.bot, self.base)

    def test_only_valid_typed_revisions_can_supersede_an_existing_skill(self):
        description = "我倾向先了解纸张。"
        first = self.record(description, self.skill)
        old = first["id"]
        revised = {**self.skill, "instruction": "先问对方用哪种纸张。"}
        self.assertIsNone(self.record(description, revised, replaces=old))
        self.assertIsNone(self.record(description, self.skill, replaces=old, seen={old}))
        self.assertIsNone(self.record("换个话题聊聊。", None, replaces=old, seen={old}))
        self.assertIsNone(self.record(description, revised, replaces=old, seen={old}, base="outdated"))
        second = self.record(description, revised, replaces=old, seen={old})
        self.assertEqual([row["instruction"] for row in self.traits()], [revised["instruction"]])
        # Deleting the source of a newer skill-only revision restores the old instruction.
        self.db.execute("DELETE FROM messages WHERE id = ?", (second["source_message_id"],))
        self.assertEqual([row["note_id"] for row in self.traits()], [old])
        self.assertIsNotNone(self.record("我倾向先了解颜料。", {**self.skill, "phase": "tools"}))
        self.assertEqual([row["note_id"] for row in self.traits()], [old])

    def test_hiding_archive_evidence_removes_skill_from_public_description_and_recall(self):
        archive = self.new_session()
        source = add_message(self.db, archive, "user", "谈谈纸张颜色。")
        set_archive_shared(self.db, archive, True)
        note = self.record("我偏爱浅色纸张。", self.skill)
        store_agent_sources(self.db, note["source_message_id"], {source["id"]})
        self.assertEqual([row["note_id"] for row in self.traits()], [note["id"]])
        self.assertEqual(get_evolution(self.db, self.bot, "纸张", base_hash=self.base)["notes"][0]["skill"],
                         self.skill)
        self.assertFalse(get_evolution(self.db, self.bot, "纸张", base_hash="another")["notes"])
        set_archive_shared(self.db, archive, False)
        self.assertEqual(self.traits(), [])
        self.assertEqual(get_evolution(self.db, self.bot, "纸张", base_hash=self.base)["notes"], [])
