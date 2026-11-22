"""验证生产 CLI 从提供方回读真正的结束记录，而事件只用于定位。"""

import argparse
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge
from weora_cli import knowledge_workflow_activity as workflow
from weora_cli.collab_client import Client, CollabError
from weora_cli.commands import JsonValue


class WorkflowActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client("https://example.invalid", str(uuid4()), "fixture")
        self.project, self.repository = str(uuid4()), str(uuid4())
        self.parser = argparse.ArgumentParser()
        knowledge.arguments(self.parser)
        env = patch.dict(os.environ, {"GITHUB_REPOSITORY": "Example/Knowledge"})
        env.start()
        self.addCleanup(env.stop)
        self.detail: dict[str, JsonValue] = {
            "id": 195, "run_attempt": 2, "status": "completed", "conclusion": "cancelled",
            "repository": {"full_name": "Example/Knowledge"},
            "path": ".github/workflows/github-self-improvement-benchmark.yml@main", "event": "pull_request_target",
        }
        self.jobs: list[JsonValue] = [
            {"run_id": 195, "status": "completed", "conclusion": "success",
             "started_at": "2022-01-01T00:00:00+00:00", "completed_at": "2022-01-01T00:00:10+00:00"},
            {"run_id": 195, "status": "completed", "conclusion": "cancelled",
             "started_at": "2022-01-01T00:00:07+00:00", "completed_at": "2022-01-01T00:00:11+00:00"},
            {"run_id": 195, "status": "completed", "conclusion": "skipped",
             "started_at": "2022-01-01T00:00:00+00:00", "completed_at": "2022-01-01T00:01:11+00:00"},
        ]

    def test_event_uses_exact_attempt_and_parallel_job_wall_time(self) -> None:
        with TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            event.write_text(json.dumps({
                "action": "completed", "repository": {"full_name": "Example/Knowledge"},
                "workflow_run": {"id": 195, "run_attempt": 2, "conclusion": "failure", "body": "secret"},
            }), encoding="utf-8")
            parsed = self.parser.parse_args(["publish-workflow", "--event", str(event), "--project", self.project])
            with (
                patch("weora_cli.knowledge.paired_client", return_value=self.client),
                patch.object(workflow, "_authorization", return_value={"Authorization": "fixture"}),
                patch.object(workflow, "_request", side_effect=[
                    (self.detail, {}), ({"total_count": len(self.jobs), "jobs": self.jobs}, {}),
                ]) as provider,
                patch.object(workflow, "repository_plan", return_value=[{
                    "status": "ready", "repository_id": self.repository, "connection_version": 5,
                }]) as connection,
                patch.object(workflow, "call", side_effect=[{"enabled": True}, {"status": "recorded"}]) as platform,
            ):
                self.assertEqual(knowledge.execute(parsed), {"status": "recorded"})
            target = "/repos/example/knowledge/actions/runs/195/attempts/2"
            self.assertEqual([call.args[2] for call in provider.call_args_list], [target, target + "/jobs"])
            self.assertEqual(platform.call_args.args[1:], (self.project, "/activity/workflows"))
            self.assertEqual(platform.call_args.kwargs["payload"], {
                "repository_id": self.repository, "connection_version": 5, "repository": "example/knowledge",
                "run_id": "195", "attempt": 2, "workflow": "improvement-benchmark", "outcome": "cancelled",
                "duration_ms": 11000,
            })
            connection.assert_called_once_with(self.client, self.project, ["https://github.com/example/knowledge.git"])

    def test_manual_replay_keeps_only_eligible_end_outcomes(self) -> None:
        parsed = self.parser.parse_args(["publish-workflow", "--project", self.project,
                                         "--repository", "example/knowledge", "--run", "195", "--attempt", "2"])
        with (
            patch.object(workflow, "_authorization"),
            patch.object(workflow, "call", side_effect=[{"enabled": False}, {"enabled": True}, {"enabled": True},
                                                        {"status": "recorded"}]) as platform,
            patch.object(workflow, "repository_plan", return_value=[{
                "status": "ready", "repository_id": self.repository, "connection_version": 1,
            }]),
            patch.object(workflow, "_request", side_effect=[
                (self.detail | {"path": ".github/workflows/unknown.yml"}, {}),
                (self.detail | {"conclusion": "success"}, {}),
                ({"total_count": 1, "jobs": [self.jobs[2]]}, {}),
            ]) as provider,
        ):
            self.assertEqual(workflow.execute(self.client, parsed), {"status": "disabled"})
            provider.assert_not_called()
            self.assertEqual(workflow.execute(self.client, parsed), {"status": "ignored"})
            self.assertEqual(workflow.execute(self.client, parsed), {"status": "recorded"})
            payload = platform.call_args.kwargs["payload"]
            self.assertEqual((payload["outcome"], payload["duration_ms"]), ("skipped", 0))

        for wrong in ({"run_attempt": 9}, {"status": "in_progress"}):
            with (
                self.subTest(wrong=wrong),
                patch.object(workflow, "_authorization"),
                patch.object(workflow, "call", return_value={"enabled": True}) as platform,
                patch.object(workflow, "_request", return_value=(self.detail | wrong, {})),
            ):
                with self.assertRaises(CollabError) as invalid:
                    workflow.execute(self.client, parsed)
                self.assertEqual(invalid.exception.code, "KNOWLEDGE_WORKFLOW_EVENT")
                self.assertEqual(platform.call_count, 1)
