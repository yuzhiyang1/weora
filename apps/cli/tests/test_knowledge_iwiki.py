"""验证 iWiki 的传输、检查点和知识审阅使用同一条链路。"""

import argparse
import json
import os
import unittest
from collections import Counter
from contextlib import nullcontext
from email.message import Message
from http.client import HTTPResponse, IncompleteRead
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request
from uuid import UUID, uuid4

from weora_cli import knowledge, knowledge_iwiki, knowledge_iwiki_sections, knowledge_rule_import
from weora_cli.collab_client import Client, CollabError, _NoRedirect, as_object
from weora_cli.commands import JsonValue
from weora_cli.knowledge_classifier import parse_classification
from weora_cli.knowledge_local_code import local_snapshot
from weora_cli.knowledge_offline_code import export_codebase, offline_codebase, offline_reconcile, record_iwiki_mappings


class IWikiImportTests(unittest.TestCase):
    def test_dual_import_preserves_regular_receipt_and_retries_only_summary(self) -> None:
        device = Client("https://weora.example.com", str(uuid4()), "fixture-device")
        publish = Mock(return_value={"items": [{"id": "ordinary"}]})
        summaries = Mock(return_value={"items": [{"id": "summary"}]})
        ai = Mock(side_effect=[
            "unavailable output",
            json.dumps({"business-api": "GET /ledger", "external-knowledge": "", "glossary": "LedgerAccount"}),
        ])
        with (
            TemporaryDirectory() as directory,
            patch.object(knowledge_rule_import, "config_path", return_value=Path(directory) / "collaboration.json"),
            patch.object(
                knowledge_iwiki, "_call", side_effect=["A" * 3300 + " keep context", {"title": "账单"}],
            ) as fetch,
            patch.object(knowledge_rule_import, "classify_rule", return_value={
                "kind": "doc", "title": "账单说明", "body": "账单", "tags": [],
            }) as classifier,
            patch.object(knowledge_iwiki_sections, "_answer", ai),
            patch.dict(os.environ, {"TAI_PAT_TOKEN": "fixture-token"}),
        ):
            first = knowledge_rule_import.run_import(
                device, publish=publish, publish_summary=summaries, accept_all=True, iwiki_source="https://iwiki.woa.com/p/42",
                iwiki_dual=True, iwiki_require_review=True,
            )
            self.assertEqual(as_object(first["iwiki_summary"])["status"], "failed")
            self.assertEqual(first["items"], [{"id": "ordinary"}])
            second = knowledge_rule_import.run_import(
                device, publish=publish, publish_summary=summaries, resume=str(first["run_id"]),
            )
            self.assertEqual(as_object(second["iwiki_summary"])["items"], [{"id": "summary"}])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(classifier.call_count, 1)
            self.assertEqual(len(classifier.call_args.args[2]), 3000)
            self.assertIn("keep context", ai.call_args.args[1])
            self.assertNotIn("fixture-token", ai.call_args.args[1])
            publish.assert_called_once()
            summaries.assert_called_once()
            payload = summaries.call_args.args[1]
            self.assertEqual(payload["source"], "iwiki://42")
            self.assertTrue(payload["require_review"])
            self.assertEqual([section["section"] for section in payload["sections"]], ["business-api", "glossary"])
            state = Path(directory) / "knowledge-import"
            self.assertNotIn("keep context", next(state.rglob("*.json")).read_text(encoding="utf-8"))

    def test_dual_preview_and_offline_review_use_only_confirmed_sections(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch.object(knowledge_rule_import, "config_path", return_value=Path(directory) / "collaboration.json"),
            patch.object(knowledge_iwiki, "_call", side_effect=lambda _, tool, __: (
                {"title": "术语"} if tool == "metadata" else "LedgerAccount"
            )),
            patch.object(knowledge_rule_import, "classify_rule", return_value=None),
            patch.object(knowledge_iwiki_sections, "_answer", return_value=json.dumps({
                "business-api": "GET /ledger", "external-knowledge": "外部手册", "glossary": "LedgerAccount",
            })),
            patch.dict(os.environ, {"TAI_PAT_TOKEN": "fixture"}),
        ):
            parser = argparse.ArgumentParser()
            knowledge.arguments(parser)
            source = ["import", "--from-iwiki", "https://iwiki.woa.com/p/42", "--iwiki-dual"]
            target = Path(directory) / "export"
            document = target / "docs/team-codebase/external-knowledge.md"
            document.parent.mkdir(parents=True)
            original = "# Wiki\r\n\r\n## 业务接口\r\n\r\nmanual notes\r\n".encode()
            document.write_bytes(original)
            preview = as_object(knowledge.execute(parser.parse_args([*source, "--dry-run"])))
            self.assertEqual(as_object(preview["iwiki_summary"])["status"], "preview")
            queued = as_object(knowledge.execute(parser.parse_args([
                *source, "--output", str(target), "--all", "--require-review",
            ])))
            self.assertEqual(document.read_bytes(), original)
            run = str(queued["run_id"])
            review = ["review", run]
            check = parser.parse_args([*review, "preview", "--iwiki-section", "business-api"])
            draft = as_object(knowledge.execute(check))
            self.assertEqual(draft["body"], "GET /ledger")
            accept = parser.parse_args([*review, "accept", "--iwiki-section", "business-api"])
            accepted = knowledge.execute(accept)
            content = document.read_bytes()
            self.assertIn(b"GET /ledger", content)
            self.assertIn(b"manual notes\r\n", content)
            self.assertNotIn(b"LedgerAccount", content)
            self.assertEqual(knowledge.execute(accept), accepted)
            self.assertEqual(document.read_bytes(), content)
            knowledge.execute(parser.parse_args([*review, "reject", "--iwiki-section", "glossary"]))
            knowledge_iwiki_sections.write_sections(target, {"external-knowledge": "updated elsewhere"}, "iwiki://99")
            with self.assertRaises(CollabError) as stale:
                knowledge_rule_import.review_local_iwiki_section(UUID(run), "external-knowledge", "accept", "")
            self.assertEqual(stale.exception.code, "KNOWLEDGE_IWIKI_OUTPUT_CONFLICT")
            self.assertIn(b"updated elsewhere", document.read_bytes())

    def test_offline_sections_can_be_approved_in_order_and_replayed_on_crlf(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sections = {"business-api": "GET /ledger\n分页", "glossary": "LedgerAccount\n账单"}
            bases = knowledge_iwiki_sections.baselines(root, sections)
            path = knowledge_iwiki_sections.write_sections(
                root, {"business-api": sections["business-api"]}, "iwiki://42", expected=bases,
            )
            assert path is not None
            document = Path(path)
            document.write_bytes(document.read_bytes().replace(b"\n", b"\r\n"))
            knowledge_iwiki_sections.write_sections(
                root, {"glossary": sections["glossary"]}, "iwiki://42", expected=bases,
            )
            content = document.read_bytes()
            stamp = document.stat().st_mtime_ns
            knowledge_iwiki_sections.write_sections(root, sections, "iwiki://42", expected=bases)
            self.assertEqual(document.read_bytes(), content)
            self.assertEqual(document.stat().st_mtime_ns, stamp)
            self.assertIn(b"GET /ledger\r\n", content)
            self.assertIn(b"LedgerAccount\r\n", content)

    def test_iwiki_import_links_reviewed_source_to_local_code_and_invalidates_version(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch.object(knowledge_rule_import, "config_path", return_value=Path(directory) / "collaboration.json"),
            patch.object(knowledge_iwiki, "_call", side_effect=[
                "a" * 3100 + " Use \x60LedgerClient\x60. Also CreateInvoice.", {"title": "账单"},
                "无可匹配的术语。", {"title": "新版"},
            ]),
            patch.object(knowledge_rule_import, "classify_rule", return_value={
                "kind": "doc", "title": "说明", "body": "这里只介绍业务。", "tags": [],
            }),
            patch.dict(os.environ, {"TAI_PAT_TOKEN": "fixture-token"}),
        ):
            output = Path(directory) / "wiki"
            source = Path(directory) / "billing"
            source.mkdir()
            file = source / "ledger.py"
            file.write_text("class LedgerClient: pass\nclass CreateInvoice: pass\n", encoding="utf-8")

            def index() -> None:
                export_codebase(output, local_snapshot(source, limit=200, preview=False, incremental=True))

            def relations() -> list[dict[str, str]]:
                graph = output / "teamwiki/.indices/graph-index.json"
                return [
                    item for item in json.loads(graph.read_bytes())["edges"] if item.get("derived") == "iwiki"
                ]

            index()
            result = knowledge_rule_import.run_import(
                None, publish=Mock(), output=output, accept_all=True, iwiki_source="https://iwiki.woa.com/p/42",
            )
            assert result["iwiki_code_mappings"] == 1
            link = relations()[0]
            self.assertEqual((link["source"], link["target"], link["evidence"]),
                             ("iwiki/p/42", "billing/ledger.py", "LedgerClient"))
            exported = output / link["source_file"]
            self.assertNotIn("LedgerClient", exported.read_text(encoding="utf-8"))
            offline_reconcile(output, preview=False)
            self.assertEqual(relations(), [link])
            self.assertEqual(as_object(offline_codebase(output, None, lint=True)["summary"])["high"], 0)
            file.write_text("class DifferentLedger: pass\n", encoding="utf-8")
            index()
            self.assertEqual(relations(), [])
            file.write_text("class LedgerClient: pass\nclass CreateInvoice: pass\n", encoding="utf-8")
            index()
            self.assertEqual(relations(), [])
            self.assertEqual(record_iwiki_mappings(output, [
                ("https://iwiki.woa.com/p/42", exported.name, ["LedgerClient"]),
            ]), 1)
            updated = knowledge_rule_import.run_import(
                None, publish=Mock(), output=output, accept_all=True, iwiki_source="https://iwiki.woa.com/p/42",
            )
            self.assertNotIn("iwiki_code_mappings", updated)
            self.assertEqual(relations(), [])

    def test_page_and_space_are_distinct_and_other_hosts_are_rejected(self) -> None:
        self.assertEqual(knowledge_iwiki.prepare_fetch("123")["trees"], ["123"])
        self.assertEqual(
            knowledge_iwiki.prepare_fetch("https://iwiki.woa.com/pages/456?from=test")["pages"],
            ["456"],
        )
        for invalid in ("", "https://other.example/p/456", "https://iwiki.woa.com@other.example/p/456"):
            with self.assertRaises(CollabError):
                knowledge_iwiki.prepare_fetch(invalid)

    def test_rpc_uses_canonical_transport_and_stops_after_matching_sse_event(self) -> None:
        class LiveResponse(BytesIO):
            """读到目标事件后仍有内容，模拟 SSE 持久连接。"""

            def __init__(self, data: bytes) -> None:
                super().__init__(data)
                self.headers = Message()
                self.headers["Content-Type"] = "text/event-stream"

        received: list[Request] = []

        def open_response(request: Request, *, timeout: int) -> LiveResponse:
            self.assertEqual(timeout, 30)
            received.append(request)
            data = request.data
            assert isinstance(data, bytes)
            envelope = json.loads(data)
            request_id = envelope["id"]
            self.assertEqual(envelope["params"], {"name": "getDocument", "arguments": {"docid": "42"}})
            result = {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"text": "# 来源正文"}]}}
            stream = LiveResponse(
                b'event: message\ndata: {"id":0,"result":{}}\n\n'
                + f"event: message\ndata: {json.dumps(result)}\n\n".encode()
                + b"data: not part of this response\n\n"
            )
            return stream

        opener = Mock()
        opener.open.side_effect = open_response
        with patch.object(knowledge_iwiki, "build_opener", return_value=opener) as build:
            self.assertEqual(knowledge_iwiki._call("fixture-token", "getDocument", {"docid": "42"}), "# 来源正文")
            self.assertEqual(received[0].full_url, knowledge_iwiki._ENDPOINT)
            self.assertEqual(received[0].get_header("Authorization"), "Bearer fixture-token")
            self.assertIsInstance(build.call_args.args[0], _NoRedirect)

    def test_rpc_permission_errors_do_not_echo_server_body(self) -> None:
        opener = Mock()
        opener.open.side_effect = HTTPError(
            knowledge_iwiki._ENDPOINT, 403, "test denied", Message(), BytesIO(b"private")
        )
        with (
            patch.object(knowledge_iwiki, "build_opener", return_value=opener),
            self.assertRaises(CollabError) as denied,
        ):
            knowledge_iwiki._call("fixture-token", "getDocument", {"docid": "42"})
        self.assertEqual(denied.exception.code, "KNOWLEDGE_IWIKI_ACCESS_DENIED")
        self.assertNotIn("private", str(denied.exception))

    def test_rpc_truncated_http_response_is_retryable_without_echoing_content(self) -> None:
        response = Mock(spec=HTTPResponse)
        response.headers = Message()
        response.headers["Content-Type"] = "application/json"
        response.length = 40
        response.read.return_value = b'{"id":1,"result":"private"}'
        opener = Mock()
        opener.open.return_value = nullcontext(response)
        for failure in (None, IncompleteRead(b"private", 40)):
            with (
                self.subTest(failure=failure),
                patch.object(knowledge_iwiki, "build_opener", return_value=opener),
                self.assertRaises(CollabError) as interrupted,
            ):
                response.read.side_effect = failure
                knowledge_iwiki._call("fixture-token", "getDocument", {"docid": "42"})
            self.assertEqual(interrupted.exception.code, "KNOWLEDGE_IWIKI_UNREACHABLE")
            self.assertNotIn("private", str(interrupted.exception))

    def test_document_parts_read_in_parallel_and_resume_with_saved_title(self) -> None:
        progress = knowledge_iwiki.prepare_fetch("https://iwiki.woa.com/p/42")
        candidates: list[JsonValue] = []
        barrier = Barrier(2, timeout=4)
        attempts: Counter[str] = Counter()
        checkpoints: list[dict[str, JsonValue]] = []

        def wiki(token: str, tool: str, arguments: dict[str, str]) -> JsonValue:
            self.assertEqual(token, "fixture-token")
            self.assertEqual(arguments, {"docid": "42"})
            attempts[tool] += 1
            if attempts[tool] == 1:
                # 只有两项同时提交才能通过屏障。正文第一次失败，标题成功入检查点。
                barrier.wait()
                if tool == "getDocument":
                    raise CollabError("KNOWLEDGE_IWIKI_UNREACHABLE", "暂时失败")
            return {"title": "第一版标题"} if tool == "metadata" else {"content": "# 第一版正文"}

        with patch.dict(os.environ, {"TAI_PAT_TOKEN": "fixture-token"}), patch.object(
            knowledge_iwiki, "_call", side_effect=wiki,
        ):
            failures = knowledge_iwiki.fetch_pages(
                progress, candidates, lambda: checkpoints.append(json.loads(json.dumps(progress))),
            )
            self.assertEqual(failures, [
                {"stage": "download", "page": "42", "code": "KNOWLEDGE_IWIKI_UNREACHABLE"},
            ])
            self.assertEqual(candidates, [])
            resumed = checkpoints[-1]
            self.assertEqual(resumed["downloads"], {"42": {"title_hint": "第一版标题"}})
            self.assertEqual(knowledge_iwiki.fetch_pages(
                resumed, candidates, lambda: checkpoints.append(json.loads(json.dumps(resumed))),
            ), [])
            # 两项均已入检查点时，即使在组装前中断，续做也无需凭据。
            fully_read = checkpoints[-2]
            replayed: list[JsonValue] = []
            with patch.dict(os.environ, {"TAI_PAT_TOKEN": ""}):
                self.assertEqual(knowledge_iwiki.fetch_pages(fully_read, replayed, lambda: None), [])
            self.assertEqual(replayed, candidates)
        self.assertEqual(attempts, {"getDocument": 2, "metadata": 1})
        self.assertEqual(resumed["downloads"], {})
        self.assertEqual(len(candidates), 1)
        candidate = as_object(candidates[0])
        self.assertEqual(candidate["title_hint"], "第一版标题")
        self.assertEqual(candidate["ref"], "https://iwiki.woa.com/p/42")
        self.assertEqual(candidate["excerpt"], "# 第一版正文")

    def test_import_resumes_failed_tree_and_download_without_reclassifying_published_page(self) -> None:
        calls: Counter[tuple[str, str]] = Counter()
        classified: Counter[str] = Counter()
        batches: list[list[JsonValue]] = []

        def wiki(token: str, tool: str, arguments: dict[str, str]) -> JsonValue:
            self.assertEqual(token, "fixture-token")
            page = next(iter(arguments.values()))
            calls[(tool, page)] += 1
            if tool == "getSpacePageTree":
                if page == "1":
                    return [
                        {"docid": 2, "title": "已下载", "has_children": False},
                        {"docid": 3, "title": "子树", "has_children": True},
                    ]
                if calls[(tool, page)] == 1:
                    raise CollabError("KNOWLEDGE_IWIKI_HTTP_FAILED", "暂时失败")
                return [{"docid": 2}, {"docid": 4}]
            if tool == "metadata":
                if page == "3" and calls[(tool, page)] == 1:
                    raise CollabError("KNOWLEDGE_IWIKI_HTTP_FAILED", "暂时失败")
                return {"title": f"Wiki 文档 {page}"}
            return f"# 文档 {page}"

        def classify(driver: str, ref: str, excerpt: str) -> dict[str, JsonValue] | None:
            classified[ref] += 1
            reply = {"type": "doc", "title": "", "summary": excerpt, "tags": [], "isPersonal": False}
            return parse_classification(json.dumps(reply), ref)

        def publish(project: str | None, items: list[JsonValue]) -> dict[str, JsonValue]:
            self.assertEqual(project, project_id)
            batches.append(items.copy())
            return {"items": items}

        project_id = str(uuid4())
        device = Client("https://weora.example.com", str(uuid4()), "fixture-device")
        with (
            TemporaryDirectory() as directory,
            patch.object(knowledge_rule_import, "config_path", return_value=Path(directory) / "collaboration.json"),
            patch.object(knowledge_iwiki, "_call", side_effect=wiki),
            patch.object(knowledge_rule_import, "classify_rule", side_effect=classify),
            patch.dict(os.environ, {"TAI_PAT_TOKEN": "fixture-token"}),
        ):
            with self.assertRaises(CollabError) as incomplete:
                knowledge_rule_import.run_import(
                    device,
                    project=project_id,
                    accept_all=True,
                    iwiki_source="1",
                    publish=publish,
                )
            self.assertEqual(incomplete.exception.code, "KNOWLEDGE_IWIKI_IMPORT_INCOMPLETE")
            state = as_object(incomplete.exception.data)
            failures = state["failures"]
            self.assertIsInstance(failures, list)
            assert isinstance(failures, list)
            self.assertEqual([as_object(item)["stage"] for item in failures], ["tree", "download"])
            run = str(state["run_id"])
            records = list(Path(directory).rglob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertNotIn("fixture-token", records[0].read_text(encoding="utf-8"))
            result = knowledge_rule_import.run_import(device, resume=run, publish=publish)
            self.assertEqual(result["accepted"], 3)
            self.assertEqual(knowledge_rule_import.run_import(device, resume=run, publish=publish), result)
        self.assertEqual(classified["https://iwiki.woa.com/p/2"], 1)
        self.assertEqual(calls[("getDocument", "2")], 1)
        self.assertEqual(calls[("getDocument", "3")], 1)
        self.assertEqual(calls[("metadata", "3")], 2)
        self.assertEqual(calls[("getSpacePageTree", "1")], 1)
        self.assertEqual(len(batches), 2)
        first, replayed = as_object(batches[0][0]), as_object(batches[1][0])
        self.assertEqual(first["operation_id"], replayed["operation_id"])
        self.assertEqual(first["source"], {"type": "wiki", "ref": "https://iwiki.woa.com/p/2"})
        self.assertEqual(first["title"], "Wiki 文档 2")
