"""使用本机真实 Git 裸库验证离线流程，替换托管平台的传输端点。"""

import json
import os
import shutil
import subprocess
import unittest
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from weora_cli import knowledge_classifier, knowledge_offline_repo
from weora_cli.cli import build_parser, execute
from weora_cli.collab_client import CollabError, as_object, items
from weora_cli.commands import JsonValue
from weora_cli.knowledge_offline_code import offline_code_writer
from weora_cli.knowledge_repo_import import RepositoryImportSource

_A = "https://github.com/fixture/orders.git"
_B = "https://github.com/fixture/payments.git"
_SSH_A = "ssh://git@github.com/fixture/orders"


@unittest.skipUnless(shutil.which("git"), "需要实际 Git 命令")
class OfflineRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / "out"
        self.home = self.root / "settings"
        self.upstreams: dict[str, Path] = {}
        self.transport_calls: list[tuple[str, str]] = []
        original = knowledge_offline_repo._git_bytes

        def fixture(
            cwd: Path, env: dict[str, str], *arguments: str, maximum: int, timeout: int = 45,
            payload: bytes | None = None,
        ) -> bytes:
            command = arguments[0]
            if command in {"clone", "ls-remote"}:
                self.assertIn(env.get("GIT_ALLOW_PROTOCOL"), {"https", "ssh"})
                remote = arguments[-2]
                self.transport_calls.append((command, remote))
                upstream = self.upstreams.get(remote)
                if upstream is None:
                    raise CollabError("KNOWLEDGE_OFFLINE_GIT", "测试仓库没有可用的远端引用。")
                arguments = (*arguments[:-2], upstream.as_uri(), arguments[-1])
                env = env | {"GIT_ALLOW_PROTOCOL": "file"}
            return original(cwd, env, *arguments, maximum=maximum, timeout=timeout, payload=payload)

        self.enterContext(patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(self.home / "unpaired.json")}))
        self.enterContext(patch(
            "weora_cli.knowledge.paired_client", side_effect=AssertionError("离线仓库请求了设备配对"),
        ))
        self.enterContext(patch.object(knowledge_offline_repo, "_git_token", return_value=None))
        self.enterContext(patch.object(knowledge_offline_repo, "_git_bytes", side_effect=fixture))

    def git(self, cwd: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-C", str(cwd), *arguments],
            capture_output=True, text=True, check=True, timeout=15,
        )
        return result.stdout.strip()

    def commit(self, repo: Path) -> str:
        self.git(repo, "add", "-A")
        self.git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "source")
        return self.git(repo, "rev-parse", "HEAD")

    def seed(self, slug: str, url: str) -> str:
        repo = self.root / slug
        repo.mkdir()
        self.git(repo, "init", "--initial-branch=main")
        (repo / "main.py").write_bytes(b"from billing import rate\n\ndef total(): return rate()\n")
        (repo / "billing.py").write_bytes(b"def rate(): return 2\n")
        (repo / ".private.py").write_bytes(b"def ignored(): pass\n")
        (repo / "billing.test.ts").write_bytes(b"export class Fixture {}\n")
        self.upstreams[url] = repo
        return self.commit(repo)

    def command(self, *arguments: str) -> dict[str, JsonValue]:
        return as_object(execute(build_parser().parse_args(["context", *arguments])))

    def test_preview_uses_only_remote_head_and_unchanged_incremental_skips_clone(self) -> None:
        head = self.seed("orders", _A)
        args = ("import", "--from-repo", _A, "--output", str(self.output), "--incremental", "--skip-enrich")
        preview = self.command(*args, "--dry-run")
        self.assertEqual(items(preview)[0]["headSha"], head)
        self.assertEqual(self.transport_calls, [("ls-remote", _A)])
        self.assertFalse(self.output.exists())
        self.assertFalse(self.home.exists())
        first = items(self.command(*args))[0]
        result = as_object(first["result"])
        self.assertEqual((result["headSha"], result["branch"], result["fileCount"]), (head, "main", 2))
        self.assertEqual(self.transport_calls[-2:], [("ls-remote", _A), ("clone", _A)])
        self.transport_calls.clear()
        self.assertIs(items(self.command(*args, "--dry-run"))[0]["unchanged"], True)
        unchanged = as_object(items(self.command(*args))[0]["result"])
        self.assertIs(unchanged["unchanged"], True)
        self.assertEqual(self.transport_calls, [("ls-remote", _A), ("ls-remote", _A)])
        self.assertEqual(unchanged["revision"], result["revision"])

        (self.upstreams[_A] / "billing.py").unlink()
        latest = self.commit(self.upstreams[_A])
        changed = as_object(items(self.command(*args))[0]["result"])
        self.assertEqual((changed["headSha"], changed["removed"], changed["reused"]), (latest, 1, 1))
        lint = self.command("codebase", "--output", str(self.output), "--lint")
        self.assertEqual(lint["issues"], [])

    def test_priority_aliases_partial_failure_and_resume_only_the_pending_remote(self) -> None:
        self.seed("orders", _A)
        self.upstreams[_SSH_A] = self.upstreams[_A]
        listing = self.root / "repos.yaml"
        listing.write_text(
            f"version: 1\nrepos:\n  - url: '{_A}'\n    priority: high\n    auth: public\n"
            f"  - url: '{_B}'\n  - url: '{_SSH_A}'\n    priority: low\n  - org: fixture\n",
            encoding="utf-8",
        )
        with self.assertRaises(CollabError) as error:
            self.command(
                "import", "--from-repo-list", str(listing), "--output", str(self.output),
                "--concurrency", "2", "--skip-enrich",
            )
        self.assertEqual(error.exception.code, "KNOWLEDGE_CODEBASE_BATCH_INCOMPLETE")
        report = as_object(error.exception.data)
        self.assertEqual((report["succeeded"], report["failed"], report["skipped"]), (1, 1, 2))
        self.assertEqual([row["status"] for row in items(report)], ["succeeded", "failed", "skipped", "skipped"])
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--status")["project"],
                         "github__fixture__orders")
        self.seed("payments", _B)
        self.transport_calls.clear()
        run = str(report["run_id"])
        resumed = self.command("import", "--resume", run)
        self.assertEqual((resumed["run_id"], resumed["succeeded"]), (run, 2))
        self.assertEqual(self.command("import", "--resume", run), resumed)
        self.assertEqual(self.transport_calls, [("clone", _B)])
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["issues"], [])

    def test_explicit_domains_update_offline_catalog_even_when_ai_is_skipped(self) -> None:
        self.seed("orders", _A)
        self.seed("payments", _B)
        common = ("--output", str(self.output), "--skip-enrich", "--incremental")
        with patch.object(knowledge_classifier, "_answer") as model:
            preview = self.command("import", "--from-repo", _A, "--domain", "订单", "--dry-run", *common)
            self.assertEqual(items(preview)[0]["domain"], "订单")
            self.assertFalse(self.output.exists())
            first = items(self.command("import", "--from-repo", _A, "--domain", "订单", *common))[0]
            self.assertEqual(as_object(first["design"])["domain"], "订单")
            self.transport_calls.clear()
            list_path = self.root / "repos.yaml"
            list_path.write_text(
                f"repos:\n- url: {_A}\n  domain: 商贸订单\n- url: {_B}\n  domain: 计费\n", encoding="utf-8",
            )
            result = self.command("import", "--from-repo-list", str(list_path), *common)
            self.assertEqual([as_object(row["design"])["domain"] for row in items(result)], ["商贸订单", "计费"])
            self.assertNotIn(("clone", _A), self.transport_calls)
            model.assert_not_called()
        catalog = self.output / "teamwiki/evidence/code/index.md"
        body = catalog.read_text(encoding="utf-8")
        self.assertIn("| 商贸订单 |", body)
        self.assertIn("| 计费 |", body)
        self.assertNotIn("| 订单 |", body)

    def test_repository_overview_merges_evidence_and_recovers_after_optional_failure(self) -> None:
        self.seed("orders", _A)
        asked: list[str] = []
        available = False

        def answer(driver: str, prompt: str, schema: str) -> str:
            evidence = as_object(json.loads(prompt.rsplit("\n", 1)[-1]))
            fields = json.loads(schema)["properties"]
            if "indexed_files" in evidence:
                asked.append("overview")
                self.assertIn("main.py", str(evidence["indexed_files"]))
                self.assertNotIn("return rate()", prompt)
                if not available:
                    raise CollabError("FIXTURE_OVERVIEW_RETRY", "模型暂时不可用")
                return json.dumps({"body": "# 计价导读\n\n总额由账单费率决定。"})
            asked.append("analysis")
            if "layer" in fields:
                return json.dumps({"domain": "财务", "layer": "service", "summary": "计算费率", "responsibilities": []})
            if "domain" in fields:
                return json.dumps({"domain": "计价", "description": "计算费用", "keywords": ["账单"]})
            return json.dumps({"body": "# 设计\n\n证据不足，仍需运行轨迹确认。"})

        args = ("import", "--from-repo", _A, "--output", str(self.output), "--incremental")
        teamwiki = self.output / "teamwiki"
        with (
            patch("weora_cli.knowledge_classifier.shutil.which", return_value="codex"),
            patch.object(knowledge_classifier, "_answer", side_effect=answer),
        ):
            first = items(self.command(*args, "--via", "codex"))[0]
            result = as_object(first["result"])
            design = as_object(first["design"])
            root = teamwiki / "evidence" / "code" / str(result["project"])
            snapshot = root / "snapshots" / str(result["revision"])
            evidence_path = snapshot / "overview.md"
            original = evidence_path.read_bytes()
            combined = snapshot / "_repository-overview.md"
            self.assertEqual(asked[0], "overview")
            self.assertEqual(design["repository_overview"], "pending")
            self.assertEqual(as_object(design["failures"])[combined.name], "FIXTURE_OVERVIEW_RETRY")
            self.assertFalse(combined.exists())
            self.assertIn("/overview.md", (root / "index.md").read_text(encoding="utf-8"))

            available = True
            asked.clear()
            self.transport_calls.clear()
            second = items(self.command(*args, "--via", "codex"))[0]
            self.assertEqual(as_object(second["design"])["repository_overview"], "generated")
            self.assertEqual(asked, ["overview"])
            self.assertEqual(self.transport_calls, [("ls-remote", _A)])
            text = combined.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(original.decode().strip()))
            self.assertEqual(text.count("## AI 架构导读"), 1)
            self.assertIn("总额由账单费率决定", text)
            self.assertEqual(evidence_path.read_bytes(), original)
            asked.clear()
            self.command(*args, "--via", "codex")
            self.assertEqual(asked, [])
            self.assertIn(combined.name, (root / "index.md").read_text(encoding="utf-8"))
            self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["issues"], [])

            (self.upstreams[_A] / "billing.py").write_bytes(b"def rate(): return 3\n")
            self.commit(self.upstreams[_A])
            self.command(*args, "--skip-enrich")
            self.assertEqual(asked, [])
            self.assertNotIn(combined.name, (root / "index.md").read_text(encoding="utf-8"))

    def test_offline_batch_resumes_missing_design_without_git_transport(self) -> None:
        self.seed("orders", _A)
        self.seed("payments", _B)
        list_path = self.root / "repos.yaml"
        list_path.write_text(f"repos:\n- url: {_A}\n- url: {_B}\n", encoding="utf-8")
        pending = True
        asked: list[tuple[str, str]] = []

        def answer(driver: str, prompt: str, schema: str) -> str:
            facts = as_object(json.loads(prompt.rsplit("\n", 1)[-1]))
            project = str(facts["project"])
            fields = json.loads(schema)["properties"]
            if "layer" in fields:
                return json.dumps({"domain": "计价", "layer": "service", "summary": "计算账单", "responsibilities": []})
            if "domain" in fields:
                asked.append((project, "domain"))
                return json.dumps({"domain": "计价", "description": "计算账单", "keywords": ["账单"]})
            stage = "scenario" if "architecture" in facts else str(facts.get("component", "architecture"))
            asked.append((project, stage))
            if pending and project.endswith("payments") and stage == "billing.py":
                raise CollabError("FIXTURE_MODEL_FAILURE", "测试模型失败")
            body = "# 设计\n\n证据不足，当前调用的业务语义需要项目说明。" if stage == "scenario" else "# 设计"
            return json.dumps({"body": body})

        args = ("import", "--from-repo-list", str(list_path), "--output", str(self.output), "--via", "codex")
        with (
            patch("weora_cli.knowledge_classifier.shutil.which", return_value="codex"),
            patch.object(knowledge_classifier, "_answer", side_effect=answer),
        ):
            with self.assertRaises(CollabError) as error:
                self.command(*args)
            report = as_object(error.exception.data)
            self.assertEqual(report["succeeded"], 1)
            self.assertEqual(items(report)[1]["phase"], "design")
            self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["issues"], [])
            asked.clear()
            self.transport_calls.clear()
            pending = False
            complete = self.command("import", "--resume", str(report["run_id"]))
        self.assertEqual(complete["succeeded"], 2)
        self.assertEqual(self.transport_calls, [])
        self.assertEqual(
            asked, [("github__fixture__payments", "billing.py"), ("github__fixture__payments", "scenario")],
        )

    def test_org_generates_list_and_writes_same_teamwiki(self) -> None:
        self.seed("orders", _A)
        fetched = [{"url": _A, "auth": "token", "priority": "normal"}]
        with chdir(self.root), patch("weora_cli.knowledge.list_org", return_value=fetched) as discovery:
            dry = self.command("import", "--from-org", "fixture", "--output", str(self.output), "--dry-run")
            self.assertTrue(dry["preview"])
            self.assertFalse(Path(str(dry["whitelist"])).exists())
            self.assertEqual(self.transport_calls, [])
            report = self.command("import", "--from-org", "fixture", "--output", str(self.output), "--skip-enrich")
            self.assertEqual(discovery.call_count, 2)
        self.assertEqual(report["succeeded"], 1)
        self.assertIn(_A, Path(str(report["whitelist"])).read_text(encoding="utf-8"))
        self.assertEqual(self.transport_calls, [("clone", _A)])
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["issues"], [])

    def test_busy_wiki_rejects_duplicate_writer_before_fetch(self) -> None:
        self.seed("orders", _A)
        with offline_code_writer(self.output), self.assertRaises(CollabError) as error:
            self.command("import", "--from-repo", _A, "--output", str(self.output), "--skip-enrich")
        self.assertEqual(error.exception.code, "KNOWLEDGE_OFFLINE_BUSY")
        self.assertEqual(self.transport_calls, [])
        run = str(as_object(error.exception.data)["run_id"])
        self.assertEqual(self.command("import", "--resume", run)["succeeded"], 1)


class OfflineTransportTests(unittest.TestCase):
    def test_credentials_are_scoped_to_https_repository_and_public_has_no_helper(self) -> None:
        private = knowledge_offline_repo._repository(RepositoryImportSource(_A))
        with patch.object(knowledge_offline_repo, "_git_token", return_value=("x-access-token", "fixture-token")):
            env = knowledge_offline_repo._git_env(private)
        configured = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        self.assertEqual(configured["http.followRedirects"], "false")
        self.assertTrue(configured[f"http.{_A}.extraHeader"].startswith("Authorization: Basic "))
        public = knowledge_offline_repo._repository(RepositoryImportSource(_A, "public"))
        with patch.object(knowledge_offline_repo, "_git_token", side_effect=AssertionError("公开模式查找了凭据")):
            env = knowledge_offline_repo._git_env(public)
        configured = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        self.assertEqual(configured["credential.helper"], "")
        self.assertFalse(any("Authorization" in value for value in configured.values()))

    def test_forced_ssh_uses_standard_provider_port(self) -> None:
        source = RepositoryImportSource("https://gitcode.com/team/module.git", "ssh")
        repo = knowledge_offline_repo._repository(source)
        self.assertEqual(repo.transport, "ssh://git@gitcode.com:2222/team/module.git")
        self.assertEqual(knowledge_offline_repo._git_env(repo)["GIT_ALLOW_PROTOCOL"], "ssh")
