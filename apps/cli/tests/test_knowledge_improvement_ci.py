"""验证 PR 事件范围、提交检查和基准执行接入。"""

import io
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from weora_cli import cli, knowledge
from weora_cli import knowledge_improvement_ci as ci
from weora_cli import knowledge_improvement_delivery as delivery
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue
from weora_cli.knowledge_mr import ReviewRepository
from weora_cli.knowledge_session_origin import commits_in_checkout


class ImprovementCiTests(unittest.TestCase):
    def test_job_writes_gate_for_verified_head_and_skips_foreign_or_superseded(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_file = root / "event.json"
            workspace, project, proposal, run = (uuid4() for _ in range(4))
            head, base = "a" * 40, "b" * 40
            repo: dict[str, JsonValue] = {"full_name": "example/team"}
            event: dict[str, JsonValue] = {
                "action": "synchronize", "repository": repo, "pull_request": {
                    "number": 9, "head": {"ref": f"weora/self-improvement/{proposal.hex}", "sha": head, "repo": repo},
                    "base": {"ref": "main", "repo": repo}, "merged": False,
                },
            }

            def fixture() -> None:
                event_file.write_text(json.dumps(event), encoding="utf-8")

            fixture()
            startup = [
                "context", "self-improvement", "ci", "--project", str(project), "--event", str(event_file),
                "--workflow-run", "31", "--attempt", "2", "--checkout", str(root),
                "--image", "sha256:" + "c" * 64, "--judge-model", "fixed",
            ]
            states: list[tuple[str, JsonValue]] = []
            operations: list[JsonValue] = []
            result = "failure"
            plan_state = "pending"
            benchmark_status = "pending"

            def platform(*args: object, **kwargs: JsonValue) -> dict[str, JsonValue]:
                stage = str(args[2]).rsplit("/", 1)[-1]
                body = as_object(kwargs.get("payload"))
                self.assertEqual(body["head_commit"], head)
                if stage == "merge":
                    self.assertNotIn("operation_id", body)
                    return {"merge_commit": "f" * 40}
                operations.append(body["operation_id"])
                self.assertEqual((body["workflow_run"], body["attempt"]), ("31", "2"))
                if stage == "plan":
                    return {"benchmark_id": str(run), "base_resources": base, "head_commit": head,
                            "repository": "example/team", "check_state": plan_state,
                            "benchmark_status": benchmark_status,
                            "check_operation_id": body["operation_id"]}
                return {"check_state": result, "head_commit": head, "benchmark_id": str(run)}

            def status(*args: object, **kwargs: JsonValue) -> tuple[JsonValue, dict[str, str]]:
                payload = as_object(kwargs.get("payload"))
                self.assertEqual(payload["target_url"], "https://github.com/example/team/actions/runs/31")
                states.append((str(args[2]), payload["state"]))
                return {}, {}

            receipt = Mock()
            client = Client("https://example.test", str(workspace), "fixture")
            with (
                patch.object(knowledge, "paired_client", return_value=client),
                patch.object(ci, "call", side_effect=platform) as backend,
                patch.object(ci, "_authorization", return_value={"Authorization": "Bearer fixture"}),
                patch.object(ci, "_request", side_effect=status),
                patch.object(ci, "_fetch_resources") as fetch,
                patch("weora_cli.knowledge_improvement_ci.Sandbox.prepare", return_value=receipt) as prepare,
                patch.object(ci, "execute_run") as execute,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(cli.main(startup), 1)
                self.assertIn('"check_state": "failure"', stdout.getvalue())
                self.assertEqual(execute.call_args.args[2].run, str(run))
                self.assertEqual(execute.call_args.args[2].check_operation_id, operations[0])
                self.assertEqual(fetch.call_args.args[-2:], (base, head))
                self.assertEqual(operations[0], operations[1])
                expected_path = f"/repos/example/team/statuses/{head}"
                self.assertEqual(states, [(expected_path, "pending"), (expected_path, "failure")])

                result = "superseded"
                self.assertEqual(cli.main(startup), 0)
                self.assertEqual(states[-1], (expected_path, "pending"))
                self.assertEqual(len(states), 3)

                plan_state = "superseded"
                self.assertEqual(cli.main(startup), 0)
                self.assertEqual(len(states), 3)
                plan_state, result = "pending", "error"
                prepare.side_effect = CollabError("KNOWLEDGE_BENCHMARK_SANDBOX", "fixture unavailable")
                self.assertEqual(cli.main(startup), 1)
                self.assertEqual(states[-2:], [(expected_path, "pending"), (expected_path, "error")])
                self.assertIn('"execution_error": "KNOWLEDGE_BENCHMARK_SANDBOX"', stdout.getvalue())

                pr = as_object(event["pull_request"])
                as_object(pr["head"])["repo"] = {"full_name": "fork/team"}
                fixture()
                old = backend.call_count
                self.assertEqual(cli.main(startup), 0)
                self.assertEqual(backend.call_count, old)

                as_object(pr["head"])["repo"] = repo
                event["action"], pr["merged"] = "closed", True
                fixture()
                self.assertEqual(cli.main(startup), 0)
                self.assertIn('"check_state": "merged"', stdout.getvalue())
                self.assertEqual(len(states), 5)

                event["action"], pr["merged"] = "synchronize", False
                fixture()
                result, benchmark_status = "success", "complete"
                prepare.reset_mock()
                execute.reset_mock()
                self.assertEqual(cli.main(startup[:startup.index("--checkout")]), 0)
                self.assertEqual(states[-1], (expected_path, "success"))
                self.assertEqual(len(states), 6)
                prepare.assert_not_called()
                execute.assert_not_called()

    @unittest.skipUnless(shutil.which("git"), "需要实际 Git 命令")
    def test_fetches_only_fixed_objects_into_a_new_repository(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, fetched = root / "remote", root / "fetched"
            remote.mkdir()
            fetched.mkdir()

            def git(*args: str) -> str:
                return subprocess.check_output(["git", "-C", str(remote), *args], text=True).strip()

            git("init", "-q", "-b", "main")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "test@example.test")
            file = remote / "asset.md"
            file.write_text("Base", encoding="utf-8")
            git("add", "asset.md")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            file.write_text("Head", encoding="utf-8")
            git("commit", "-qam", "head")
            head = git("rev-parse", "HEAD")
            original = delivery._git

            def local(cwd: Path, env: dict[str, str], *args: str) -> bytes:
                safe = [str(remote) if part == "https://github.com/example/team.git" and args[0] == "fetch"
                        else part for part in args]
                return original(cwd, env | {"GIT_ALLOW_PROTOCOL": "file"}, *safe)

            with patch.object(ci, "_git", side_effect=local):
                ci._fetch_resources(fetched, ReviewRepository("github", "https://api.github.com", "example/team"),
                                    {"Authorization": "Bearer fixture"}, base, head)
            self.assertEqual(commits_in_checkout(fetched, {base, head}), {base, head})
            self.assertFalse((fetched / "asset.md").exists())
