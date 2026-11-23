import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient

from exp.harness import persona_source_hash
from thought_app.app import app
from thought_app.database import connect
from thought_app.evolution import remember_evolution
from thought_app.journal import add_message


class EvolutionPathTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "THOUGHT_DB": str(Path(temp) / "traits.sqlite3"), "DEEPSEEK_API_KEY": "test",
        }))
        self.client = self.enterContext(TestClient(app))
        self.agent = next(row for row in self.get("/api/config/agents")["agents"] if row["name"] == "拾荒者")
        self.path = f"/api/config/bots/{self.agent['id']}/evolution/path"
        self.session_id = self.client.post("/api/sessions", json={"title": "灯塔"}).json()["id"]
        self.description = "聊到灯塔时，我喜欢先请对方描述眼前的光。"
        with connect() as db:
            source = add_message(db, self.session_id, "assistant", self.description,
                                 bot_id=self.agent["id"], bot_name=self.agent["name"])
            self.note = remember_evolution(db, self.agent["id"], source["id"], {
                "kind": "habit", "text": self.description, "keywords": ["灯塔"], "evidence": self.description,
                "skill": {"name": "留意灯光", "phase": "attention", "instruction": "注意灯光的具体变化。"},
            }, seen_note_ids=set(), base_hash=persona_source_hash(self.agent["prompt"]))
            self.assertIsNotNone(self.note)

    def get(self, path):
        response = self.client.get(path)
        self.assertTrue(response.is_success, response.text)
        return response.json()

    def intro(self):
        return next(bot for bot in self.get("/api/config")["bots"] if bot["id"] == self.agent["id"])

    def test_path_and_role_introduction_follow_authored_base(self):
        path = self.get(self.path)
        self.assertEqual(path["base_description"], self.agent["prompt"])
        self.assertEqual(path["traits"][0]["description"], self.description)
        self.assertEqual(path["events"][0]["skill"]["phase"], "attention")
        self.assertEqual(self.intro()["evolved_preferences"], [self.description])
        result = self.client.patch(f"/api/config/agents/{self.agent['id']}", json={"prompt": "改为专注城市道路的历史。"})
        self.assertTrue(result.is_success, result.text)
        path = self.get(self.path)
        self.assertEqual(path["events"][0]["status"], "previous_base")
        self.assertEqual(path["traits"], [])
        self.assertEqual(self.intro()["evolved_preferences"], [])

    def test_backup_preserves_traits_and_accepts_previous_note_only_format(self):
        export = self.client.get("/api/backup/export")
        self.assertTrue(export.is_success)
        expected = self.get(self.path)
        removed = self.client.delete(f"/api/config/bots/{self.agent['id']}/evolution/{self.note['id']}")
        self.assertTrue(removed.is_success, removed.text)
        restored = self.client.post("/api/backup/import", content=export.content, headers={"Content-Type": "application/zip"})
        self.assertTrue(restored.is_success, restored.text)
        self.assertEqual(self.get(self.path), expected)

        with ZipFile(io.BytesIO(export.content)) as archive:
            previous = json.loads(archive.read("journal.json"))
        del previous["tables"]["agent_evolution_traits"]
        old_backup = io.BytesIO()
        with ZipFile(old_backup, "w") as archive:
            archive.writestr("journal.json", json.dumps(previous))
        restored = self.client.post("/api/backup/import", content=old_backup.getvalue(),
                                    headers={"Content-Type": "application/zip"})
        self.assertTrue(restored.is_success, restored.text)
        path = self.get(self.path)
        self.assertEqual(path["traits"], [])
        self.assertEqual(path["events"][0]["text"], self.description)
        self.assertIsNone(path["events"][0]["skill"])
