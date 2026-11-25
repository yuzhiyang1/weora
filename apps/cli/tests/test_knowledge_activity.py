"""验证只上报可重放的会话计数，项目或设备切换不补发旧记录。"""

import argparse
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import collaboration, knowledge
from weora_cli import knowledge_activity as activity
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue


class KnowledgeActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name).resolve()
        self.cwd = self.home / "project"
        self.cwd.mkdir()
        self.client = Client("https://example.invalid", str(uuid4()), "fixture")
        env = patch.dict(os.environ, {
            "WEORA_COLLAB_CONFIG": str(self.home / "device.json"), "CODEX_HOME": str(self.home / "codex"),
            "WEORA_USAGE_REPORT_DISABLED": "",
        })
        env.start()
        self.addCleanup(env.stop)
        self.project = str(uuid4())
        self.parser = argparse.ArgumentParser()
        knowledge.arguments(self.parser)

    def _transcript(self, session: str, source: str) -> Path:
        path = self.home / "codex" / "sessions" / f"rollout-{session}.jsonl"
        path.parent.mkdir(exist_ok=True, parents=True)
        lines = [
            {"type": "session_meta", "payload": {"id": session, "source": source}},
            {"type": "event_msg", "payload": {"type": "turn_aborted", "reason": "interrupted"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "a",
                                                    "output": "The tool use was rejected. hidden secret"}},
            {"type": "event_msg", "payload": {"type": "exec_command_end", "call_id": "b", "exit_code": 1}},
        ]
        path.write_text("\n".join(json.dumps(row) for row in lines), encoding="utf-8")
        return path

    def test_hook_and_cli_retry_share_only_scoped_counts_then_resume(self) -> None:
        native = str(uuid4())
        transcript = self._transcript(native, "cli")
        stop: dict[str, JsonValue] = {
            "hook_event_name": "Stop", "session_id": native, "cwd": str(self.cwd),
            "transcript_path": str(transcript),
        }
        pushes: list[dict[str, JsonValue]] = []

        def request(
            _client: Client, project: str | None, suffix: str = "", *,
            method: str = "GET", payload: dict[str, JsonValue] | None = None, **_query: JsonValue,
        ) -> dict[str, JsonValue]:
            if suffix == "/settings/usage-report":
                return {"enabled": True}
            if method == "GET":
                return {}
            self.assertEqual((project, suffix, method), (self.project, "/activity/sessions", "PUT"))
            assert payload is not None
            pushes.append(payload)
            if len(pushes) == 1:
                raise CollabError("WEORA_UNREACHABLE", "fixture network interruption")
            return {"status": "recorded"}

        with (
            patch.object(activity, "call", side_effect=request),
            patch("weora_cli.collaboration.paired_client", return_value=self.client),
            patch("weora_cli.collaboration.observe_start"),
            patch("weora_cli.collaboration.observe_archive", return_value=""),
            patch("weora_cli.collaboration.observe_session_share", return_value=""),
            patch("weora_cli.knowledge.paired_client", return_value=self.client),
            patch("weora_cli.knowledge.Path.cwd", return_value=self.cwd),
        ):
            command = self.parser.parse_args(["activity-settings", "enable", "--project", self.project])
            self.assertTrue(as_object(knowledge.execute(command))["enabled"])
            with patch("sys.stdin", io.StringIO(json.dumps(stop))):
                self.assertEqual(collaboration.hook_output(argparse.Namespace(runtime="codex")), {})
            for prompt in ("不对，请重来", "wrong again"):
                activity.observe(self.client, self.cwd, "codex", stop | {
                    "hook_event_name": "UserPromptSubmit", "prompt": prompt,
                })
            publish = self.parser.parse_args(["publish-activity"])
            first = as_object(knowledge.execute(publish))
            self.assertEqual(first["pending"], 1)
            switched = Client(self.client.base, self.client.workspace, "other-device")
            self.assertIsNone(activity.publish(switched, self.cwd))
            activity.configure(self.client, self.cwd, "enable", workspace=False, project=str(uuid4()), shared=False)
            self.assertEqual(as_object(knowledge.execute(publish))["reported"], 0)
            activity.configure(self.client, self.cwd, "enable", workspace=False, project=self.project, shared=False)
            transcript.unlink()
            self.assertEqual(as_object(knowledge.execute(publish))["reported"], 1)
            self.assertEqual(as_object(knowledge.execute(publish))["reported"], 0)
            self.assertEqual(pushes[0], pushes[1])
            self.assertEqual({key: pushes[0][key] for key in ("interrupts", "tool_rejections", "corrections")},
                             {"interrupts": 1, "tool_rejections": 1, "corrections": 1})
            self.assertEqual(pushes[0]["run_mode"], "attended")
            self.assertNotIn("secret", str(pushes))
            self.assertNotIn(native, str(pushes))
            self.assertNotIn(str(self.cwd), str(pushes))

    def test_new_correction_during_publication_stays_pending(self) -> None:
        native = str(uuid4())
        transcript = self._transcript(native, "cli")
        stop = {"hook_event_name": "Stop", "session_id": native, "transcript_path": str(transcript)}
        with patch.object(activity, "call", return_value={"enabled": True}) as request:
            activity.configure(self.client, self.cwd, "enable", workspace=False, project=self.project, shared=False)
            activity.observe(self.client, self.cwd, "codex", stop)

            def accept_then_resume(*_args: object, **_kwargs: object) -> dict[str, JsonValue]:
                activity.observe(self.client, self.cwd, "codex", stop | {
                    "hook_event_name": "UserPromptSubmit", "prompt": "wrong, please redo",
                })
                return {"status": "recorded"}

            request.side_effect = accept_then_resume
            first = activity.publish(self.client, self.cwd)
            assert first is not None
            self.assertEqual((first["reported"], first["pending"]), (1, 1))
            request.side_effect = None
            second = activity.publish(self.client, self.cwd)
            assert second is not None
            self.assertEqual((second["reported"], second["pending"]), (1, 0))
            self.assertEqual(request.call_args.kwargs["payload"]["corrections"], 1)

    def test_unattended_drain_is_independent_and_respects_opt_out(self) -> None:
        native = str(uuid4())
        transcript = self._transcript(native, "exec")
        with patch.object(activity, "call", return_value={"enabled": True}) as request:
            activity.configure(self.client, self.cwd, "enable", workspace=False, project=self.project, shared=False)
            with patch.dict(os.environ, {"WEORA_USAGE_REPORT_DISABLED": "1"}):
                self.assertIsNone(activity.drain(self.client, self.cwd, "codex", native, transcript))
            activity.drain(self.client, self.cwd, "codex", native, transcript)
            self.assertEqual(request.call_args.kwargs["payload"]["run_mode"], "unattended")
            self.assertEqual(request.call_args.kwargs["payload"]["interrupts"], 1)
