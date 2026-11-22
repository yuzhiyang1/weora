"""用真实检出验证回归候选的选择与拒绝口径。"""

import argparse
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge, knowledge_improvement
from weora_cli.collab_client import Client, as_object, items


@unittest.skipUnless(shutil.which("git"), "需要实际 Git 命令")
class ImprovementTests(unittest.TestCase):
    def test_plan_requires_registered_checkout_and_a_real_commit(self) -> None:
        with TemporaryDirectory() as temporary:
            cwd = Path(temporary)

            def git(*args: str) -> str:
                return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

            git("init", "-q")
            git("config", "user.email", "test@example.test")
            git("config", "user.name", "Test User")
            git("commit", "--allow-empty", "-qm", "Original start")
            git("remote", "add", "origin", "git@github.com:team/repo.git")
            commit = git("rev-parse", "HEAD")
            git("tag", "-am", "A tag object does not hold a full commit id", "v1")
            tag = git("rev-parse", "v1")
            project, repo, proposal = str(uuid4()), str(uuid4()), str(uuid4())

            def subject(identifier: str, status: str, sha: str, version: int = 1) -> dict[str, object]:
                previous = {"label_id": 7, "git_start": {"status": "recorded", "repository_id": repo, "commit": sha},
                            "connection_version": version}
                return {"subject_id": identifier, "latest": {"git_start": {"status": status}}, "recorded": previous
                        if status != "tracked_changes" else None, "learnings": [{"title": "已关联经验"}]}

            subjects = [
                subject("older-is-usable", "not_recorded", commit),
                subject("missing", "recorded", "0" * len(commit)),
                subject("tag", "recorded", tag),
                subject("dirty", "tracked_changes", commit),
                subject("old-connection", "recorded", commit, 2),
                subject("covered", "tracked_changes", commit) | {"task": "benchmarks/regressions/tasks/landed.yaml"},
            ]
            parser = argparse.ArgumentParser()
            knowledge.arguments(parser)
            args = parser.parse_args(["digest", "--project", project, "--json", "--scorer", "quality", "--limit", "1"])
            client = Client("https://example.test", str(uuid4()), "device-token")
            with (
                patch.object(knowledge, "paired_client", return_value=client),
                patch.object(knowledge.Path, "cwd", return_value=cwd),
                patch.object(knowledge, "_call", return_value={"truncated": True}) as digest,
                patch.object(
                    knowledge_improvement, "call",
                    return_value={
                        "scorer": "quality", "subjects": subjects, "proposal_id": proposal, "repository_id": repo,
                    },
                ) as evidence,
                patch.object(
                    knowledge_improvement, "repository_plan",
                    return_value=[{"status": "ready", "repository_id": repo, "connection_version": 1}],
                ) as repo_plan,
            ):
                result = as_object(knowledge.execute(args))
            digest.assert_called_once_with(client, project, "/digest/failures", limit=1)
            evidence.assert_called_once_with(client, project, "/self-improvement/candidates", scorer="quality")
            repo_plan.assert_called_once_with(client, project, ["ssh://git@github.com/team/repo.git"])
            plan = as_object(result.get("improvement"))
            self.assertEqual(items({"items": plan.get("failures")}), [{
                "label_id": 7, "subject_id": "older-is-usable", "connection_version": 1,
                "git_start": {"status": "recorded", "repository_id": repo, "commit": commit},
                "learnings": [{"title": "已关联经验"}],
            }])
            self.assertEqual(plan["covered"], ["benchmarks/regressions/tasks/landed.yaml"])
            self.assertTrue(plan["hasProposal"])
            self.assertTrue(plan["ready"])
            self.assertEqual(plan["refused"], [
                {"subject_id": "missing", "why": "not_in_checkout"},
                {"subject_id": "tag", "why": "not_in_checkout"},
                {"subject_id": "dirty", "why": "tracked_changes"},
                {"subject_id": "old-connection", "why": "not_in_checkout"},
            ])
