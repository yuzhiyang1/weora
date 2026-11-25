"""用真实 Git 工作树验证只在启动时记录可重放的起点。"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli.collab_client import Client
from weora_cli.commands import JsonValue
from weora_cli.knowledge_session_origin import observe_start, start_metadata, subject_key


@unittest.skipUnless(shutil.which("git"), "需要实际 Git 命令")
class SessionOriginTests(unittest.TestCase):
    def test_start_is_captured_once_without_transmitting_paths_or_credentials(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            checkout = directory / "checkout"
            checkout.mkdir()

            def git(*args: str) -> str:
                return subprocess.check_output(["git", "-C", str(checkout), *args], text=True).strip()

            git("init", "-q")
            git("config", "user.email", "test@example.test")
            git("config", "user.name", "Tester")
            git("remote", "add", "origin", "git@github.com:team/repo.git")
            tracked = checkout / "tracked.txt"
            tracked.write_text("first", encoding="utf-8")
            git("add", "tracked.txt")
            git("commit", "-qm", "first")
            first = git("rev-parse", "HEAD")
            (checkout / "untracked.txt").write_text("untracked", encoding="utf-8")
            client = Client("https://example.test", str(uuid4()), "device-token")
            session = uuid4()
            project = str(uuid4())
            event: dict[str, JsonValue] = {
                "session_id": str(session), "hook_event_name": "SessionStart", "source": "startup",
            }
            with patch.dict(os.environ, {
                "WEORA_COLLAB_CONFIG": str(directory / "config.json"), "CODEX_HOME": str(directory / "codex"),
            }):
                key = subject_key("codex", session)
                observe_start(client, checkout, "codex", event | {"source": "resume"})
                self.assertEqual(start_metadata(client, project, "codex", key), {})
                observe_start(client, checkout, "codex", event)
                original = start_metadata(client, project, "codex", key)
                record = original["git_start"]
                self.assertIsInstance(record, dict)
                assert isinstance(record, dict)
                self.assertEqual(record["commit"], first)
                self.assertFalse(record["tracked_changes"])
                self.assertEqual(record["repository"], "ssh://git@github.com/team/repo.git")
                self.assertEqual(start_metadata(client, None, "codex", key), {})

                tracked.write_text("changed", encoding="utf-8")
                dirty = uuid4()
                observe_start(client, checkout, "codex", event | {"session_id": str(dirty)})
                recorded = start_metadata(client, project, "codex", subject_key("codex", dirty))["git_start"]
                assert isinstance(recorded, dict)
                self.assertTrue(recorded["tracked_changes"])
                git("commit", "-qam", "second")
                observe_start(client, checkout, "codex", event)
                self.assertEqual(start_metadata(client, project, "codex", key), original)

                missing = uuid4()
                missing_event = event | {"session_id": str(missing)}
                git("remote", "set-url", "origin", "https://user:secret@github.com/team/repo.git")
                observe_start(client, checkout, "codex", missing_event)
                git("remote", "set-url", "origin", "https://github.com/team/repo.git")
                observe_start(client, checkout, "codex", missing_event)
                self.assertEqual(start_metadata(client, project, "codex", subject_key("codex", missing)), {})
