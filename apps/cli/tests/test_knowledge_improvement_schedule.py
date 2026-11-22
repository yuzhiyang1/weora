"""验证调度使用冻结的真实 Git 对象，重跑沿用原修订。"""

import argparse
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from weora_cli import knowledge, knowledge_improve_run
from weora_cli import knowledge_improvement_delivery as delivery
from weora_cli import knowledge_improvement_schedule as schedule
from weora_cli.collab_client import Client, CollabError
from weora_cli.commands import JsonValue
from weora_cli.knowledge_benchmark_executor import _env
from weora_cli.knowledge_session_origin import checkout_remote, resolve_commit


@unittest.skipUnless(shutil.which("git"), "需要实际 Git 命令")
class ScheduledImprovementTests(unittest.TestCase):
    def test_one_workflow_run_recovers_on_a_new_attempt_and_uses_frozen_git_objects(self) -> None:
        with TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            remote = "https://github.com/example/review.git"
            git_env = _env(git=True)
            delivery._git(checkout, git_env, "init", "-q", "--template=", "--initial-branch=main")
            (checkout / "AGENTS.md").write_text("Check the boundary.", encoding="utf-8")
            delivery._git(checkout, git_env, "add", "--", "AGENTS.md")
            delivery._git(checkout, git_env, "-c", "user.name=Fixture", "-c", "user.email=test@example.test",
                          "commit", "-qm", "base")
            base = delivery._git(checkout, git_env, "rev-parse", "HEAD").decode().strip()
            project, repository, proposal = (str(uuid4()) for _ in range(3))
            parser = argparse.ArgumentParser()
            knowledge.arguments(parser)
            args = parser.parse_args([
                "self-improvement", "scheduled", "review-quality",
                "--project", project, "--checkout", str(checkout), "--repository", "Example/Review",
                "--base-branch", "main", "--model", "fixture", "--image", "fixture",
            ])
            client = Mock(spec=Client)
            pending: dict[str, JsonValue] = {
                "status": "pending_delivery", "base_commit": base,
                "resource_repository_id": repository, "proposal_id": proposal, "revision": 2,
            }
            delivered = pending | {"status": "delivered", "pr_number": 17}
            cache: list[str] = []
            resources: list[Path] = []

            def git(root: Path, env: dict[str, str], *values: str) -> bytes:
                # 只把 GitHub 测试端点替换成本机仓库，继续执行真实 fetch。
                target = tuple(str(checkout) if value == remote and values[0] in {"fetch", "ls-remote"}
                               else value for value in values)
                return delivery._git(root, env, *target)

            def check_finish(_client: Client, command: argparse.Namespace,
                             receipt: dict[str, JsonValue]) -> dict[str, JsonValue]:
                cache.append(command.key)
                root = command.resources
                resources.append(root)
                self.assertEqual(command.base_ref, base)
                self.assertEqual(checkout_remote(root), remote)
                self.assertEqual(resolve_commit(root, base), base)
                self.assertFalse((root / "AGENTS.md").exists())
                return receipt

            with (
                patch.dict(os.environ, {"GITHUB_RUN_ID": "421", "GITHUB_RUN_ATTEMPT": "1"}),
                patch.object(schedule, "_git", side_effect=git),
                patch.object(schedule, "_remote_ref", side_effect=lambda root, env, _remote, ref:
                             delivery._remote_ref(root, env, str(checkout), ref)),
                patch.object(schedule, "recover", side_effect=[{"status": "missing"}, pending, delivered, pending]),
                patch.object(schedule, "repository_plan", return_value=[{"status": "ready",
                                                                          "repository_id": repository}]),
                patch.object(schedule, "_authorization") as auth,
                patch.object(schedule, "_remote_env", return_value=git_env),
                patch.object(schedule, "_request", return_value=({
                    "full_name": "Example/Review", "default_branch": "main",
                }, {})),
                patch.object(schedule, "finish", side_effect=check_finish) as finish,
            ):
                self.assertEqual(schedule.execute(client, args), {"status": "missing"})
                self.assertFalse(resources[-1].exists())
                os.environ["GITHUB_RUN_ATTEMPT"] = "2"
                self.assertEqual(schedule.execute(client, args), pending)
                self.assertEqual(cache[0], cache[1])
                self.assertFalse(resources[-1].exists())
                self.assertEqual(schedule.execute(client, args), delivered)
                self.assertEqual(auth.call_count, 2)
                self.assertEqual(finish.call_count, 2)
                delivery._git(checkout, git_env, "-c", "user.name=Fixture", "-c", "user.email=test@example.test",
                              "commit", "--allow-empty", "-qm", "new base")
                with self.assertRaises(CollabError) as moved:
                    schedule.execute(client, args)
                self.assertEqual(moved.exception.code, "KNOWLEDGE_IMPROVEMENT_BASE")
                self.assertEqual(finish.call_count, 2)


class ImmediateImprovementTests(unittest.TestCase):
    def test_resume_skips_the_generator_and_does_not_publish_a_newer_revision(self) -> None:
        client = Mock(spec=Client)
        project, proposal = str(uuid4()), str(uuid4())
        args = argparse.Namespace(project=project, suite="benchmarks/regressions")
        revision: dict[str, JsonValue] = {"status": "pending_delivery", "proposal_id": proposal, "revision": 2}
        with (
            patch.object(knowledge_improve_run, "stage") as stage,
            patch.object(delivery, "call", return_value={"revision": 3, "status": "open"}) as spec,
            patch.object(delivery, "_publish") as publish,
        ):
            reply = knowledge_improve_run.finish(client, args, revision)
            self.assertEqual(reply, revision | {"status": "superseded"})
            spec.assert_called_once_with(client, project, f"/self-improvement/proposals/{proposal}/delivery",
                                         suite=args.suite)
            stage.assert_not_called()
            publish.assert_not_called()

    def test_publish_propagates_a_durable_receipt_for_the_exact_revision(self) -> None:
        client = Mock(spec=Client)
        args = argparse.Namespace(project=str(uuid4()), suite="benchmarks/regressions")
        proposal = str(uuid4())
        revision: dict[str, JsonValue] = {"proposal_id": proposal, "revision": 1}
        result = {"pr_number": 17, "head_commit": "a" * 40}
        with (
            patch.object(knowledge_improve_run, "stage", return_value=revision) as stage,
            patch.object(delivery, "call", return_value={"revision": 1, "status": "open", "current": None}),
            patch.object(delivery, "_publish", return_value=result) as publish,
        ):
            reply = knowledge_improve_run.finish(client, args, {"status": "missing"})
            self.assertEqual(reply, revision | {"status": "delivered", "delivery": result} | result)
            stage.assert_called_once_with(client, args)
            self.assertEqual(publish.call_args.args[2].proposal, proposal)


if __name__ == "__main__":
    unittest.main()
