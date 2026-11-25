"""验证无人值守收尾的任务隔离、断线续做和原生命令退出约定。"""

import argparse
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from weora_cli import cli, knowledge, skills
from weora_cli import knowledge_unattended as unattended
from weora_cli.collab_client import Client, CollabError
from weora_cli.commands import JsonValue
from weora_cli.knowledge_run_mode import TASK_ENV


class UnattendedSessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / "checkout"
        self.cwd.mkdir()
        self.client = Client("https://example.invalid", str(uuid4()), "synthetic-device")
        self.project = str(uuid4())
        self.scopes: dict[str, JsonValue] = {"archive": self.project, "scoring": self.project, "activity": self.project}
        self.options = argparse.Namespace(project=None, run_mode="unattended", limit=2, bound=False)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for fixture in (
            patch.dict(os.environ, {
                "WEORA_COLLAB_CONFIG": str(self.root / "device.json"), "WEORA_RUN_MODE": "unattended",
            }),
            patch.object(unattended, "_targets", side_effect=lambda *_: self.scopes.copy()),
            patch.object(unattended, "_bound_project", return_value=self.project),
            patch.object(unattended, "activity_binding", return_value={"enabled": True, "project": self.project}),
            patch.object(unattended, "publish_activity", return_value={"reported": 1, "errors": []}),
            patch.object(unattended, "score_work", return_value={"status": "empty", "items": []}),
        ):
            self.stack.enter_context(fixture)
        self.stage_usage = self.stack.enter_context(patch.object(unattended, "stage_activity"))
        self.stage_score = self.stack.enter_context(patch.object(unattended, "stage_drained_session"))
        self.publish = self.stack.enter_context(patch.object(unattended, "archive_session", return_value={"items": []}))

    def _record(self, task: str = "", *, directory: Path | None = None) -> tuple[str, Path]:
        cwd, identifier = directory or self.cwd, str(uuid4())
        source = self.root / f"rollout-{identifier}.jsonl"
        source.write_text("\n".join(map(json.dumps, [
            {"type": "session_meta", "payload": {"id": identifier, "source": "exec", "cwd": str(cwd)}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Checked the fixture."},
            ]}},
        ])), encoding="utf-8")
        with patch.dict(os.environ, {TASK_ENV: task, "CODEX_HOME": str(self.root)}):
            unattended.observe(self.client, cwd, "codex", {
                "hook_event_name": "SessionStart", "session_id": identifier,
            })
            unattended.observe(self.client, cwd, "codex", {
                "hook_event_name": "Stop", "session_id": identifier, "transcript_path": str(source),
            })
        return identifier, source

    def _fake_transcript(self, runtime: str, session: object, chosen: Path | None) -> Path:
        self.assertEqual(runtime, "codex")
        assert chosen is not None
        self.assertIn(str(session), chosen.name)
        return chosen

    def test_task_scope_and_retry_keep_first_cutoff_and_other_stages(self) -> None:
        task, active = str(uuid4()), str(uuid4())
        native, _ = self._record(task)
        self._record(active)
        self.publish.side_effect = [CollabError("WEORA_UNREACHABLE", "Fixture is offline."), {"items": []}]
        with (
            patch.dict(os.environ, {"CODEX_HOME": str(self.root)}),
            patch.object(unattended, "resolve_transcript", side_effect=self._fake_transcript),
        ):
            self.assertEqual(unattended.drain(self.client, self.cwd, self.options)["status"], "empty")
            self.stage_score.assert_not_called()
            self.publish.assert_not_called()
            failed = unattended.drain(self.client, self.cwd, self.options, task=task)
            self.assertTrue(failed["drain_failed"])
            self.stage_score.assert_called_once()
            self.stage_usage.assert_called_once()
            first_cutoff = self.publish.call_args.kwargs["stopped_at"]
            self.assertEqual(first_cutoff, self.stage_score.call_args.kwargs["stopped_at"])
            self.assertEqual(first_cutoff, self.stage_usage.call_args.kwargs["stopped_at"])
            completed = unattended.drain(self.client, self.cwd, self.options)
            self.assertNotIn("drain_failed", completed)
            self.assertEqual(self.publish.call_args.args[3], native)
            self.assertEqual(self.publish.call_args.kwargs["stopped_at"], first_cutoff)
            self.stage_score.assert_called_once()
            self.assertNotIn("sessions", unattended.drain(self.client, self.cwd, self.options))
        self.assertEqual(self.publish.call_count, 2)

    def test_changed_archive_binding_keeps_scoring_and_never_retargets(self) -> None:
        self._record()
        self.scopes["archive"] = str(uuid4())
        with (
            patch.dict(os.environ, {"CODEX_HOME": str(self.root)}),
            patch.object(unattended, "resolve_transcript", side_effect=self._fake_transcript),
        ):
            result = unattended.drain(self.client, self.cwd, self.options)
        self.assertTrue(result["drain_failed"])
        self.assertIn({"action": "archive", "code": "KNOWLEDGE_DRAIN_BINDING_CHANGED"}, result["errors"])
        self.publish.assert_not_called()
        self.stage_score.assert_called_once()

    def test_claude_sdk_capture_is_processed_without_ci_or_session_id(self) -> None:
        identifier = str(uuid4())
        source = self.root / "projects" / "fixture" / f"{identifier}.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({
            "type": "assistant", "sessionId": identifier, "cwd": str(self.cwd),
            "message": {"content": "Confirmed the fixture."},
        }), encoding="utf-8")
        env = {"CLAUDE_CONFIG_DIR": str(self.root), "CLAUDE_CODE_ENTRYPOINT": "sdk-ts",
               "CI": "", "WEORA_RUN_MODE": "", TASK_ENV: ""}
        with patch.dict(os.environ, env):
            unattended.observe(self.client, self.cwd, "claude-code", {
                "hook_event_name": "SessionStart", "session_id": identifier,
            })
            report = unattended.drain(self.client, self.cwd, self.options)
        self.assertNotIn("drain_failed", report)
        self.assertEqual(self.publish.call_args.args[2:5], ("claude-code", identifier, source))

    def test_changed_transcript_cannot_extend_a_frozen_session_on_retry(self) -> None:
        _, source = self._record()
        self.publish.side_effect = CollabError("WEORA_UNREACHABLE", "Fixture is offline.")
        with (
            patch.dict(os.environ, {"CODEX_HOME": str(self.root)}),
            patch.object(unattended, "resolve_transcript", side_effect=self._fake_transcript),
        ):
            unattended.drain(self.client, self.cwd, self.options)
            with source.open("a", encoding="utf-8") as stream:
                stream.write("\n{}")
            ignored = unattended.drain(self.client, self.cwd, self.options)
        self.assertEqual(ignored["errors"][0]["code"], "KNOWLEDGE_DRAIN_TRANSCRIPT_CHANGED")
        self.publish.assert_called_once()

    def test_external_driver_uses_ci_mode_and_reports_own_failure(self) -> None:
        with (
            patch.dict(os.environ, {"WEORA_RUN_MODE": "", "CI": "true", TASK_ENV: ""}),
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "drain_unattended", return_value={"drain_failed": True}) as drain,
            patch.object(cli, "emit"),
        ):
            self.assertEqual(cli.main(["context", "score-drain"]), 1)
        self.assertEqual(drain.call_args.args[2].run_mode, "unattended")

    def test_skill_launcher_waits_and_keeps_native_exit_code(self) -> None:
        steps: list[tuple[str, str]] = []

        def run_native(*args: object, env: dict[str, str]) -> int:
            self.assertEqual(env["WEORA_RUN_MODE"], "unattended")
            steps.append(("native", env[TASK_ENV]))
            return 7

        def drain(*args: object, task: str) -> dict[str, JsonValue]:
            steps.append(("drain", task))
            return {"drain_failed": True}

        parser = cli.build_parser()
        args = parser.parse_args(["skill", "launch", "--runtime", "codex", "--project", str(self.cwd),
                                  "--unattended", "--", "exec", "test"])
        with (
            patch.object(skills, "_binding_scope", return_value=("project", str(self.cwd))),
            patch.object(skills, "database", return_value=ExitStack()),
            patch.object(skills, "lookup"),
            patch.object(skills, "client_for", return_value=self.client),
            patch.object(skills, "pull"),
            patch.object(skills, "launch", side_effect=run_native),
            patch.object(skills, "drain_unattended", side_effect=drain),
            patch("sys.stderr"),
        ):
            self.assertEqual(skills.launch_bound(args), 7)
        self.assertEqual(steps, [("native", steps[0][1]), ("drain", steps[0][1])])
