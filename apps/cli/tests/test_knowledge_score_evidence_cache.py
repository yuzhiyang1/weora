"""验证本机后台候选的闲置、目录授权和证据固定。"""

import hashlib
import json
import os
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge_score_evidence_cache as cache
from weora_cli.collab_client import Client, CollabError


class LocalScoreEvidenceCacheTests(unittest.TestCase):
    def test_idle_resume_and_retry_keep_frozen_scrubbed_evidence(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            client = Client("https://example.invalid", str(uuid4()), "fixture")
            project, session, subject = str(uuid4()), str(uuid4()), str(uuid4())
            cwd = root / "work"
            cwd.mkdir()
            codex = root / "codex"
            transcript = codex / "sessions" / f"rollout-fixture-{session}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("\n".join(json.dumps(row) for row in [
                {"type": "session_meta", "payload": {"id": session, "source": "cli", "cwd": str(cwd)}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "First evidence. token=synthetic_fixture_secret"},
                ]}},
            ]), encoding="utf-8")

            def bound(directory: Path) -> str | None:
                return project if directory.resolve() == cwd.resolve() else None

            with (
                patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(root / "device.json"), "CODEX_HOME": str(codex)}),
                patch.object(cache, "register_evidence", return_value=(subject, 1)) as register,
                patch.object(time, "time", return_value=50_000) as now,
            ):
                cache.note_finished(client, project, cwd, "attended", "codex", session, transcript)
                cache.freeze_ready(client, project, "attended", bound)
                register.assert_not_called()
                cache.note_resumed(client, "codex", session)
                now.return_value += 22_000
                cache.freeze_ready(client, project, "attended", bound)
                register.assert_not_called()
                cache.note_finished(client, project, cwd, "attended", "codex", session, transcript)
                now.return_value += 22_000
                cache.freeze_ready(client, project, "attended", bound)
                register.assert_called_once()
                registered = register.call_args.args[2]
                self.assertEqual(set(registered), {
                    "runtime", "subject_key", "digest", "run_mode", "captured_at", "recall_session_key",
                })
                self.assertEqual(registered["captured_at"], datetime.fromtimestamp(72_000, UTC).isoformat())
                self.assertEqual(registered["recall_session_key"], hashlib.sha256(session.encode()).hexdigest())
                [evidence] = cache.device_evidence(client, project, "attended", bound).values()
                self.assertEqual(evidence.key[:2], (subject, 1))
                self.assertNotIn("synthetic_fixture_secret", str(evidence.load()))
                transcript.write_text("New source.", encoding="utf-8")
                self.assertIn("First evidence.", str(evidence.load()))
                self.assertFalse(cache.device_evidence(client, project, "attended", lambda _: None))
                evidence.turns_path.write_text('{"turns":["tampered"]}', encoding="utf-8")
                with self.assertRaisesRegex(CollabError, "指纹不一致"):
                    evidence.load()
                orphan = evidence.turns_path.with_name(f"{uuid4()}.1.{'a' * 64}.turns.json")
                orphan.write_text("{}", encoding="utf-8")
                os.utime(orphan, (1000, 1000))
                now.return_value += 15 * 86400
                self.assertFalse(cache.device_evidence(client, project, "attended", bound))
                self.assertFalse(orphan.exists())
                self.assertFalse(evidence.turns_path.exists())
                self.assertTrue(transcript.exists())
