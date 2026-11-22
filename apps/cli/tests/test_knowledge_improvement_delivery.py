"""验证远端分支、同一个 PR 和断线后的收据恢复。"""

import argparse
import hashlib
import shutil
import subprocess
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge
from weora_cli import knowledge_improvement_delivery as delivery
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue


@unittest.skipUnless(shutil.which("git"), "需要实际 Git 命令")
class ImprovementDeliveryTests(unittest.TestCase):
    def test_real_bare_git_and_one_pr_with_lost_receipt_and_new_revision(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, bare = root / "checkout", root / "remote"
            checkout.mkdir()
            bare.mkdir()

            def git(where: Path, *args: str) -> str:
                return subprocess.check_output(["git", "-C", str(where), *args], text=True).strip()

            git(bare, "init", "-q", "--bare", "-b", "main")
            git(checkout, "init", "-q", "-b", "main")
            git(checkout, "config", "user.name", "Fixture")
            git(checkout, "config", "user.email", "test@example.test")
            git(checkout, "remote", "add", "origin", "https://github.com/example/review.git")
            (checkout / "rules").mkdir()
            rule = checkout / "rules/review.md"
            original = b"Original documentation."
            rule.write_bytes(original)
            git(checkout, "add", ".")
            git(checkout, "commit", "-qm", "baseline")
            git(checkout, "push", "-q", str(bare), "main")
            base = git(checkout, "rev-parse", "HEAD")
            rule.write_bytes(b"Local uncommitted work.")

            project, proposal, repo = (str(uuid4()) for _ in range(3))
            branch = "weora/self-improvement/" + proposal.replace("-", "")
            marker = f"<!-- weora-improvement:{proposal} -->"
            task_path = "benchmarks/regressions/tasks/first.yaml"
            spec: dict[str, JsonValue] = {
                "proposal_id": proposal, "repository_id": repo, "repository": "example/review",
                "connection_version": 1, "branch": branch, "base_branch": "main", "status": "open",
                "base_commit": base, "created_at": datetime.now(UTC).isoformat(), "scorer": "quality",
                "revision": 1, "marker": marker, "current": None, "previous": None,
                "files": [
                    {"path": "rules/review.md", "before_sha256": hashlib.sha256(original).hexdigest(),
                     "content": "Improved criteria."},
                    {"path": task_path, "before_sha256": None, "content": '{"schemaVersion":"v1alpha1"}\n'},
                ],
            }
            counters = {"push": 0, "PR": 0, "confirm": 0}
            transport = delivery._git

            def local_git(
                cwd: Path, env: dict[str, str], *args: str, content: bytes | None = None,
            ) -> bytes:
                if args[0] == "push":
                    counters["push"] += 1
                replaced = [str(bare) if value == "https://github.com/example/review.git" else value for value in args]
                # GitHub 运输和凭据由替身隔离，Git 对象及租约在真实本机裸库上执行。
                return transport(cwd, env | {"GIT_ALLOW_PROTOCOL": "file"}, *replaced, content=content)

            def platform(*args: object, **kwargs: JsonValue) -> dict[str, JsonValue]:
                method = kwargs.get("method")
                if method == "POST":
                    payload = as_object(kwargs.get("payload"))
                    if spec["current"] is None:
                        spec["current"] = payload | {"pr_number": None}
                    else:
                        self.assertEqual(as_object(spec["current"])["head_commit"], payload["head_commit"])
                    return as_object(spec["current"])
                if method == "PUT":
                    counters["confirm"] += 1
                    if counters["confirm"] == 1:
                        raise CollabError("WEORA_UNREACHABLE", "fixture request lost")
                    receipt = as_object(spec["current"]) | {"pr_number": 17}
                    spec["current"] = receipt
                    return receipt
                return dict(spec)

            def github(*args: object, **kwargs: JsonValue) -> tuple[JsonValue, dict[str, str]]:
                method = kwargs.get("method")
                if method == "POST":
                    counters["PR"] += 1
                elif counters["PR"] == 0:
                    return [], {}
                data: dict[str, JsonValue] = {
                    "number": 17, "html_url": "https://github.com/example/review/pull/17",
                    "body": marker, "state": "open", "base": {"ref": "main"},
                    "head": {"ref": branch, "sha": git(bare, "rev-parse", branch)},
                }
                return (data if method == "POST" or str(args[2]).endswith("/17") else [data]), {}

            parser = argparse.ArgumentParser()
            knowledge.arguments(parser)
            args = parser.parse_args([
                "self-improvement", "publish", "--project", project, "--proposal", proposal,
                "--resources", str(checkout),
            ])
            client = Client("https://example.test", str(uuid4()), "fixture")
            binding = {"status": "ready", "repository_id": repo, "connection_version": 1}
            with (
                patch.object(knowledge, "paired_client", return_value=client),
                patch.object(delivery, "call", side_effect=platform),
                patch.object(delivery, "repository_plan", return_value=[binding]),
                patch.object(delivery, "_authorization", return_value={"Authorization": "Bearer fixture"}),
                patch.object(delivery, "_request", side_effect=github),
                patch.object(delivery, "_git", side_effect=local_git),
            ):
                with self.assertRaises(CollabError) as lost:
                    knowledge.execute(args)
                self.assertEqual(lost.exception.code, "WEORA_UNREACHABLE")
                first_head = git(bare, "rev-parse", branch)
                receipt = as_object(knowledge.execute(args))
                self.assertEqual(receipt["head_commit"], first_head)
                self.assertEqual(counters, {"push": 1, "PR": 1, "confirm": 2})
                self.assertEqual(git(bare, "show", f"{first_head}:rules/review.md"), "Improved criteria.")
                self.assertIn("v1alpha1", git(bare, "show", f"{first_head}:{task_path}"))
                old = spec["files"]
                assert isinstance(old, list)
                spec.update({"revision": 2, "previous": spec["current"], "current": None,
                             "files": [*old, {"path": "rules/new.md", "before_sha256": None, "content": "New rule."}]})
                later = as_object(knowledge.execute(args))

            self.assertEqual(counters, {"push": 2, "PR": 1, "confirm": 3})
            self.assertEqual(later["pull_request"], receipt["pull_request"])
            self.assertEqual(git(bare, "rev-parse", f"{branch}^"), first_head)
            self.assertEqual(git(bare, "rev-parse", "main"), base)
            self.assertEqual(rule.read_bytes(), b"Local uncommitted work.")
