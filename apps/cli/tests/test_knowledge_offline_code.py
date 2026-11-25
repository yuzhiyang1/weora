"""从 CLI 入口验证无平台配置的离线导入、更新与完整性检查。"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast


class OfflineCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "services" / "billing"
        self.source.mkdir(parents=True)
        self.output = self.root / "wiki"
        self.wiki = self.output / "teamwiki"

    def command(self, *args: str, exit_code: int = 0) -> dict[str, Any]:
        settings = self.root / "missing" / "device.json"
        result = subprocess.run(
            [sys.executable, "-m", "weora_cli", "context", *args],
            cwd=self.root, env=os.environ.copy() | {"WEORA_COLLAB_CONFIG": str(settings)},
            capture_output=True, encoding="utf-8", timeout=20, check=False,
        )
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        payload = json.loads(result.stdout or result.stderr)
        self.assertFalse(settings.exists())
        return cast(dict[str, Any], payload["data"] if payload["ok"] else payload["error"])

    def test_offline_baseline_reuses_facts_and_recomputes_relations_on_removal(self) -> None:
        caller = self.source / "main.py"
        target = self.source / "billing.py"
        caller.write_bytes(b"from billing import price\n\ndef total():\n    return price()\n")
        target.write_bytes(b"def price():\n    return 1\n")
        args = ("import", "--dir", str(self.source), "--output", str(self.output), "--incremental", "--skip-enrich")
        preview = self.command(*args, "--dry-run")
        self.assertEqual(preview["candidates"], 2)
        self.assertFalse(self.output.exists())
        first = self.command(*args)
        self.assertEqual((first["project"], first["extracted"]), ("billing", 2))
        graph_path = self.wiki / ".indices" / "graph-index.json"
        graph = json.loads(graph_path.read_bytes())
        self.assertIn(
            {"kind": "REFERENCES", "source": "billing/main.py", "target": "billing/billing.py"},
            graph["edges"],
        )
        source_path = self.wiki / "evidence" / "code" / "billing"
        curated = source_path / "design.md"
        curated.write_text("# Team notes", encoding="utf-8")
        repeat = self.command(*args)
        self.assertEqual((repeat["reused"], repeat["revision"]), (2, first["revision"]))
        target.unlink()
        current = self.command(*args)
        self.assertEqual((current["reused"], current["removed"]), (1, 1))
        self.assertEqual(curated.read_text(encoding="utf-8"), "# Team notes")
        self.assertNotIn("billing/billing.py", str(json.loads(graph_path.read_bytes())))
        status = self.command("codebase", "--output", str(self.output), "--status")
        self.assertEqual((status["fileCount"], status["revision"]), (1, current["revision"]))
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["summary"]["high"], 0)
        manifest = json.loads((source_path / "source-manifest.json").read_bytes())
        relative = next(path for path in manifest["artifacts"] if path.startswith("files/"))
        evidence = source_path / "snapshots" / manifest["revision"] / relative
        evidence.write_text("changed", encoding="utf-8")
        before = graph_path.stat().st_mtime_ns
        lint = self.command("codebase", "--output", str(self.output), "--lint", exit_code=1)
        self.assertTrue(any(item["category"] == "evidence-invalid" for item in lint["issues"]))
        self.assertEqual(graph_path.stat().st_mtime_ns, before)
        self.assertEqual(evidence.read_text(encoding="utf-8"), "changed")
        self.assertEqual(self.command(*args)["reused"], 1)
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["issues"], [])

    def test_offline_gap_evidence_keeps_source_lines_in_each_snapshot(self) -> None:
        caller = self.source / "tracking.py"
        caller.write_text("\nimport orrerybridge.transit\n", encoding="utf-8")
        args = ("import", "--dir", str(self.source), "--output", str(self.output), "--incremental", "--skip-enrich")
        self.command(*args)
        source = self.wiki / "evidence" / "code" / "billing"
        evidence_file = "gaps/evidence.json"
        manifest = json.loads((source / "source-manifest.json").read_bytes())
        previous = source / "snapshots" / manifest["revision"] / evidence_file
        self.assertEqual(hashlib.sha256(previous.read_bytes()).hexdigest(), manifest["artifacts"][evidence_file])
        [gap] = json.loads(previous.read_bytes())
        self.assertEqual(gap["sources"], [{
            "name": "orrerybridge.transit", "alias": "orrerybridge", "line": 2, "path": "tracking.py",
        }])
        self.assertIn(f"snapshots/{manifest['revision']}/{evidence_file}",
                      (self.wiki / "gaps" / "detected.md").read_text(encoding="utf-8"))
        caller.write_text("def track(): pass\n", encoding="utf-8")
        self.command(*args)
        current = json.loads((source / "source-manifest.json").read_bytes())
        self.assertNotEqual(current["revision"], manifest["revision"])
        self.assertEqual(json.loads((source / "snapshots" / current["revision"] / evidence_file).read_bytes()), [])
        self.assertEqual(json.loads(previous.read_bytes()), [gap])

    def test_same_name_keeps_distinct_sources_and_explicit_status(self) -> None:
        other = self.root / "tools" / self.source.name
        other.mkdir(parents=True)
        (other / "client.ts").write_text("export const example = 1", encoding="utf-8")
        (self.source / "service.py").write_text("def total(): pass", encoding="utf-8")
        (self.source / "config.py").write_text("def resolve(): pass", encoding="utf-8")
        common = ("--output", str(self.output), "--max-files", "1", "--skip-enrich")
        first = self.command("import", "--dir", str(self.source), *common)
        second = self.command("import", "--dir", str(other), *common)
        self.assertTrue(first["truncated"])
        self.assertRegex(second["project"], r"^billing-[a-f0-9]{8}$")
        lint = self.command("codebase", "--output", str(self.output), "--lint")
        self.assertEqual(lint["summary"]["medium"], 1)
        for project in (first, second):
            selected = self.command("codebase", "--output", str(self.output), "--project", project["project"])
            self.assertEqual(selected["revision"], project["revision"])

    def test_reconcile_previews_then_replaces_only_the_root_graph(self) -> None:
        source = self.source / "ledger.py"
        source.write_text("class BillingLedger: pass\n", encoding="utf-8")
        imports = ("import", "--dir", str(self.source), "--output", str(self.output), "--skip-enrich")
        self.command(*imports)
        product = self.wiki / "product" / "billing.md"
        product.parent.mkdir()
        product.write_text(
            "# 产品账单\nupdated: 2026-08-01\nBillingLedger has 3 states: `A|B|C` and is blocking.\n"
            "GET /bill\nPOST /missing\n",
            encoding="utf-8",
        )
        code = self.wiki / "docs" / "billing-api.md"
        code.parent.mkdir()
        code.write_text(
            "# BillingLedger\nupdated: 2026-11-01\nBillingLedger has 2 states: `A|B` and is non-blocking.\nGET /bill\n",
            encoding="utf-8",
        )
        rule = self.output / "rules" / "billing.md"
        rule.parent.mkdir()
        rule.write_text("# Billing rule\nUse `BillingLedger`.\n", encoding="utf-8")
        graph = self.wiki / ".indices" / "graph-index.json"
        args = ("codebase", "--output", str(self.output), "--reconcile")

        def files() -> dict[Path, tuple[bytes, int]]:
            return {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.output.rglob("*") if path.is_file() and not path.name.endswith(".lock")
            }

        original = files()
        result = self.command(*args, "--dry-run")
        self.assertEqual(files(), original)
        self.assertTrue(result["preview"])
        self.assertEqual(
            {row["target"]["id"] for row in result["mappings"]}, {"billing/ledger.py", "wiki:docs/billing-api.md"},
        )
        self.assertEqual({row["kind"] for row in result["conflicts"]}, {
            "COUNT_MISMATCH", "STATE_MISMATCH", "BEHAVIOR_MISMATCH",
        })
        self.assertEqual(result["gaps"][0]["evidence"], ["POST /missing"])
        self.assertEqual(result["api_matches"][0]["confidence"], 1.0)
        self.assertEqual(result["stale_warnings"][0]["severity"], "critical")
        self.assertEqual({row["source"]["path"] for row in result["relation_candidates"]}, {"rules/billing.md"})

        applied = self.command(*args)
        self.assertTrue(applied["changed"])
        self.assertEqual(applied["relation_candidates"], [])
        self.assertEqual(
            {p: value for p, value in files().items() if p != graph},
            {p: value for p, value in original.items() if p != graph},
        )
        self.assertIn("product_reconciliation", graph.read_text(encoding="utf-8"))
        self.assertNotIn("resource:rules/billing.md", graph.read_text(encoding="utf-8"))
        stamp = graph.stat().st_mtime_ns
        self.assertFalse(self.command(*args)["changed"])
        self.assertEqual(graph.stat().st_mtime_ns, stamp)
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["summary"]["high"], 0)

        source.write_text("class OtherLedger: pass\n", encoding="utf-8")
        self.command(*imports, "--incremental")
        edges = json.loads(graph.read_bytes())["edges"]
        self.assertEqual([row["target"] for row in edges if row["kind"] == "MAPS_TO"], ["wiki:docs/billing-api.md"])
        code.unlink()
        self.assertTrue(self.command(*args)["changed"])
        self.assertNotIn("product_reconciliation", graph.read_text(encoding="utf-8"))

    def test_offline_typescript_alias_reexport_updates_graph(self) -> None:
        config = self.source / "tsconfig.json"
        config.write_text('{"compilerOptions":{"paths":{"@core":["barrel.ts"]}}}', encoding="utf-8")
        (self.source / "entry.ts").write_text('import {send} from "@core";\nsend();\n', encoding="utf-8")
        (self.source / "barrel.ts").write_text('export {dispatch as send} from "./worker";\n', encoding="utf-8")
        (self.source / "worker.ts").write_text("export function dispatch() {}\n", encoding="utf-8")
        args = ("import", "--dir", str(self.source), "--output", str(self.output), "--incremental", "--skip-enrich")
        graph = self.wiki / ".indices" / "graph-index.json"
        self.assertEqual(self.command(*args)["extracted"], 4)
        link = {"kind": "REFERENCES", "source": "billing/entry.ts", "target": "billing/worker.ts"}
        self.assertIn(link, json.loads(graph.read_bytes())["edges"])
        config.write_text('{"compilerOptions":{"paths":{"@core":["missing.ts"]}}}', encoding="utf-8")
        self.assertEqual(self.command(*args)["reused"], 3)
        self.assertNotIn(link, json.loads(graph.read_bytes())["edges"])
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["summary"]["high"], 0)

    def test_offline_swift_calls_use_module_boundaries_and_refresh(self) -> None:
        module = self.source / "Sources" / "Bill"
        module.mkdir(parents=True)
        (module / "Plan.swift").write_text("func plan() { submit() }\n", encoding="utf-8")
        target = module / "Submit.swift"
        target.write_text("func submit() {}\n", encoding="utf-8")
        other = self.source / "Packages" / "Other" / "Sources" / "Bill"
        other.mkdir(parents=True)
        (other / "Submit.swift").write_text("func submit() {}\n", encoding="utf-8")
        args = ("import", "--dir", str(self.source), "--output", str(self.output), "--incremental", "--skip-enrich")
        graph = self.wiki / ".indices" / "graph-index.json"
        self.assertEqual(self.command(*args)["extracted"], 3)
        edge = {"source": "billing/Sources/Bill/Plan.swift", "target": "billing/Sources/Bill/Submit.swift",
                "kind": "REFERENCES"}
        references = [row for row in json.loads(graph.read_bytes())["edges"] if row["kind"] == "REFERENCES"]
        self.assertEqual(references, [edge])
        target.write_text("fileprivate func submit() {}\n", encoding="utf-8")
        self.assertEqual(self.command(*args)["reused"], 2)
        self.assertNotIn(edge, json.loads(graph.read_bytes())["edges"])
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["summary"]["high"], 0)

    def test_offline_go_graph_uses_go_mod_and_removes_the_stale_edge(self) -> None:
        module = self.source / "go.mod"
        module.write_text("module example.org/payments\n", encoding="utf-8")
        lib = self.source / "ledger" / "v2"
        lib.mkdir(parents=True)
        (lib / "index.go").write_text("package ledger\nfunc Load() {}\n", encoding="utf-8")
        (self.source / "charge.go").write_text(
            'package charge\nimport "example.org/payments/ledger/v2"\nfunc Charge() { ledger.Load() }\n',
            encoding="utf-8",
        )
        plan = self.command("import", "--dir", str(self.source), "--output", str(self.output), "--dry-run",
                            "--max-files", "1", "--skip-enrich")
        self.assertEqual(plan["files"], ["go.mod"])

        args = ("import", "--dir", str(self.source), "--output", str(self.output), "--incremental", "--skip-enrich")
        graph = self.wiki / ".indices" / "graph-index.json"
        first = self.command(*args)
        self.assertEqual(first["extracted"], 3)
        nodes = json.loads(graph.read_bytes())
        edge = {"source": "billing/charge.go", "target": "billing/ledger/v2/index.go", "kind": "REFERENCES"}
        self.assertIn(edge, nodes["edges"])
        module.write_text("module other.example/replacement\n", encoding="utf-8")
        changed = self.command(*args)
        self.assertEqual(changed["reused"], 2)
        self.assertNotIn(edge, json.loads(graph.read_bytes())["edges"])

    def test_offline_javascript_call_edges_follow_module_changes(self) -> None:
        app = self.source / "app.jsx"
        app.write_text('import * as Ledger from "./ledger.js";\nconst app = <div>{Ledger.refresh()}</div>;\n',
                       encoding="utf-8")
        ledger = self.source / "ledger.ts"
        ledger.write_text("export function refresh() {}\n", encoding="utf-8")
        args = ("import", "--dir", str(self.source), "--output", str(self.output), "--skip-enrich", "--incremental")
        graph = self.wiki / ".indices" / "graph-index.json"
        edge = {"source": "billing/app.jsx", "target": "billing/ledger.ts", "kind": "REFERENCES"}

        first = self.command(*args)
        self.assertIn(edge, json.loads(graph.read_bytes())["edges"])
        snapshots = self.wiki / "evidence" / "code" / "billing" / "snapshots"
        pages = (snapshots / first["revision"] / "files").glob("*.md")
        self.assertTrue(any("Ledger.refresh()" in (text := page.read_text(encoding="utf-8")) and "EXTRACTED" in text
                            for page in pages))

        ledger.unlink()
        self.assertEqual(self.command(*args)["reused"], 1)
        self.assertNotIn(edge, json.loads(graph.read_bytes())["edges"])
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["summary"]["high"], 0)

    def test_offline_configuration_and_swift_graph_follow_source_changes(self) -> None:
        swift = self.source / "Sources" / "Billing"
        swift.mkdir(parents=True)
        implementation = swift / "Payment.swift"
        implementation.write_text("struct PaymentAdapter: ReceiptPort {}\n", encoding="utf-8")
        protocol = swift / "Contract.swift"
        protocol.write_text("public protocol ReceiptPort {}\n", encoding="utf-8")
        tests = self.source / "Tests" / "Billing"
        tests.mkdir(parents=True)
        (tests / "Payment.swift").write_text("struct TestAdapter: TestPort, ReceiptPort {}\n", encoding="utf-8")
        (tests / "Contract.swift").write_text("protocol TestPort {}\n", encoding="utf-8")
        (tests / "outside.py").write_text("def excluded(): pass\n", encoding="utf-8")
        config_directory = self.root / "deployment"
        config_directory.mkdir()
        config = config_directory / "runtime.conf"
        config.write_text('[billing]\nPAYMENT_ADAPTER = "fixture-private-value"\n', encoding="utf-8")
        args = ("--output", str(self.output), "--skip-enrich", "--incremental")

        def publish(directory: Path) -> dict[str, Any]:
            return self.command("import", "--dir", str(directory), *args)

        def edges() -> set[tuple[str, str, str]]:
            graph = json.loads((self.wiki / ".indices" / "graph-index.json").read_bytes())
            return {(row["source"], row["target"], row["kind"]) for row in graph["edges"]}

        self.assertEqual(publish(self.source)["extracted"], 4)
        self.assertEqual(publish(config_directory)["extracted"], 1)
        caller = "billing/Sources/Billing/Payment.swift"
        conformance = (caller, "billing/Sources/Billing/Contract.swift", "IMPLEMENTS")
        dependency = (caller, "deployment/runtime.conf", "DEPENDS_ON")
        test_conformance = ("billing/Tests/Billing/Payment.swift", "billing/Tests/Billing/Contract.swift", "IMPLEMENTS")
        self.assertTrue({conformance, dependency, test_conformance} <= edges())
        self.assertNotIn(("billing/Tests/Billing/Payment.swift", conformance[1], "IMPLEMENTS"), edges())
        for document in self.wiki.rglob("*.md"):
            self.assertNotIn("fixture-private-value", document.read_text(encoding="utf-8"))
        protocol.write_text("struct ReceiptPort {}\n", encoding="utf-8")
        self.assertEqual(publish(self.source)["reused"], 3)
        self.assertNotIn(conformance, edges())
        config.write_text("[billing]\nANOTHER_KEY = 1\n", encoding="utf-8")
        publish(config_directory)
        self.assertNotIn(dependency, edges())
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["summary"]["high"], 0)

    def test_cross_repository_edges_rebuild_after_target_changes(self) -> None:
        consumer = self.source / "index.py"
        consumer.write_bytes(b"from gateway import PaymentAdapter\n")
        target = self.root / "provider"
        target.mkdir()
        endpoint = target / "payment.py"
        endpoint.write_bytes(b"class PaymentAdapter: pass\n")
        config = self.root / "data"
        config.mkdir()
        (self.source / "bill.py").write_bytes(b"class BillingInvoice: pass\n")
        (config / "schema.sql").write_bytes(b"CREATE TABLE ledger.billing_invoice (id int);\n")

        def publish(directory: Path) -> dict[str, Any]:
            return self.command("import", "--dir", str(directory), "--output", str(self.output), "--skip-enrich")

        graph = self.wiki / ".indices" / "graph-index.json"

        def inferred() -> list[dict[str, Any]]:
            return [edge for edge in json.loads(graph.read_bytes())["edges"] if "derived" in edge]

        origin = publish(self.source)
        provider = publish(target)
        schema = publish(config)
        links = inferred()
        self.assertEqual({
            (edge["source"], edge["target"], edge["method"]) for edge in links
        }, {
            ("billing/index.py", "provider/payment.py", "import"),
            ("billing/bill.py", "data/schema.sql", "configuration"),
        })
        revisions = {origin["project"]: origin["revision"], provider["project"]: provider["revision"],
                     schema["project"]: schema["revision"]}
        for edge in links:
            self.assertEqual(edge["source_revision"], revisions[edge["source"].split("/")[0]])
            self.assertEqual(edge["target_revision"], revisions[edge["target"].split("/")[0]])
        endpoint.write_bytes(b"class PaymentGateway: pass\n")
        publish(target)
        self.assertEqual([edge["target"] for edge in inferred()], ["data/schema.sql"])
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["issues"], [])

    @unittest.skipUnless(shutil.which("git"), "需要实际 Git 命令")
    def test_distinct_subdirectories_and_equivalent_remote_roots(self) -> None:
        def git(cwd: Path, *arguments: str) -> str:
            return subprocess.check_output(
                ["git", "-c", "core.autocrlf=false", "-C", str(cwd), *arguments], encoding="utf-8",
            ).strip()

        child = self.source / "pricing"
        child.mkdir()
        (child / "service.py").write_bytes(b"def price(): return 1\n")
        git(self.source, "init", "-q")
        git(self.source, "remote", "add", "origin", "git@github.com:book/billing.git")
        git(self.source, "add", ".")
        git(self.source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "code")
        common = ("--output", str(self.output), "--incremental", "--skip-enrich")
        first = self.command("import", "--dir", str(self.source), *common)
        self.assertEqual(first["project"], "github__book__billing")
        self.assertEqual(first["headSha"], git(self.source, "rev-parse", "HEAD"))
        subdir = self.command("import", "--dir", str(child), *common)
        self.assertEqual((subdir["project"], subdir["sourceSubdir"]), ("pricing", "pricing"))
        clone = self.root / "clone"
        git(self.root, "clone", "-q", str(self.source), str(clone))
        git(clone, "remote", "set-url", "origin", "https://github.com/book/billing.git")
        equivalent = self.command("import", "--dir", str(clone), *common)
        self.assertEqual((equivalent["project"], equivalent["reused"]), (first["project"], 1))
        self.assertEqual(self.command("codebase", "--output", str(self.output), "--lint")["issues"], [])

    def test_symlinked_teamwiki_cannot_redirect_writes(self) -> None:
        (self.source / "main.py").write_text("def run(): pass", encoding="utf-8")
        self.output.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        try:
            self.wiki.symlink_to(outside, target_is_directory=True)
        except OSError:
            if sys.platform != "win32":
                self.skipTest("当前设备未授权创建符号链接")
            junction = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "New-Item -ItemType Junction -Path $env:WEORA_TEST_LINK -Target $env:WEORA_TEST_TARGET"],
                env=os.environ.copy() | {"WEORA_TEST_LINK": str(self.wiki), "WEORA_TEST_TARGET": str(outside)},
                capture_output=True, timeout=20, check=False,
            )
            if junction.returncode != 0:
                self.skipTest("当前设备未授权创建目录联接")
        error = self.command(
            "import", "--dir", str(self.source), "--output", str(self.output), "--skip-enrich", exit_code=1,
        )
        self.assertEqual(error["code"], "KNOWLEDGE_OFFLINE_PATH")
        self.assertEqual(list(outside.iterdir()), [])
