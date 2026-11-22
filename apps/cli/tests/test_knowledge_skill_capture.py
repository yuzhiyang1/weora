"""通过原生 Hook 固定已安装 Skill 的版本、范围与描述。"""

import json
import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID, uuid4

from weora_cli import knowledge_finished_sessions, knowledge_transcript, skill_adapters
from weora_cli.collab_client import Client, config_path, write_json
from weora_cli.knowledge_session_origin import subject_key
from weora_cli.knowledge_skill_capture import skill_capture
from weora_cli.skill_local import database, digest


class SessionSkillCaptureTests(unittest.TestCase):
    def test_hook_freezes_verified_package_history_for_current_project(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            home = root / "client-home"
            work = root / "checkout"
            tools = work / ".agents" / "skills"
            checked = tools / "release-review" / "SKILL.md"
            checked.parent.mkdir(parents=True)
            ignored = tools / "personal-note" / "SKILL.md"
            ignored.parent.mkdir()
            ignored.write_text("---\ndescription: Personal copy.\n---\n", encoding="utf-8")
            source = b"---\nname: release-review\ndescription: Check the release.\n---\n# Review\n"
            checked.write_bytes(source)
            workspace, project, slot, device = (str(uuid4()) for _ in range(4))
            first, second = str(uuid4()), str(uuid4())
            client = Client("https://example.invalid", workspace, "fixture")
            native = uuid4()
            key = subject_key("codex", native)
            with (
                patch.dict(os.environ, {
                    "WEORA_COLLAB_CONFIG": str(root / "device.json"), "WEORA_SKILL_STATE": str(root / "skill-state"),
                    "WEORA_SCORING_DISABLED": "",
                }),
                patch("pathlib.Path.home", return_value=root / "home"),
                patch.object(skill_adapters, "tool_home", return_value=home),
                patch("weora_cli.skill_local.tool_home", return_value=home),
                patch("weora_cli.skill_delivery_capture.tool_home", return_value=home),
                patch.object(knowledge_finished_sessions, "start_scoring"),
            ):
                write_json(config_path(), {"base": client.base, "workspace": workspace, "device_id": device})
                with database() as db:
                    db.execute("INSERT INTO binding VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                        "fixture", str(work), "codex", "project", str(home), client.base,
                        workspace, device, None, 1, project,
                    ))
                    db.execute("INSERT INTO installation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                        "fixture", "release-review", str(checked.parent), slot, first, str(uuid4()), "1.0.0",
                        "fixture-digest", json.dumps({"SKILL.md": digest(source)}), 0,
                    ))
                    db.commit()
                event = {"session_id": str(native), "hook_event_name": "SessionStart"}
                knowledge_finished_sessions.observe(client, work, "codex", event)
                old_cutoff = datetime.now(UTC).isoformat()
                knowledge_finished_sessions.observe(
                    client, work, "codex", event | {"hook_event_name": "UserPromptSubmit"},
                )
                pinned = skill_capture(client, project, "codex", key, old_cutoff)["skill_capture"]
                self.assertEqual([row["generation"] for row in pinned["installations"]], [first])

                next_body = source.replace(b"Check the release.", b"Review the approved release.")
                checked.write_bytes(next_body)
                with database() as db:
                    db.execute("UPDATE installation SET generation=?, version_id=?, files=?", (
                        second, str(uuid4()), json.dumps({"SKILL.md": digest(next_body)}),
                    ))
                    db.commit()
                knowledge_finished_sessions.observe(
                    client, work, "codex", event | {"hook_event_name": "UserPromptSubmit"},
                )
                new_cutoff = datetime.now(UTC).isoformat()
                earlier = skill_capture(client, project, "codex", key, old_cutoff)["skill_capture"]
                later = skill_capture(client, project, "codex", key, new_cutoff)["skill_capture"]
                self.assertEqual([row["generation"] for row in earlier["installations"]], [first])
                self.assertEqual([row["generation"] for row in later["installations"]], [first, second])
                self.assertEqual([row["description"] for row in later["installations"]],
                                 ["Check the release.", "Review the approved release."])
                self.assertEqual(len(later["usage_session"]), 64)
                self.assertEqual(skill_capture(client, str(uuid4()), "codex", key, new_cutoff), {})
                self.assertEqual(skill_capture(client, None, "codex", key, new_cutoff), {})

                synthetic = {
                    "session": str(native), "runtime": "codex", "captured_at": new_cutoff, "title": "test",
                    "turns": [{"role": "assistant", "text": "Completed."}],
                }
                with (
                    patch.object(knowledge_transcript, "captured_snapshot", return_value=synthetic),
                    patch.object(knowledge_transcript, "call", return_value={"id": str(UUID(int=1))}) as server,
                ):
                    preview = knowledge_transcript.share_session(
                        client, project, "codex", str(native), None, None, None,
                    )
                    self.assertEqual(preview["skill_count"], 2)
                    self.assertNotIn("skill_capture", preview)
                    server.assert_not_called()
                    knowledge_transcript.share_session(
                        client, project, "codex", str(native), None, None, str(preview["confirm"]),
                    )
                self.assertEqual(server.call_args.kwargs["payload"]["skill_capture"], later)

                personal = skill_adapters.destination("codex", None) / "release-review" / "SKILL.md"
                personal.parent.mkdir(parents=True)
                personal.write_text("---\ndescription: Personal override.\n---\n", encoding="utf-8")
                shadowed = str(uuid4())
                knowledge_finished_sessions.observe(client, work, "codex", event | {"session_id": shadowed})
                state = skill_capture(client, project, "codex", subject_key("codex", UUID(shadowed)),
                                      datetime.now(UTC).isoformat())
                self.assertEqual(state["skill_capture"]["installations"], [])
