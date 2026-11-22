"""通过真实资源提交和生成器替身验证自我改进 CLI。"""

import argparse
import hashlib
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from weora_cli import knowledge
from weora_cli import knowledge_improve_run as runner
from weora_cli.collab_client import Client, CollabError, as_object, items
from weora_cli.commands import JsonValue


@unittest.skipUnless(shutil.which("git"), "需要实际 Git 命令")
class ImproveRunTests(unittest.TestCase):
    def test_run_binds_tasks_and_replays_saved_batch_without_another_model_call(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "assets"
            checkout.mkdir()

            def git(*args: str) -> str:
                return subprocess.check_output(["git", "-C", str(checkout), *args], text=True).strip()

            git("init", "-q")
            git("config", "user.email", "test@example.test")
            git("config", "user.name", "Fixture")
            git("remote", "add", "origin", "git@git.example.test:team/assets.git")
            rules = checkout / "rules"
            rules.mkdir()
            file = rules / "review.md"
            file.write_bytes(b"Original team rule.")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            file.write_bytes(b"Local work that is not part of the base.")

            project, repo, operation, learning = (str(uuid4()) for _ in range(4))
            failures: list[dict[str, JsonValue]] = [
                {"label_id": label, "runtime": "codex", "label": "fail", "git_start": {"commit": base},
                 "learnings": [{"id": learning, "revision": 1}]}
                for label in (7, 8)
            ]
            selection = {"ready": True, "proposal_id": None, "failures": failures, "covered": []}
            posted: list[dict[str, JsonValue]] = []
            sandbox = Mock()

            def invoke(
                phase: str, request: dict[str, JsonValue], timeout: int,
            ) -> dict[str, JsonValue]:
                self.assertEqual(phase, "improve")
                self.assertGreater(timeout, 0)
                context = as_object(request["evidence"])
                self.assertEqual(context["resources"], {"rules/review.md": "Original team rule."})
                self.assertIn("Review failed on edge cases.", str(context["failures"]))
                return {
                    "change": True, "reason": "Both failures missed edge cases.",
                    "tasks": [{"label_id": label, "name": f"check-{label}", "title": "Review",
                               "prompt": "Review for bugs.", "success_criteria": "Report the missing edge case."}
                              for label in (7, 8)],
                    "changes": [{"path": "rules/review.md", "content": "Always review edge cases."}],
                }

            sandbox.invoke.side_effect = invoke

            def request(*args: object, **kwargs: JsonValue) -> dict[str, JsonValue]:
                endpoint = str(args[2])
                if endpoint == "/session-scorers/plan":
                    return {"items": [{"name": "quality", "prompt": "Score edge cases."}]}
                if endpoint == f"/{learning}":
                    return {"revision": 1, "title": "Review", "body": "Review failed on edge cases."}
                self.assertEqual(endpoint, "/self-improvement/proposals")
                payload = as_object(kwargs["payload"])
                posted.append(payload)
                if len(posted) == 1:
                    raise CollabError("WEORA_UNREACHABLE", "retry")
                return {"revision": 1, "change_paths": ["rules/review.md"]}

            parser = argparse.ArgumentParser()
            knowledge.arguments(parser)
            argv = [
                "self-improvement", "run", "quality", "--project", project, "--checkout", str(checkout),
                "--resources", str(checkout), "--base-ref", base, "--image", "sha256:" + "a" * 64,
                "--model", "fixed-model", "--key", operation,
            ]
            client = Client("https://example.test", str(uuid4()), "fixture")
            with (
                patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(root / "collaboration.json")}),
                patch.object(knowledge, "paired_client", return_value=client),
                patch.object(runner, "plan", return_value=selection),
                patch.object(runner, "repository_plan", return_value=[{"status": "ready", "repository_id": repo}]),
                patch.object(runner, "call", side_effect=request),
                patch("weora_cli.knowledge_improve_run.Sandbox.prepare", return_value=sandbox),
            ):
                preview = as_object(knowledge.execute(parser.parse_args([*argv, "--preview"])))
                self.assertEqual(preview["status"], "draft")
                self.assertEqual(posted, [])
                with self.assertRaises(CollabError) as interrupted:
                    knowledge.execute(parser.parse_args(argv))
                self.assertEqual(interrupted.exception.code, "WEORA_UNREACHABLE")
                result = knowledge.execute(parser.parse_args(argv))
                self.assertEqual(result, {"revision": 1, "change_paths": ["rules/review.md"]})
                self.assertEqual(sandbox.invoke.call_count, 1)

            self.assertEqual(posted[0], posted[1])
            self.assertEqual(posted[0]["base_commit"], base)
            self.assertEqual({task["starting_commit"] for task in items({"items": posted[0]["tasks"]})}, {base})
            change = items({"items": posted[0]["changes"]})[0]
            self.assertEqual(change["before_sha256"], hashlib.sha256(b"Original team rule.").hexdigest())

    def test_skip_and_validate_untrusted_generated_output(self) -> None:
        failure: dict[str, JsonValue] = {"label_id": 7, "git_start": {"commit": "a" * 40}}
        skipped: dict[str, JsonValue] = {"change": False, "tasks": [], "changes": []}
        self.assertEqual(runner._validated(skipped, [failure], {}, set()), ([], []))
        proposed: dict[str, JsonValue] = {
            "change": True,
            "tasks": [{"label_id": 7, "name": "review", "title": "Review", "prompt": "Inspect the code.",
                       "success_criteria": "Report the missed case."}],
            "changes": [{"path": "src/app.py", "content": "def run(): pass"}],
        }
        with self.assertRaises(CollabError):
            runner._validated(proposed, [failure], {}, set())
        with self.assertRaises(CollabError):
            runner._validated(proposed, [], {}, set())
