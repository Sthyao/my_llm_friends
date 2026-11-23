import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from exp.harness import persona_source_hash
from thought_app.app import app
from thought_app.database import connect
from thought_app.evolution import remember_evolution
from thought_app.journal import add_message
from thought_app.prompts import load_prompt


class BuiltinReplyStyleTests(unittest.TestCase):
    def test_old_default_is_migrated_on_startup_and_import_with_evolved_preference(self):
        temp = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "THOUGHT_DB": str(Path(temp) / "test.sqlite3"), "DEEPSEEK_API_KEY": "test",
        }))
        current = load_prompt("personas/keeper")
        legacy = current.rsplit("\n\n", 1)[0] + (
            "\n\n像并排散步时说话，短而具体。一轮只挑一个真正想听下去的细节。"
            "朋友想先自己消化时就一起安静一会儿。禁止虚构你们共同经历过的事，"
            "也别用一句漂亮话替朋友归纳整个人。"
        )
        custom = legacy + "\n我也喜欢聊过去的街区。"
        description = "聊到旧桥时，我习惯先问它有什么声音。"
        legacy_hash = persona_source_hash(legacy)

        with TestClient(app) as client:
            agent = next(item for item in client.get("/api/config/agents").json()["agents"]
                         if item["name"] == "拾荒者")
            bot_id, persona_id = agent["id"], agent["persona_id"]
            session_id = client.post("/api/sessions", json={"title": "旧桥"}).json()["id"]
            with connect() as db:
                db.execute("UPDATE personas SET prompt = ? WHERE id = ?", (legacy, persona_id))
                custom_id = db.execute(
                    "INSERT INTO personas (name, prompt) VALUES ('自定义拾荒者', ?)", (custom,),
                ).lastrowid
                db.execute("INSERT INTO agent_persona_programs VALUES (?, ?, 1, ?, '测试模型')", (
                    persona_id, legacy_hash, json.dumps({"skills": [], "voice": "简短回复。"}),
                ))
                message = add_message(db, session_id, "assistant", description, bot_id=bot_id, bot_name=agent["name"])
                self.assertIsNotNone(remember_evolution(db, bot_id, message["id"], {
                    "kind": "habit", "text": description, "keywords": ["旧桥"], "evidence": description,
                    "skill": {"name": "旧桥的声音", "phase": "attention", "instruction": "留意旧桥发出的声音。"},
                }, seen_note_ids=set(), base_hash=legacy_hash))
            backup = client.get("/api/backup/export")
            self.assertTrue(backup.is_success)

        with TestClient(app) as client:
            def verify_migrated():
                path = client.get(f"/api/config/bots/{bot_id}/evolution/path").json()
                self.assertEqual(path["base_description"], current)
                self.assertEqual(path["traits"][0]["description"], description)
                self.assertIsNone(path["base_version"])
                with connect() as db:
                    self.assertEqual(db.execute("SELECT prompt FROM personas WHERE id = ?", (custom_id,)).fetchone()[0],
                                     custom)

            verify_migrated()
            restored = client.post("/api/backup/import", content=backup.content,
                                   headers={"Content-Type": "application/zip"})
            self.assertTrue(restored.is_success, restored.text)
            verify_migrated()
