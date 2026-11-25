"""真实 Git 对象覆盖试验输入、两侧资源隔离和重试结算。"""

import argparse
import io
import os
import shutil
import subprocess
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from weora_cli import benchmark_native, cli, knowledge, knowledge_benchmark
from weora_cli import knowledge_benchmark_executor as executor
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue


@unittest.skipUnless(shutil.which("git"), "需要实际 Git 命令")
class BenchmarkExecutorTests(unittest.TestCase):
    def test_pins_git_and_resumes_settlement_without_running_same_trial_twice(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            checkout = directory / "checkout"
            checkout.mkdir()

            def git(*args: str) -> str:
                return subprocess.check_output(["git", "-C", str(checkout), *args], text=True).strip()

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "test@example.test")
            git("remote", "add", "origin", "git@git.example.test:team/review.git")
            (checkout / "code.txt").write_text("original", encoding="utf-8")
            rules = checkout / "rules"
            rules.mkdir()
            rule = rules / "review.md"
            rule.write_text("main", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "main")
            base = git("rev-parse", "HEAD")
            rule.write_text("pulled", encoding="utf-8")
            (checkout / "code.txt").write_text("later", encoding="utf-8")
            git("commit", "-qam", "pulled")
            proposed = git("rev-parse", "HEAD")

            project, run_id, repo, task_id, main, pulled = [str(uuid4()) for _ in range(6)]
            slots: list[dict[str, JsonValue]] = [
                {"id": ident, "task_id": task_id, "arm": arm, "tool": tool,
                 "starting_commit": base, "outcome": "not_run"}
                for ident, arm, tool in ((main, "main", "codex"), (pulled, "pulled", "claude"))
            ]
            attempts: list[str] = []
            judged: list[str] = []
            writes: dict[str, JsonValue] = {}
            registration: dict[str, JsonValue] | None = None
            registration_id = str(uuid4())
            active_attempt = str(uuid4())
            run: dict[str, JsonValue] = {
                "id": run_id, "repository_id": repo, "resource_repository_id": repo,
                "connection_version": 1, "resource_connection_version": 1, "status": "pending",
                "base_resources": base, "proposal_resources": proposed,
                "deadline": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
            }

            def api(*args: object, **kwargs: JsonValue) -> dict[str, JsonValue]:
                nonlocal registration
                assert args[0] == client and args[1] == project
                if str(args[2]).endswith("/inputs"):
                    return {"execution": registration, "run": run | {"trials": [dict(row) for row in slots]}, "tasks": [
                        {"id": task_id, "prompt": "FIXTURE PROMPT", "success_criteria": "PRIVATE CRITERIA"},
                    ]}
                payload = as_object(kwargs["payload"])
                self.assertEqual(payload.pop("check_operation_id"), active_attempt)
                if str(args[2]).endswith("/execution"):
                    expected: dict[str, JsonValue] = {"id": registration_id, "profile": payload}
                    if registration is not None and registration != expected:
                        raise CollabError("KNOWLEDGE_BENCHMARK_CONFIG", "changed")
                    registration = expected
                    return registration
                raw = payload["items"]
                assert isinstance(raw, list) and len(raw) == 1
                grade = as_object(raw[0])
                self.assertEqual(grade["execution_id"], registration_id)
                identifier = str(grade["trial_id"])
                if not writes and identifier == main:
                    writes["interrupted"] = True
                    raise CollabError("WEORA_UNREACHABLE", "retry")
                writes[identifier] = grade["outcome"]
                slot = next(row for row in slots if row["id"] == identifier)
                slot["outcome"] = grade["outcome"]
                return run | {"trials": [dict(row) for row in slots],
                              "status": "complete" if pulled in writes else "pending"}

            def invoke(phase: str, payload: dict[str, JsonValue], timeout: int, **paths: Path) -> dict[str, JsonValue]:
                self.assertGreater(timeout, 0)
                if phase == "trial":
                    self.assertEqual(set(payload), {"tool", "prompt"})
                    self.assertNotIn("PRIVATE CRITERIA", str(payload))
                    self.assertEqual((paths["source"] / "code.txt").read_text(encoding="utf-8"), "original")
                    current = (paths["assets"] / "rules/review.md").read_text(encoding="utf-8")
                    attempts.append(current)
                    return {"answer": current, "diff": ""}
                self.assertEqual(paths, {})
                self.assertEqual(payload["criteria"], "PRIVATE CRITERIA")
                response = str(as_object(payload["evidence"])["answer"])
                judged.append(response)
                return {"passed": response == "pulled"}

            client = Client("https://example.test", str(uuid4()), "fixture-device")
            parser = argparse.ArgumentParser()
            knowledge.arguments(parser)
            args = parser.parse_args([
                "benchmark", "run-execute", "--project", project, "--run", run_id,
                "--checkout", str(checkout), "--resources", str(checkout), "--judge-model", "judge-v1",
                "--image", "weora/test@sha256:" + "a" * 64,
            ])
            args.check_operation_id = active_attempt
            sandbox = Mock(image="sha256:" + "b" * 64, network="none", gateway=None)
            sandbox.invoke.side_effect = invoke
            match = {"status": "ready", "repository_id": repo, "connection_version": 1}
            with (
                patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(directory / "collaboration.json")}),
                patch.object(knowledge, "paired_client", return_value=client),
                patch.object(executor, "call", side_effect=api),
                patch.object(executor, "repository_plan", return_value=[match, match]),
                patch.object(executor.Sandbox, "prepare", return_value=sandbox),
            ):
                with self.assertRaises(CollabError) as interrupted:
                    knowledge.execute(args)
                self.assertEqual(interrupted.exception.code, "WEORA_UNREACHABLE")
                self.assertEqual(attempts, ["main"])
                # 原收据可以先结算，其余试验拒绝不同模型，并可到另一执行机续跑。
                active_attempt = str(uuid4())
                args.check_operation_id = active_attempt
                args.judge_model = "judge-v2"
                with self.assertRaises(CollabError) as mismatch:
                    knowledge.execute(args)
                self.assertEqual(mismatch.exception.code, "KNOWLEDGE_BENCHMARK_CONFIG")
                self.assertEqual(attempts, ["main"])
                args.judge_model = "judge-v1"
                with patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(directory / "other" / "device.json")}):
                    result = as_object(knowledge.execute(args))

            self.assertEqual(attempts, ["main", "pulled"])
            self.assertEqual(judged, attempts)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(as_object(as_object(registration)["profile"])["image"], sandbox.image)
            self.assertEqual((writes[main], writes[pulled]), ("failed", "passed"))
            self.assertEqual((checkout / "code.txt").read_text(encoding="utf-8"), "later")


    def test_prepare_uses_resolved_image_id_and_rejects_wrong_image(self) -> None:
        reference = "fixtures/review@sha256:" + "a" * 64
        selected = "sha256:" + "b" * 64
        with patch.object(executor.shutil, "which", return_value="docker.exe"):
            with patch.object(executor.Sandbox, "_inspect", side_effect=["linux", selected, "true"]):
                sandbox = executor.Sandbox.prepare(reference, "internal", "http://Model-Gateway:8888")
            self.assertEqual((sandbox.image, sandbox.gateway), (selected, "http://model-gateway:8888"))
            with (
                patch.object(executor.Sandbox, "_inspect", side_effect=["linux", selected]),
                self.assertRaises(CollabError) as mismatch,
            ):
                executor.Sandbox.prepare("sha256:" + "c" * 64, None, None)
        self.assertEqual(mismatch.exception.code, "KNOWLEDGE_BENCHMARK_SANDBOX")

    def test_native_trial_installs_only_selected_resources_and_returns_actual_changes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, assets, work = (root / name for name in ("source", "assets", "work"))
            for directory in (checkout, assets, work):
                directory.mkdir()
            subprocess.run(["git", "-C", str(checkout), "init", "-q", "--template="], check=True)
            (checkout / "code.txt").write_text("original\n", encoding="utf-8")
            (assets / "rules").mkdir()
            (assets / "rules/review.md").write_text("ONLY THIS ARM", encoding="utf-8")
            (assets / "skills" / "reviewer").mkdir(parents=True)
            (assets / "skills/reviewer/SKILL.md").write_text("Reviewer package", encoding="utf-8")

            def native(tool: str, prompt: str, cwd: Path, model: str | None, *, judge: bool) -> str:
                self.assertEqual(tool, "codex")
                self.assertEqual(prompt, "public task")
                self.assertIsNone(model)
                self.assertFalse(judge)
                self.assertIn("ONLY THIS ARM", (cwd / "AGENTS.md").read_text(encoding="utf-8"))
                self.assertTrue((cwd / ".agents/skills/reviewer/SKILL.md").is_file())
                (cwd / "code.txt").write_text("changed\n", encoding="utf-8")
                (cwd / "receipt.txt").write_text("added\n", encoding="utf-8")
                return "agent answer"

            with (
                patch.object(benchmark_native, "_WORK", work),
                patch.object(benchmark_native, "_native", side_effect=native),
                patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}),
            ):
                evidence = benchmark_native._trial({"tool": "codex", "prompt": "public task"}, checkout, assets)
            self.assertEqual(evidence["answer"], "agent answer")
            self.assertIn("+changed", evidence["diff"])
            self.assertIn("+added", evidence["diff"])
            self.assertNotIn("ONLY THIS ARM", evidence["diff"])
            self.assertEqual((checkout / "code.txt").read_text(encoding="utf-8"), "original\n")


class SandboxTests(unittest.TestCase):
    def test_combined_run_preflights_before_writing_and_preserves_report_exit_code(self) -> None:
        workspace, project, run, operation = (str(uuid4()) for _ in range(4))
        client = Client("https://example.test", workspace, "fixture-device")
        image = "sha256:" + "a" * 64
        argv = [
            "context", "benchmark", "run", "--project", project, "--checkout", ".", "--resources", ".",
            "--base-ref", "main", "--proposal-ref", "proposal", "--key", operation, "--image", image,
            "--judge-model", "judge-v1", "--fail-on-regression",
        ]
        prepared = Mock()
        with (
            patch.object(knowledge, "paired_client", return_value=client),
            patch.object(executor.Sandbox, "prepare", return_value=prepared) as preflight,
            patch.object(knowledge_benchmark, "_run_plan", return_value={"id": run}) as planned,
            patch.object(knowledge_benchmark, "execute_run", return_value={"id": run, "gate": "regressed"}) as execute,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            preflight.side_effect = CollabError("KNOWLEDGE_BENCHMARK_SANDBOX", "fixture unavailable")
            self.assertEqual(cli.main(argv), 1)
            planned.assert_not_called()
            preflight.side_effect = None
            self.assertEqual(cli.main(argv), 1)
            self.assertEqual(execute.call_args.args[2].run, run)
            self.assertIs(execute.call_args.kwargs["sandbox"], prepared)
            self.assertIn('"gate": "regressed"', stdout.getvalue())

    def test_oci_process_mounts_only_requested_inputs_and_uses_internal_network(self) -> None:
        image = "weora/test@sha256:" + "a" * 64
        with (
            patch("weora_cli.knowledge_benchmark_executor.shutil.which", return_value="docker.exe"),
            patch.object(executor.Sandbox, "_inspect", side_effect=["linux", "sha256:abc", "false"]),
            self.assertRaises(CollabError),
        ):
            executor.Sandbox.prepare(image, "outer", "http://gateway:8080")
        runner = executor.Sandbox("docker", image, "none", None)
        with (
            patch.dict(os.environ, {"WEORA_COLLAB_TOKEN": "local-secret", "DOCKER_HOST": "ssh://remote"}),
            patch.object(executor, "_bounded", return_value=b'{"passed": true}') as launched,
        ):
            self.assertEqual(runner.invoke("judge", {"criteria": "SECRET"}, 5), {"passed": True})
            argv = launched.call_args.args[0]
            self.assertIn("--read-only", argv)
            self.assertEqual(argv[argv.index("--network") + 1], "none")
            self.assertEqual(argv.count("--mount"), 1)
            self.assertNotIn("WEORA_COLLAB_TOKEN", executor._env())
            self.assertNotIn("DOCKER_HOST", executor._env())
            self.assertNotIn("SECRET", " ".join(argv))

    def test_bounded_subprocess_accepts_stdin_and_rejects_excess_output(self) -> None:
        roundtrip = executor._bounded(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"], b"hello", 5,
        )
        self.assertEqual(roundtrip, b"hello")
        with self.assertRaises(ValueError):
            executor._bounded([sys.executable, "-c", "print('x' * 1_100_000)"], b"", 5)
