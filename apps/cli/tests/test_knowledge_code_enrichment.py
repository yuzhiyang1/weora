"""验证离线深度生成的 CLI、文件证据和按阶段续做。"""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge_classifier
from weora_cli.cli import build_parser, execute
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue

_DIAGRAM = """# 订单金额

```mermaid
sequenceDiagram
    participant H as handler
    participant S as storage
    H->>S: read rate
    S-->>H: rate
```

处理器读取费率后计算金额。
"""


_DOMAIN = {"domain": "订单计价", "description": "计算订单费用", "keywords": ["费率", "订单"]}
_PROFILE = {
    "domain": "订单", "layer": "service", "summary": "提供计价能力", "responsibilities": ["提供费率和费用计算"],
}


class CodeEnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "checkout"
        self.output = self.root / "wiki"
        for name in ("handler", "storage"):
            (self.source / name).mkdir(parents=True)
        (self.source / "handler" / "order.py").write_bytes(
            b"from storage.invoice import rate\n\ndef total():\n    return rate()\n",
        )
        self.target = self.source / "storage" / "invoice.py"
        self.target.write_bytes(b"def rate():\n    return 2\n")
        (self.source / "storage" / "contract.ts").write_bytes(b"export interface PaymentPort {}\n")
        self.enterContext(patch(
            "weora_cli.knowledge.paired_client", side_effect=AssertionError("离线设计请求了平台配对"),
        ))
        self.initial = self.refresh()
        self.project = str(self.initial["project"])
        self.revision = str(self.initial["revision"])
        self.base = self.output / "teamwiki" / "evidence" / "code" / self.project
        self.docs = self.base / "snapshots" / self.revision / "docs"

    def command(self, *arguments: str) -> dict[str, JsonValue]:
        return as_object(execute(build_parser().parse_args(["context", *arguments])))

    def enrich(self, *arguments: str) -> dict[str, JsonValue]:
        return self.command("codebase", "--deep-enrich", "--output", str(self.output), "--project", self.project,
                            *arguments)

    def refresh(self) -> dict[str, JsonValue]:
        return self.command(
            "import", "--dir", str(self.source), "--output", str(self.output), "--incremental", "--skip-enrich",
        )

    def test_preview_and_explicit_unavailable_tool_write_nothing(self) -> None:
        manifests = list(self.base.glob("snapshots/*/_manifest.json"))
        self.assertEqual(len(manifests), 1)
        component_manifest = json.loads(manifests[0].read_bytes())
        self.assertEqual([part["slug"] for part in component_manifest["components"]], ["handler", "storage"])
        before = (self.base / "index.md").read_bytes()
        with patch.dict(os.environ, {"WEORA_AI_TOOL": "unavailable-tool"}):
            plan = self.enrich("--dry-run")
            self.assertEqual(plan["missing_components"], ["handler", "storage"])
            self.assertEqual(plan["scenarios"], "pending")
            self.assertEqual(plan["domain_status"], "pending")
            with self.assertRaises(CollabError) as failure:
                self.enrich()
        self.assertEqual(failure.exception.code, "KNOWLEDGE_CODE_ENRICH_TOOL")
        self.assertFalse(self.docs.exists())
        self.assertEqual((self.base / "index.md").read_bytes(), before)

    def test_parallel_components_preserve_partial_outputs_and_resume_only_missing(self) -> None:
        together = Barrier(2, timeout=5)
        calls: list[tuple[str, dict[str, JsonValue]]] = []
        fail_storage = True

        def model(driver: str, prompt: str, schema: str, **_: object) -> str:
            self.assertEqual(driver, "codex")
            evidence = as_object(json.loads(prompt.rsplit("\n", 1)[-1]))
            fields = json.loads(schema)["properties"]
            if "layer" in fields:
                calls.append(("profile", evidence))
                return json.dumps(_PROFILE)
            if "domain" in fields:
                calls.append(("domain", evidence))
                self.assertIn("提供计价能力", str(evidence["module_profiles"]))
                self.assertIn("handler/order.py:3", str(evidence["declarations"]))
                return json.dumps(_DOMAIN)
            self.assertIn("body", json.loads(schema)["properties"])
            if component := evidence.get("component"):
                calls.append((str(component), evidence))
                if fail_storage:
                    together.wait()
                    if component == "storage":
                        raise CollabError("MODEL_TEMPORARY_FAILURE", "测试中的组件调用失败。")
                return json.dumps({"body": f"# {component} 设计\n\n定位证据见代码索引。"})
            if "architecture" in evidence:
                calls.append(("scenario", evidence))
                return json.dumps({"body": _DIAGRAM})
            calls.append(("architecture", evidence))
            return json.dumps({"body": "# 架构总览\n\nhandler 调用 storage。"})

        with (
            patch("weora_cli.knowledge_classifier.shutil.which", return_value="codex-fixture"),
            patch.object(knowledge_classifier, "_answer", side_effect=model),
        ):
            with self.assertRaises(CollabError) as failure:
                self.enrich("--via", "codex")
            self.assertEqual(failure.exception.code, "KNOWLEDGE_CODE_ENRICH_INCOMPLETE")
            report = as_object(failure.exception.data)
            self.assertEqual(report["missing_components"], ["storage"])
            self.assertEqual(report["failures"], {"storage.md": "MODEL_TEMPORARY_FAILURE"})
            self.assertIs(report["missing_architecture"], False)
            self.assertEqual(report["domain_status"], "generated")
            self.assertEqual([stage for stage, _ in calls[:3]], ["profile", "profile", "domain"])
            domain_file = self.docs.parent / "_domains.json"
            self.assertEqual(json.loads(domain_file.read_bytes()), _DOMAIN | {"components": ["handler", "storage"]})
            self.assertIn("订单计价", (self.base.parent / "index.md").read_text(encoding="utf-8"))
            manifest = json.loads((self.docs.parent / "_component-profiles.json").read_bytes())
            self.assertEqual(
                [(row["slug"], row["confidence"], row["responsibilities"]) for row in manifest["components"]],
                [
                    ("handler", "INFERRED", _PROFILE["responsibilities"]),
                    ("storage", "INFERRED", _PROFILE["responsibilities"]),
                ],
            )
            self.assertIn(
                {"from": "handler", "to": "storage", "relation": "DEPENDS_ON", "confidence": "EXTRACTED", "count": 1},
                manifest["edges"],
            )
            self.assertEqual([stage for stage, _ in calls].count("architecture"), 1)
            assert (self.docs / "handler.md").exists()
            assert not (self.docs / "storage.md").exists()
            assert not (self.docs / "graph-g5-scenarios.md").exists()
            self.assertIn("handler/order.py:4", (self.docs / "graph-g2-dataflow.md").read_text(encoding="utf-8"))
            self.assertIn("PaymentPort", (self.docs / "graph-g3-interfaces.md").read_text(encoding="utf-8"))
            self.assertIn("REFERENCES", (self.docs / "graph-g6-triples.md").read_text(encoding="utf-8"))
            retained = self.docs / "handler.md"
            retained.write_text("# 人工补充", encoding="utf-8")

            calls.clear()
            fail_storage = False
            result = self.enrich("--via", "codex")
            self.assertTrue(result["complete"])
            self.assertEqual([stage for stage, _ in calls], ["storage", "scenario"])
            self.assertEqual(retained.read_text(encoding="utf-8"), "# 人工补充")
            self.assertIn("sequenceDiagram", (self.docs / "graph-g5-scenarios.md").read_text(encoding="utf-8"))
            self.assertIn("源码概览", (self.docs / "README.md").read_text(encoding="utf-8"))
            self.assertEqual(set(calls[-1][1]), {
                "project", "revision", "components", "relations", "interfaces", "calls", "architecture",
                "designed_components", "repository_domain", "repository_description", "repository_keywords",
                "module_profiles",
            })

            self.assertEqual(calls[-1][1]["repository_domain"], _DOMAIN["domain"])
            calls.clear()
            self.enrich("--via", "codex")
            self.assertEqual(calls, [])
            self.assertEqual(self.refresh()["revision"], self.revision)
            self.assertIn("设计与图谱", (self.base / "index.md").read_text(encoding="utf-8"))
            self.target.write_bytes(b"def rate():\n    return 3\n")
            changed = self.refresh()
            self.assertNotEqual(changed["revision"], self.revision)
            next_plan = self.enrich("--dry-run")
            self.assertEqual(next_plan["missing_components"], ["handler", "storage"])
            self.assertEqual(next_plan["domain_status"], "pending")
            self.assertNotIn("订单计价", (self.base.parent / "index.md").read_text(encoding="utf-8"))
            self.assertTrue(domain_file.exists())
            self.assertNotIn(self.revision, (self.base / "index.md").read_text(encoding="utf-8"))
            self.assertTrue(retained.exists())

    def test_directory_import_runs_design_by_default_and_skips_with_flag(self) -> None:
        phases: list[str] = []

        def model(driver: str, prompt: str, schema: str) -> str:
            self.assertEqual(driver, "codex")
            evidence = as_object(json.loads(prompt.rsplit("\n", 1)[-1]))
            fields = json.loads(schema)["properties"]
            if "layer" in fields:
                phases.append("profile")
                return json.dumps(_PROFILE)
            if "domain" in fields:
                phases.append("domain")
                return json.dumps(_DOMAIN)
            stage = "scenarios" if "architecture" in evidence else str(evidence.get("component", "architecture"))
            phases.append(stage)
            return json.dumps({"body": _DIAGRAM if stage == "scenarios" else f"# {stage}"})

        args = ("import", "--dir", str(self.source), "--output", str(self.output), "--incremental")
        with (
            patch.dict(os.environ, {"WEORA_AI_TOOL": "", "TEAMAI_AI_TOOL": ""}),
            patch("weora_cli.knowledge_classifier.shutil.which", side_effect=lambda name: "codex" if name == "codex"
                  else None),
            patch.object(knowledge_classifier, "_answer", side_effect=model),
        ):
            self.assertNotIn("design", self.command(*args, "--skip-enrich"))
            self.assertEqual(phases, [])
            designed = self.command(*args)
            self.assertTrue(as_object(designed["design"])["complete"])
            self.assertEqual(phases[:3], ["profile", "profile", "domain"])
            self.assertCountEqual(phases[3:5], ["handler", "storage"])
            self.assertEqual(phases[5:], ["architecture", "scenarios"])
            phases.clear()
            self.assertTrue(as_object(self.command(*args)["design"])["complete"])
            self.assertEqual(phases, [])

    def test_removing_all_source_files_keeps_an_empty_index_without_agent_calls(self) -> None:
        for path in self.source.rglob("*.*"):
            if path.is_file():
                path.unlink()
        with (
            patch("weora_cli.knowledge_classifier.shutil.which", return_value="codex"),
            patch.object(knowledge_classifier, "_answer") as model,
        ):
            empty = self.command("import", "--dir", str(self.source), "--output", str(self.output), "--via", "codex")
        self.assertEqual(empty["fileCount"], 0)
        self.assertEqual(empty["design"], {"complete": True, "skipped": "no_components"})
        model.assert_not_called()

    def test_explicit_tool_preflight_and_single_retry_preserve_index(self) -> None:
        fresh = self.root / "fresh"
        args = ("import", "--dir", str(self.source), "--output", str(fresh))
        with patch.dict(os.environ, {"WEORA_AI_TOOL": "unavailable-tool"}):
            self.assertTrue(self.command(*args, "--dry-run")["preview"])
            with self.assertRaises(CollabError) as failure:
                self.command(*args)
            self.assertEqual(failure.exception.code, "KNOWLEDGE_CODE_ENRICH_TOOL")
            self.assertFalse(fresh.exists())
        with (
            patch("weora_cli.knowledge_classifier.shutil.which", return_value="codex"),
            patch.object(
                knowledge_classifier, "_answer", side_effect=CollabError("PROVIDER_FIXTURE", "测试生成失败"),
            ),
            self.assertRaises(CollabError) as failure,
        ):
            self.command(*args, "--via", "codex")
        self.assertEqual(failure.exception.code, "KNOWLEDGE_CODE_IMPORT_DESIGN_INCOMPLETE")
        report = as_object(failure.exception.data)
        self.assertEqual(as_object(report["index"])["fileCount"], 3)
        self.assertEqual(as_object(report["retry"])["project"], self.project)
        self.assertEqual(self.command("codebase", "--output", str(fresh), "--lint")["issues"], [])

    def test_partial_profiles_preserve_extracted_components_and_only_retry_missing(self) -> None:
        asked: list[object] = []
        missing = True

        def answer(driver: str, prompt: str, schema: str) -> str:
            fields = json.loads(schema)["properties"]
            evidence = as_object(json.loads(prompt.rsplit("\n", 1)[-1]))
            if "layer" in fields:
                asked.append(evidence["component"])
                if missing and evidence["component"] == "storage":
                    return json.dumps(_PROFILE | {"layer": "made-up"})
                return json.dumps(_PROFILE)
            if "domain" in fields:
                self.assertIn("handler", str(evidence["module_profiles"]))
                self.assertNotIn("storage", str(evidence["module_profiles"]))
                return json.dumps(_DOMAIN)
            return json.dumps({"body": _DIAGRAM})

        with (
            patch("weora_cli.knowledge_classifier.shutil.which", return_value="codex"),
            patch.object(knowledge_classifier, "_answer", side_effect=answer) as model,
        ):
            partial = self.enrich("--via", "codex")
            self.assertTrue(partial["complete"])
            self.assertEqual(partial["missing_profiles"], ["storage"])
            self.assertEqual(partial["failures"], {"storage.profile.json": "KNOWLEDGE_CODE_COMPONENT_PROFILE_REPLY"})
            path = self.docs.parent / "_component-profiles.json"
            self.assertEqual([row["confidence"] for row in json.loads(path.read_bytes())["components"]],
                             ["INFERRED", "EXTRACTED"])
            asked.clear()
            model.reset_mock()
            missing = False
            self.assertEqual(self.enrich("--via", "codex")["missing_profiles"], [])
            self.assertEqual(asked, ["storage"])
            self.assertEqual(model.call_count, 1)
            self.assertEqual([row["confidence"] for row in json.loads(path.read_bytes())["components"]],
                             ["INFERRED", "INFERRED"])

    def test_domain_failure_keeps_design_and_retries_only_classification(self) -> None:
        with (
            patch("weora_cli.knowledge_classifier.shutil.which", return_value="codex-fixture"),
            patch.object(knowledge_classifier, "_answer", side_effect=[
                json.dumps(_PROFILE), json.dumps(_PROFILE),
                json.dumps(_DOMAIN | {"keywords": "invalid"}),
                json.dumps({"body": "# 组件一"}), json.dumps({"body": "# 组件二"}),
                json.dumps({"body": "# 架构"}), json.dumps({"body": _DIAGRAM}),
                json.dumps(_DOMAIN),
            ]) as run,
        ):
            result = self.enrich("--via", "codex")
            self.assertTrue(result["complete"])
            self.assertEqual(result["domain_status"], "pending")
            self.assertEqual(result["failures"], {"_domains.json": "KNOWLEDGE_CODE_DOMAIN_REPLY"})
            self.assertFalse((self.docs.parent / "_domains.json").exists())
            index = self.base.parent / "index.md"
            self.assertIn("| 其他 |", index.read_text(encoding="utf-8"))
            self.assertIn("提供费率和费用计算", index.read_text(encoding="utf-8"))
            self.assertEqual(self.enrich("--via", "codex")["domain_status"], "generated")
            self.assertIn("费率, 订单, handler, storage", index.read_text(encoding="utf-8"))
            self.assertEqual(run.call_count, 8)
            self.assertEqual((self.docs / "architecture.md").read_text(encoding="utf-8").strip(), "# 架构")

    def test_module_limit_and_scenario_answer_contract(self) -> None:
        body = "# Design"

        def answer(driver: str, prompt: str, schema: str) -> str:
            fields = json.loads(schema)["properties"]
            if "layer" in fields:
                return json.dumps(_PROFILE)
            return json.dumps(_DOMAIN if "domain" in fields else {"body": body})

        with (
            patch("weora_cli.knowledge_classifier.shutil.which", return_value="codex-fixture"),
            patch.object(knowledge_classifier, "_answer", side_effect=answer) as run,
        ):
            limited = self.enrich("--max-modules", "1", "--via", "codex")
            self.assertTrue(limited["complete"])
            self.assertEqual(limited["scenarios"], "skipped")
            self.assertEqual(run.call_count, 5)
            with self.assertRaises(CollabError) as failure:
                self.enrich("--via", "codex")
            self.assertEqual(
                as_object(failure.exception.data)["failures"],
                {"graph-g5-scenarios.md": "KNOWLEDGE_CODE_ENRICH_SCENARIOS"},
            )
            self.assertEqual(run.call_count, 7)
            body = "# 场景\n\n证据不足，接口依赖仍需运行轨迹确认。"
            recovered = self.enrich("--via", "codex")
            self.assertTrue(recovered["complete"])
            self.assertEqual(run.call_count, 8)


class PlatformCodeEnrichmentTests(unittest.TestCase):
    def test_platform_automatic_generation_rejects_a_different_import_basis(self) -> None:
        project, local = str(uuid4()), str(uuid4())
        client = Client("http://127.0.0.1:8765", str(uuid4()), "test-device")
        index: dict[str, JsonValue] = {"codebase_id": local, "revision": "a" * 64, "generation": 2}
        with (
            TemporaryDirectory() as root,
            patch("weora_cli.knowledge.paired_client", return_value=client),
            patch("weora_cli.knowledge_classifier.shutil.which", return_value="codex"),
            patch("weora_cli.knowledge._call", return_value=index) as indexed,
            patch("weora_cli.knowledge_code_enrichment.call", return_value=index | {"generation": 3}) as design,
            patch.object(knowledge_classifier, "_answer") as model,
            self.assertRaises(CollabError) as failure,
        ):
            (Path(root) / "main.py").write_text("def total(): pass", encoding="utf-8")
            args = ["context", "import", "--dir", root, "--project", project, "--via", "codex"]
            execute(build_parser().parse_args(args))
        indexed.assert_called_once()
        design.assert_called_once_with(client, project, f"/codebases/local/{local}/design", max_modules=0)
        model.assert_not_called()
        self.assertEqual(failure.exception.code, "KNOWLEDGE_CODE_IMPORT_DESIGN_INCOMPLETE")
        data = as_object(failure.exception.data)
        self.assertEqual(data["index"], index)
        self.assertEqual(as_object(data["design"])["code"], "KNOWLEDGE_CODE_DESIGN_STALE")

    def test_platform_uses_fixed_basis_and_existing_team_document(self) -> None:
        project, repository = str(uuid4()), str(uuid4())
        client = Client("http://127.0.0.1:8765", str(uuid4()), "test-device")
        basis = "b" * 64
        saved: dict[str, dict[str, JsonValue]] = {
            "handler.md": {"id": str(uuid4()), "body": "# 正式处理器设计"},
            "architecture.md": {"id": str(uuid4()), "body": "# 团队技术架构"},
        }
        api_plan: dict[str, JsonValue] = {
            "project": "project source", "revision": "a" * 40, "basis": basis,
            "components": [
                {"slug": "handler", "title": "handler", "context": {}, "file_count": 1},
                {"slug": "storage", "title": "storage", "context": {"component": "storage"}, "file_count": 1},
            ],
            "profile_components": [
                {"slug": "storage", "title": "storage", "context": {"component": "storage"}, "file_count": 1},
            ],
            "interactions": [["handler", "storage"]], "architecture_context": {"project": "project source"},
            "domain_context": {"project": "project source", "declarations": "storage/rate.py:1 function rate"},
            "documents": dict(saved),
        }
        published: list[str] = []
        prompts: list[dict[str, JsonValue]] = []

        def api(
            request_client: Client, request_project: str, path: str, *,
            method: str = "GET", payload: dict[str, JsonValue] | None = None, **params: int,
        ) -> dict[str, JsonValue]:
            self.assertIs(request_client, client)
            self.assertEqual(request_project, project)
            self.assertEqual(path, f"/codebases/{repository}/design")
            if method == "GET":
                self.assertEqual(params, {"max_modules": 0})
                return api_plan
            assert payload is not None
            self.assertEqual(payload["basis"], basis)
            filename = str(payload.get("name", "graph-g6-triples.md"))
            published.append(filename)
            entry: dict[str, JsonValue] = {"id": str(uuid4()), "name": filename}
            if "body" in payload:
                entry["body"] = (json.dumps(_DOMAIN | {"domain": "已经发布的团队领域"})
                                 if filename == "_domains.json" else payload["body"])
                as_object(api_plan["documents"])[filename] = entry
            return {"basis": basis, "items": [entry]}

        def model(driver: str, prompt: str, schema: str) -> str:
            prompts.append(as_object(json.loads(prompt.rsplit("\n", 1)[-1])))
            fields = json.loads(schema)["properties"]
            if "layer" in fields:
                return json.dumps(_PROFILE)
            if "domain" in fields:
                return json.dumps(_DOMAIN)
            return json.dumps({"body": "# 存储" if "component" in prompts[-1] else _DIAGRAM})

        base = [
            "context", "codebase", "--deep-enrich", "--project", project, "--repository", repository, "--via", "codex",
        ]
        with (
            patch("weora_cli.knowledge.paired_client", return_value=client),
            patch("weora_cli.knowledge_classifier.shutil.which", return_value="codex-fixture"),
            patch("weora_cli.knowledge_code_enrichment.call", side_effect=api),
            patch.object(knowledge_classifier, "_answer", side_effect=model),
        ):
            preview = as_object(execute(build_parser().parse_args([*base, "--dry-run"])))
            self.assertEqual(preview["missing_components"], ["storage"])
            self.assertEqual(published, [])
            result = as_object(execute(build_parser().parse_args(base)))
            self.assertTrue(result["complete"])
            self.assertEqual(published, [
                "storage.profile.json", "_domains.json", "storage.md", "graph-g6-triples.md", "graph-g5-scenarios.md",
            ])
            self.assertEqual([row.get("component") for row in prompts], ["storage", None, "storage", None])
            self.assertEqual(prompts[2]["responsibilities"], _PROFILE["responsibilities"])
            self.assertEqual(prompts[3]["architecture"], saved["architecture.md"]["body"])
            self.assertEqual(prompts[3]["repository_domain"], "已经发布的团队领域")
            self.assertEqual(as_object(result["entries"])["handler.md"], saved["handler.md"]["id"])
