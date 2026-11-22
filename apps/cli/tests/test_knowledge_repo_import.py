"""验证仓库清单、组织筛选和批量续做。"""

import argparse
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge, knowledge_org, knowledge_repo_import
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue


class RepositoryImportTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        env = patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(self.root / "device.json")})
        env.start()
        self.addCleanup(env.stop)
        self.client = Client("https://example.invalid", str(uuid4()), "fixture")
        self.project = str(uuid4())
        self.parser = argparse.ArgumentParser()
        knowledge.arguments(self.parser)

    def test_list_priority_and_scoped_resume_after_one_failed_repository(self) -> None:
        high, low = str(uuid4()), str(uuid4())
        draft = self.root / "repos.yaml"
        draft.write_text(
            f"version: 1\nrepos:\n- repository_id: {low}\n  priority: low\n"
            f"- repository_id: {high}\n  priority: high\n- org: https://github.com/example\n",
            encoding="utf-8",
        )
        operations: dict[str, list[JsonValue]] = {high: [], low: []}
        barrier = Barrier(2)

        def response(
            client: Client,
            scope: str,
            suffix: str,
            *,
            method: str,
            payload: dict[str, JsonValue],
        ) -> dict[str, JsonValue]:
            self.assertEqual((client.workspace, scope, method), (self.client.workspace, self.project, "POST"))
            if suffix.endswith("/repo-plan"):
                sources = payload["sources"]
                assert isinstance(sources, list)
                return {
                    "items": [
                        {"source": source, "status": "ready", "repository_id": source, "connection_version": 4}
                        for source in sources
                    ]
                }
            target = suffix.rsplit("/", 1)[-1]
            operations[target].append(payload["operation_id"])
            if len(operations[target]) == 1:
                barrier.wait(timeout=5)
                if target == low:
                    raise CollabError("WEORA_UNREACHABLE", "未收到索引响应")
            self.assertEqual(payload["expected_connection_version"], 4)
            return {"indexed": 2}

        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge_repo_import, "call", side_effect=response),
        ):
            args = self.parser.parse_args(
                [
                    "import",
                    "--from-repo-list",
                    str(draft),
                    "--project",
                    self.project,
                    "--concurrency",
                    "2",
                    "--skip-enrich",
                ]
            )
            with self.assertRaises(CollabError) as failed:
                knowledge.execute(args)
            report = as_object(failed.exception.data)
            self.assertEqual(failed.exception.code, "KNOWLEDGE_CODEBASE_BATCH_INCOMPLETE")
            self.assertEqual((report["succeeded"], report["failed"], report["skipped"]), (1, 1, 1))
            rows = report["items"]
            assert isinstance(rows, list)
            self.assertEqual([as_object(row).get("source") for row in rows[:2]], [high, low])
            resume = self.parser.parse_args(["import", "--resume", str(report["run_id"])])
            confirmed = as_object(knowledge.execute(resume))
            self.assertEqual(confirmed["succeeded"], 2)
            self.assertEqual(knowledge.execute(resume), confirmed)
        self.assertEqual(len(operations[high]), 1)
        self.assertEqual(operations[low], [operations[low][0]] * 2)

    def test_resume_after_design_failure_does_not_plan_or_index_ready_sources_again(self) -> None:
        first, second = str(uuid4()), str(uuid4())
        draft = self.root / "repos.yaml"
        draft.write_text(
            f"repos:\n- repository_id: {first}\n  domain: 订单\n- repository_id: {second}\n", encoding="utf-8",
        )
        index: dict[str, JsonValue] = {"revision": "a" * 40, "generation": 3, "connection_version": 4}

        def request(*args: object, **kwargs: JsonValue) -> dict[str, JsonValue]:
            if str(args[2]).endswith("/repo-plan"):
                payload = as_object(kwargs["payload"])
                sources = payload["sources"]
                assert isinstance(sources, list)
                return {"items": [
                    {"source": source, "status": "ready", "repository_id": source, "connection_version": 4}
                    for source in sources
                ]}
            return index

        unfinished = True

        def enrich(*args: object, **kwargs: JsonValue) -> dict[str, JsonValue]:
            self.assertEqual(kwargs["expected_index"], index)
            self.assertEqual(kwargs["driver"], "codex")
            self.assertEqual(kwargs["domain_override"], "订单" if first in str(args[2]) else None)
            if first in str(args[2]) and unfinished:
                raise CollabError("KNOWLEDGE_CODE_ENRICH_INCOMPLETE", "组件未完成", {"missing_components": ["api"]})
            return {"complete": True}

        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch("weora_cli.knowledge_classifier.shutil.which", return_value="codex"),
            patch.object(knowledge_repo_import, "call", side_effect=request) as transport,
            patch.object(knowledge_repo_import, "enrich_platform", side_effect=enrich) as design,
        ):
            args = self.parser.parse_args(
                ["import", "--from-repo-list", str(draft), "--project", self.project, "--via", "codex"],
            )
            with self.assertRaises(CollabError) as failure:
                knowledge.execute(args)
            report = as_object(failure.exception.data)
            self.assertEqual((report["failed"], report["succeeded"]), (1, 1))
            rows = report["items"]
            assert isinstance(rows, list)
            self.assertEqual(as_object(rows[0])["phase"], "design")
            self.assertEqual(as_object(rows[0])["result"], index)
            self.assertEqual(transport.call_count, 3)
            unfinished = False
            transport.reset_mock()
            design.reset_mock()
            resume = self.parser.parse_args(["import", "--resume", str(report["run_id"])])
            completed = as_object(knowledge.execute(resume))
            self.assertEqual(completed["succeeded"], 2)
            transport.assert_not_called()
            self.assertEqual(design.call_count, 1)
            self.assertIn(first, design.call_args.args[2])
            self.assertEqual(knowledge.execute(resume), completed)

    def test_single_explicit_domain_publishes_without_an_agent(self) -> None:
        repo = str(uuid4())
        index = {"revision": "a" * 40, "generation": 1, "connection_version": 2}
        basis = "b" * 64
        body = json.dumps({"domain": "发票", "description": "", "keywords": [], "components": ["billing"]})

        def design(*args: object, **kwargs: JsonValue) -> dict[str, JsonValue]:
            self.assertEqual(args[2], f"/codebases/{repo}/design")
            if kwargs.get("method") == "POST":
                self.assertEqual(kwargs["payload"], {"basis": basis, "domain_override": "发票"})
                return {"basis": basis, "items": [{"id": str(uuid4()), "name": "_domains.json", "body": body}]}
            return {
                "project": "billing", "interactions": [], "basis": basis, "components": [],
                "architecture_context": {}, "domain_context": {}, "documents": {}, **index,
            }

        args = self.parser.parse_args([
            "import", "--from-repo", repo, "--project", self.project, "--skip-enrich", "--domain", "发票",
        ])
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_call", return_value=index) as index_call,
            patch("weora_cli.knowledge_code_enrichment.call", side_effect=design) as design_call,
            patch("weora_cli.knowledge_classifier._answer") as model,
        ):
            result = as_object(knowledge.execute(args))
            self.assertEqual(as_object(result["design"])["domain"], "发票")
            self.assertEqual(as_object(result["design"])["skipped"], "ai_enrichment")
            index_call.assert_called_once()
            self.assertEqual(design_call.call_count, 2)
            model.assert_not_called()

    def test_invalid_concurrency_is_refused_before_reading_list(self) -> None:
        with patch.object(knowledge, "read_repo_list") as loader, self.assertRaises(CollabError):
            knowledge.execute(
                self.parser.parse_args(
                    [
                        "import",
                        "--from-repo-list",
                        "missing.yaml",
                        "--project",
                        self.project,
                        "--concurrency",
                        "0",
                    ]
                )
            )
        loader.assert_not_called()

    def test_org_preview_does_not_write_draft_and_skip_import_only_writes_yaml(self) -> None:
        repositories = [{"url": "https://github.com/acme/core.git", "auth": "token", "priority": "normal"}]
        path = self.root / ".weora" / "repo-whitelist.draft.yaml"
        with (
            patch.object(Path, "cwd", return_value=self.root),
            patch.object(knowledge, "list_org", return_value=repositories) as discover,
            patch.object(knowledge, "paired_client") as paired,
        ):
            preview = self.parser.parse_args(["import", "--from-org", "acme", "--dry-run"])
            self.assertEqual(as_object(knowledge.execute(preview))["repositories"], [repositories[0]["url"]])
            self.assertFalse(path.exists())
            self.assertIs(discover.call_args.kwargs["include_archived"], False)
            self.assertEqual(discover.call_args.args, ("acme", 200))
            saved = self.parser.parse_args(["import", "--from-org", "acme", "--skip-import"])
            self.assertEqual(as_object(knowledge.execute(saved))["whitelist"], str(path))
            self.assertEqual(knowledge_repo_import.load_repo_list(path), ([repositories[0]["url"]], []))
            paired.assert_not_called()

    def test_platform_list_fallbacks_keep_the_same_host_and_use_namespace_paths(self) -> None:
        missing = CollabError("KNOWLEDGE_MR_ACCESS_DENIED", "模拟 404", {"status": 404})
        cases = (
            (
                "acme",
                [missing, [{"full_name": "acme/example", "clone_url": "https://other.invalid"}]],
                "/orgs/acme/repos",
                "/users/acme/repos",
                "https://github.com/acme/example.git",
            ),
            (
                "gitcode.com/acme",
                [[{"full_name": "acme/example", "archived": False}]],
                "/orgs/acme/repos",
                "/orgs/acme/repos",
                "https://gitcode.com/acme/example.git",
            ),
        )
        for org, responses, first, last, url in cases:
            with (
                self.subTest(org=org),
                patch.object(knowledge_org, "_authorization", return_value={}),
                patch.object(knowledge_org, "_page", side_effect=responses) as fetch,
            ):
                listed = knowledge_org.list_org(org, 10, include=None, exclude=None, include_archived=False)
                self.assertEqual(listed[0]["url"], url)
                self.assertEqual(fetch.call_args_list[0].args[2], first)
                self.assertEqual(fetch.call_args.args[2], last)
