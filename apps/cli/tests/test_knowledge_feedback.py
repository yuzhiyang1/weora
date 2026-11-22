"""验证离线反馈队列的作用域、恢复及并发清理。"""

import os
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge, knowledge_feedback, knowledge_settings
from weora_cli.cli import build_parser, execute
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue


class FeedbackOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.client = Client("http://127.0.0.1:8765", str(uuid4()), "fixture-pairing")
        self.entry, self.project = str(uuid4()), str(uuid4())
        self.enterContext(patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(self.root / "device.json")}))
        self.enterContext(patch.object(knowledge, "paired_client", return_value=self.client))

    def command(self, *args: str) -> dict[str, JsonValue]:
        return as_object(execute(build_parser().parse_args(["context", *args])))

    def submit(self, positive: bool, *args: str) -> dict[str, JsonValue]:
        return self.command(
            "feedback", self.entry, "--project", self.project,
            "--session", "root-fixture", "--positive" if positive else "--negative", *args,
        )

    def test_preview_recovery_and_safe_partition(self) -> None:
        with patch.object(knowledge_feedback, "call") as send:
            self.assertTrue(self.submit(True, "--dry-run")["preview"])
            send.assert_not_called()
            self.assertFalse(knowledge_feedback.has_pending_feedback(self.client))
            send.side_effect = CollabError("WEORA_UNREACHABLE", "测试断线")
            receipt = self.submit(True)
            self.assertTrue(receipt["queued"])
            original = send.call_args.kwargs["payload"]
            self.assertEqual(original["operation_id"], receipt["operation_id"])
            self.assertEqual(original["session"], "root-fixture")
            self.assertFalse(knowledge_feedback.has_pending_feedback(replace(self.client, token="other-device")))
            send.reset_mock()
            send.side_effect = None
            send.return_value = {}
            self.command("sync", "--project", str(uuid4()))
            send.assert_not_called()
            synced = self.command("sync")
            self.assertEqual(as_object(synced["feedback"])["sent"], [{
                "entry_id": self.entry, "project_id": self.project, "operation_id": receipt["operation_id"],
            }])
            send.assert_called_once_with(
                self.client, self.project, f"/{self.entry}/feedback", method="POST", payload=original,
            )
            self.assertFalse(knowledge_feedback.has_pending_feedback(self.client))

    def test_late_ack_does_not_clear_new_choice(self) -> None:
        recorded: list[dict[str, JsonValue]] = []

        def send(*args: object, payload: dict[str, JsonValue], **kwargs: object) -> dict[str, JsonValue]:
            recorded.append(payload)
            if len(recorded) == 1:
                self.assertTrue(self.submit(False)["queued"])
                return {}
            if len(recorded) == 2:
                raise CollabError("SERVICE_TEMPORARILY_UNAVAILABLE", "测试延迟", status=503)
            return {}

        with patch.object(knowledge_feedback, "call", side_effect=send):
            self.assertEqual(self.submit(True), {})
            self.assertTrue(knowledge_feedback.has_pending_feedback(self.client))
            self.command("sync", "--project", self.project)
        self.assertEqual([row["positive"] for row in recorded], [True, False, False])
        self.assertEqual(recorded[1], recorded[2])

    def test_read_only_check_leaves_queued_feedback_for_next_search(self) -> None:
        with patch.object(knowledge_feedback, "call", side_effect=CollabError("WEORA_UNREACHABLE", "off")):
            self.assertTrue(self.submit(True)["queued"])
        with (
            patch.object(knowledge_feedback, "call", return_value={}) as send,
            patch.object(knowledge, "_call", return_value={"items": []}) as search,
        ):
            self.command("recall", "fixture", "--project", self.project, "--check")
            send.assert_not_called()
            self.assertEqual(search.call_args.args[-1], "/recall/preview")
            payload = search.call_args.kwargs["payload"]
            self.assertTrue(payload["check"])
            self.assertEqual(payload["pending_feedback"][0]["session"], "root-fixture")
            self.command("recall", "fixture", "--project", self.project)
            send.assert_called_once()
        self.assertFalse(knowledge_feedback.has_pending_feedback(self.client))

    def test_disabled_automatic_commands_leave_feedback_for_manual_sync(self) -> None:
        with patch.object(knowledge_feedback, "call", side_effect=CollabError("WEORA_UNREACHABLE", "off")):
            receipt = self.submit(True)
        settings = knowledge_settings.SessionSettings(self.client, self.root)
        with (
            patch.dict(os.environ, {"WEORA_RECALL_DISABLED": "1"}),
            patch.object(knowledge_feedback, "call", return_value={}) as send,
            patch.object(knowledge, "_call", return_value={"items": []}) as search,
            patch.object(subprocess, "Popen") as launch,
        ):
            self.command("search", "fixture", "--project", self.project, "--session", "root-fixture", "--automatic")
            self.assertNotIn("payload", search.call_args.kwargs)
            self.assertNotIn("pending_feedback", search.call_args.kwargs)
            self.assertEqual(self.command("sync", "--automatic"), {"published": []})
            knowledge.sync_queued_after_start(settings)
            launch.assert_not_called()
            send.assert_not_called()
            self.assertTrue(knowledge_feedback.has_pending_feedback(self.client))
            self.command("recall", "fixture", "--project", self.project, "--check")
            projected = search.call_args.kwargs["payload"]["pending_feedback"]
            self.assertEqual(projected[0]["operation_id"], receipt["operation_id"])
            self.command("sync")
            send.assert_called_once()
            self.assertEqual(send.call_args.kwargs["payload"]["operation_id"], receipt["operation_id"])
        self.assertFalse(knowledge_feedback.has_pending_feedback(self.client))

    def test_enabled_background_and_search_send_only_current_scope(self) -> None:
        other = str(uuid4())
        with patch.object(knowledge_feedback, "call", side_effect=CollabError("WEORA_UNREACHABLE", "off")):
            for project in (None, other, self.project):
                receipt = knowledge_feedback.submit_feedback(
                    self.client, project, self.entry, "root-fixture", positive=True, preview=False,
                )
                self.assertTrue(receipt["queued"])
        with (
            patch.dict(os.environ, {"WEORA_RECALL_DISABLED": "0"}),
            patch.object(Path, "cwd", return_value=self.root),
            patch.object(knowledge_settings, "call", return_value={}),
            patch.object(subprocess, "Popen") as launch,
            patch.object(knowledge, "_call", return_value={"items": []}) as search,
            patch.object(knowledge_feedback, "call", return_value={}) as send,
        ):
            settings = knowledge_settings.SessionSettings(self.client, self.root)
            settings.configure("recall", "enable", project=self.project)
            knowledge.sync_queued_after_start(settings)
            self.assertEqual(launch.call_args.args[0][-3:], ["context", "sync", "--automatic"])
            self.assertEqual(launch.call_args.kwargs["cwd"], self.root)
            self.command("search", "fixture", "--project", self.project, "--session", "root-fixture", "--automatic")
            self.assertEqual(send.call_args.args[1], self.project)
            self.assertNotIn("pending_feedback", search.call_args.kwargs["payload"])
            send.reset_mock()
            self.command("sync", "--automatic")
            send.assert_not_called()
            self.assertTrue(knowledge_feedback.has_pending_feedback(self.client))
            send.side_effect = CollabError("WEORA_UNREACHABLE", "又断线")
            newer = self.submit(False)
            self.assertTrue(newer["queued"])
            send.side_effect = None
            send.reset_mock()
            result = self.command("sync", "--automatic")
            self.assertEqual(as_object(result["feedback"])["sent"], [{
                "entry_id": self.entry, "project_id": self.project, "operation_id": newer["operation_id"],
            }])
            self.assertEqual(send.call_args.args[1], self.project)
            send.reset_mock()
            self.command("sync")
            self.assertEqual({call.args[1] for call in send.call_args_list}, {None, other})
