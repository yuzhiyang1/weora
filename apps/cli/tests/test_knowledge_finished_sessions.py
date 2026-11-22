"""验证已结束会话的发现、项目绑定、归档与授权变化。"""

import argparse
import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge
from weora_cli import knowledge_finished_sessions as archive
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue


class FinishedSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {
            "WEORA_COLLAB_CONFIG": str(self.root / "device.json"), "CODEX_HOME": str(self.root / "codex"),
            "WEORA_ARCHIVE_DISABLED": "",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = Client("https://example.invalid", str(uuid4()), "test-device")
        self.project = str(uuid4())
        self.session = str(uuid4())
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.path = self.root / "codex" / "sessions" / f"rollout-proof-{self.session}.jsonl"
        self.path.parent.mkdir(parents=True)
        self._transcript(self.cwd)
        self.team = {"attended": "auto", "unattended": "off"}
        self.sent: list[dict[str, JsonValue]] = []
        api = patch.object(archive, "call", side_effect=self._api)
        api.start()
        self.addCleanup(api.stop)

    def _api(
        self, _client: Client, project: str | None, suffix: str = "", *, method: str = "GET",
        payload: dict[str, JsonValue] | None = None, **_params: JsonValue,
    ) -> dict[str, JsonValue]:
        if suffix == "/settings/session-archive":
            return {mode: {"action": action, "source": "workspace"} for mode, action in self.team.items()}
        if suffix == "/shared-sessions":
            self.assertEqual((project, method), (self.project, "POST"))
            assert payload is not None
            self.sent.append(payload)
            return {"id": "published", "revision": 1}
        self.assertEqual((project, suffix), (self.project, ""))
        return {"items": []}

    def _transcript(self, cwd: Path) -> None:
        self.path.write_text("\n".join(json.dumps(row) for row in [
            {"type": "session_meta", "payload": {"id": self.session, "source": "cli", "cwd": str(cwd)}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Finished. token=fixture_secret_material"}]}},
        ]), encoding="utf-8")

    def _event(self, name: str, *, headless: bool = False, session: str | None = None) -> str:
        return archive.observe(self.client, self.cwd, "codex", {
            "hook_event_name": name, "is_headless": headless, "session_id": session or self.session,
            "transcript_path": str(self.path),
        })

    def test_attended_waits_for_idle_and_skips_resumed_or_changed_transcripts(self) -> None:
        archive.configure(self.client, self.cwd, "attended", "auto", workspace=False, project=self.project)
        with patch.object(time, "time", return_value=1000):
            self._event("Stop", headless=True)
        with patch.object(time, "time", return_value=25000):
            self.assertEqual(archive.finished(self.client, self.cwd, 5, apply=True)["items"], [])
        with patch.object(time, "time", return_value=1000):
            self._event("Stop")
        with patch.object(time, "time", return_value=2000):
            self.assertEqual(archive.finished(self.client, self.cwd, 5, apply=True)["items"], [])
        with patch.object(time, "time", return_value=25000):
            with patch.object(subprocess, "Popen") as spawn:
                self.assertEqual(self._event("SessionStart", session=str(uuid4())), "")
                self.assertIn("--apply", spawn.call_args.args[0])
            expected = {"session": self.session, "project_id": self.project, "runtime": "codex",
                        "stopped_at": 1000, "action": "auto"}
            self.assertEqual(archive.finished(self.client, self.cwd, 5, apply=False)["items"], [expected])
            self._event("UserPromptSubmit")
            self.assertEqual(archive.finished(self.client, self.cwd, 5, apply=True)["items"], [])

        with patch.object(time, "time", return_value=1000):
            self._event("Stop")
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        with patch.object(time, "time", return_value=25000):
            self.assertEqual(archive.finished(self.client, self.cwd, 5, apply=True)["items"], [])
        with patch.object(time, "time", return_value=1000):
            self._event("Stop")
        with patch.object(time, "time", return_value=25000):
            self.team["attended"] = "off"
            with self.assertRaisesRegex(CollabError, "未允许"):
                archive.finished(self.client, self.cwd, 5, apply=True)
            self.assertEqual(self.sent, [])
            self.team["attended"] = "auto"
            self.assertIn("published", str(archive.finished(self.client, self.cwd, 5, apply=True)))
            self.assertEqual(archive.finished(self.client, self.cwd, 5, apply=True)["items"], [])
        self.assertEqual(self.sent[0]["automatic_mode"], "attended")
        self.assertNotIn("fixture_secret_material", str(self.sent))

    def test_unattended_drain_uses_its_own_policy_and_checks_native_directory(self) -> None:
        with self.assertRaisesRegex(CollabError, "未允许"):
            archive.configure(self.client, self.cwd, "unattended", "auto", workspace=False, project=self.project)
        archive.configure(self.client, self.cwd, "unattended", "ask", workspace=False, project=self.project)
        parser = argparse.ArgumentParser()
        knowledge.arguments(parser)
        cmd = parser.parse_args(["finished-sessions", "--drain", self.session, "--runtime", "codex", "--apply"])
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(Path, "cwd", return_value=self.cwd),
        ):
            self.assertIn("ask", str(as_object(knowledge.execute(cmd))["items"]))
            self.assertEqual(self.sent, [])
            self.team["unattended"] = "auto"
            archive.configure(self.client, self.cwd, "unattended", "auto", workspace=False, project=self.project)
            self._transcript(self.root / "unbound")
            with self.assertRaisesRegex(CollabError, "目录不一致"):
                knowledge.execute(cmd)
            self._transcript(self.cwd)
            self.assertIn("published", str(knowledge.execute(cmd)))
        self.assertEqual(self.sent[0]["automatic_mode"], "unattended")
