"""隔离验证离线贡献、根目录召回配置及已分类清单。"""

import argparse
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge, knowledge_adoption, knowledge_api, knowledge_session_scoring
from weora_cli.collab_client import Client, CollabError, as_object, lock_file, write_json
from weora_cli.knowledge_import import select_imports
from weora_cli.knowledge_settings import SessionSettings


class KnowledgeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        local = patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(self.root / "collab.json")})
        local.start()
        self.addCleanup(local.stop)
        self.client = Client("https://example.com", str(uuid4()), "temporary-device")
        parser = argparse.ArgumentParser()
        knowledge.arguments(parser)
        self.parser = parser

    def test_contribution_preview_is_local_and_keeps_namespace_target(self) -> None:
        path = self.root / "preview.md"
        path.write_text("---\ntitle: 回归方法\n---\n选择稳定游标。", encoding="utf-8")
        project, operation = str(uuid4()), str(uuid4())
        with (
            patch.object(knowledge, "paired_client") as paired,
            patch.object(knowledge, "_call") as service,
        ):
            result = as_object(knowledge.execute(self.parser.parse_args([
                "contribute", "--file", str(path), "--dry-run", "--project", project,
                "--key", operation, "--namespace", "release",
            ])))
            self.assertEqual(result, {
                "preview": True, "operation_id": operation, "project_id": project, "entry_id": None,
                "kind": "learning", "title": "回归方法", "resource_path": f"learnings/release/{operation}",
                "bytes": len("选择稳定游标。".encode()), "target_status": "active", "base_revision": None,
            })
            arguments = ["contribute", "--file", str(path), "--dry-run", "--project", project, "--key", operation]
            default = as_object(knowledge.execute(self.parser.parse_args(arguments)))
            self.assertIsNone(default["resource_path"])
            self.assertTrue(default["resolve_namespace_on_publish"])
            shared = as_object(knowledge.execute(self.parser.parse_args([*arguments, "--namespace", ""])))
            self.assertIsNone(shared["project_id"])
            self.assertEqual(shared["resource_path"], f"learnings/{operation}")
            paired.assert_not_called()
            service.assert_not_called()
            self.assertFalse((self.root / "knowledge-outbox").exists())

    def test_markdown_dates_are_saved_in_the_offline_request_and_preview(self) -> None:
        path = self.root / "dated.md"
        for original, expected in (
            ("2026-05-09", "2026-05-09T00:00:00+00:00"),
            ('"2026-05-09"', "2026-05-09T00:00:00+00:00"),
            ("2026-05-09T07:39:14+08:00", "2026-05-08T23:39:14+00:00"),
            ('"2026-05-09T07:39:14+08:00"', "2026-05-08T23:39:14+00:00"),
            ('"unknown"', None),
        ):
            with self.subTest(date=original):
                path.write_text(f"---\ntitle: 发布回顾\ndate: {original}\n---\n\n先验证回退。", encoding="utf-8")
                args = self.parser.parse_args(["contribute", "--file", str(path)])
                operation, request = knowledge._contribution_request(args)
                payload = as_object(request["payload"])
                self.assertEqual(payload.get("authored_at"), expected)
                self.assertEqual(knowledge._preview_contribution(args).get("authored_at"), expected)
                self.assertEqual(payload["operation_id"], operation)
                self.assertNotIn("date:", str(payload["body"]))
        path.write_text("---\ndate: 2026-02-30\n---\n\n正文。", encoding="utf-8")
        with self.assertRaises(CollabError) as invalid:
            knowledge._contribution_request(self.parser.parse_args(["contribute", "--file", str(path)]))
        self.assertEqual(invalid.exception.code, "KNOWLEDGE_INVALID_FRONTMATTER")

    def test_markdown_domain_is_kept_as_a_declared_classification(self) -> None:
        path = self.root / "domain.md"
        args = self.parser.parse_args(["contribute", "--file", str(path)])
        for value in ("technical", "ops", "support", "neutral", "unknown"):
            with self.subTest(domain=value):
                path.write_text(f"---\ndomain: {value}\ntags: [api]\n---\n规则正文。", encoding="utf-8")
                expected = None if value == "unknown" else value
                _, request = knowledge._contribution_request(args)
                self.assertEqual(as_object(request["payload"]).get("domain"), expected)
                self.assertEqual(knowledge._preview_contribution(args).get("domain"), expected)

    def test_recall_uses_bound_project_and_supports_explicit_shared_and_admin_setting(self) -> None:
        project = str(uuid4())
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(Path, "cwd", return_value=self.root),
            patch("weora_cli.knowledge_settings.call", return_value={"recall": {"enabled": False}}),
            patch.object(knowledge, "_call", return_value={"items": []}) as service,
        ):
            SessionSettings(self.client, self.root).configure("recall", "inherit", project=project)
            knowledge.execute(self.parser.parse_args(["recall", "QueueLease"]))
            self.assertEqual(service.call_args.args, (self.client, project))
            self.assertEqual(service.call_args.kwargs["recall_mode"], 1)
            for args in (["recall", "QueueLease", "--shared"], ["search", "QueueLease"]):
                knowledge.execute(self.parser.parse_args(args))
                self.assertEqual(service.call_args.args, (self.client, None))
            entry = str(uuid4())
            knowledge.execute(self.parser.parse_args(["get", entry]))
            service.assert_called_with(self.client, project, f"/{entry}")
            knowledge.execute(self.parser.parse_args(["recall-scope", "enable", "--project", project]))
            service.assert_called_with(self.client, project, "/settings/recall-scope",
                                       method="PUT", payload={"inherit_shared": True})

    def test_offline_project_recall_and_automatic_open_skip_pending_shared_note(self) -> None:
        project = str(uuid4())
        local, shared = str(uuid4()), str(uuid4())
        title = "QueueLease"
        source = self.root / "note.md"
        source.write_text("已排队的正文。", encoding="utf-8")
        unreachable = CollabError("WEORA_UNREACHABLE", "离线")
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge_api, "request_json", side_effect=unreachable),
        ):
            for operation, params in ((shared, []), (local, ["--project", project])):
                args = ["contribute", "--file", str(source), "--title", title, "--key", operation, *params]
                self.assertTrue(as_object(knowledge.execute(self.parser.parse_args(args)))["queued"])
            scope = ["--project", project]
            found = as_object(knowledge.execute(self.parser.parse_args(["recall", title, *scope])))
            self.assertEqual([item["id"] for item in found["items"]], [local])
            direct = as_object(knowledge.execute(self.parser.parse_args(["get", shared, *scope])))
            self.assertEqual(direct["id"], shared)
            with self.assertRaises(CollabError) as automatic:
                knowledge.execute(self.parser.parse_args([
                    "get", shared, *scope, "--session", "scope-proof", "--automatic",
                ]))
            self.assertEqual(automatic.exception.code, "WEORA_UNREACHABLE")

    def test_locked_contribution_keeps_the_durable_request(self) -> None:
        path = self.root / "locked.md"
        path.write_text("Original note.", encoding="utf-8")
        operation = str(uuid4())
        project = str(uuid4())
        options = self.parser.parse_args(["contribute", "--file", str(path), "--key", operation, "--project", project])
        with patch.object(knowledge, "_send", side_effect=CollabError("WEORA_UNREACHABLE", "离线")) as submit:
            self.assertTrue(knowledge._submit(self.client, options)["queued"])
            pending = knowledge._queue(self.client) / f"{operation}.json"
            original = pending.read_bytes()
            with lock_file(pending):
                with self.assertRaises(CollabError) as busy:
                    knowledge._submit(self.client, options)
                self.assertEqual(busy.exception.code, "KNOWLEDGE_CONTRIBUTION_QUEUE_BUSY")
                with self.assertRaises(CollabError) as blocked_sync:
                    knowledge._sync(self.client, project)
                self.assertEqual(as_object(blocked_sync.exception.data)["failures"], [{
                    "operation_id": operation, "code": "KNOWLEDGE_CONTRIBUTION_QUEUE_BUSY",
                }])
            self.assertEqual(pending.read_bytes(), original)
            self.assertEqual(submit.call_count, 1)

            submit.side_effect = CollabError("KNOWLEDGE_FORBIDDEN", "资权限发生变化")
            with self.assertRaises(CollabError):
                knowledge._submit(self.client, options)
            self.assertEqual(pending.read_bytes(), original)
            submit.side_effect = None
            submit.return_value = {"status": "active"}
            self.assertEqual(knowledge._sync(self.client, project), {"published": [{"status": "active"}]})
            self.assertFalse(pending.exists())

    def test_offline_entry_is_visible_only_for_original_device_and_scope(self) -> None:
        path = self.root / "note.md"
        path.write_text("---\ntitle: 导出重试\ntags: [导出]\n---\n沿用原请求键。", encoding="utf-8")
        project = str(uuid4())
        operation = str(uuid4())
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge_api, "request_json", side_effect=CollabError("WEORA_UNREACHABLE", "离线")),
        ):
            staged = knowledge.execute(self.parser.parse_args([
                "contribute", "--project", project, "--file", str(path), "--key", operation,
                "--resource-path", "runbooks/export.md",
            ]))
            self.assertEqual(staged, {"queued": True, "operation_id": operation})
            found = as_object(knowledge.execute(self.parser.parse_args(["search", "导出", "--project", project])))
            self.assertTrue(found["offline"])
            self.assertIn("导出重试", str(found["items"]))
            scope = ["--project", project]
            recalled = as_object(knowledge.execute(self.parser.parse_args(["recall", "导出", *scope])))
            self.assertTrue(recalled["offline"])
            self.assertEqual(recalled["items"], found["items"])
            checked = knowledge.execute(self.parser.parse_args(["recall", "导出", "--check", *scope]))
            self.assertEqual(checked, {
                "relevant": True, "score": None, "threshold": None, "reason": "local-queue",
                "title": "导出重试", "entry_id": operation, "resource_path": "runbooks/export.md",
                "matched_terms": ["导出"], "missing_terms": [], "sources": [], "offline": True,
            })
            local = as_object(knowledge.execute(self.parser.parse_args(["get", "runbooks/export.md", *scope])))
            self.assertEqual(local["body"], "沿用原请求键。")
            with self.assertRaises(CollabError):
                knowledge.execute(self.parser.parse_args(["search", "导出", "--project", str(uuid4())]))
            different = Client(self.client.base, self.client.workspace, "second-device")
            with patch.object(knowledge, "paired_client", return_value=different), self.assertRaises(CollabError):
                knowledge.execute(self.parser.parse_args(["recall", "导出", *scope]))
        with patch.object(knowledge_api, "request_json", return_value={"status": "active"}) as send:
            self.assertEqual(knowledge._sync(self.client, None), {"published": [{"status": "active"}]})
            self.assertEqual(send.call_args.args[2]["operation_id"], operation)
            self.assertEqual(send.call_args.args[2]["source"]["resource_path"], "runbooks/export.md")
        self.assertEqual(knowledge._queued_learnings(self.client, project), [])

    def test_queued_recall_sends_identifiers_and_keeps_two_views_separate(self) -> None:
        path = self.root / "local.md"
        path.write_text("---\ntitle: Atlas local\n---\nLocal body stays here.", encoding="utf-8")
        project, operation, server_id = (str(uuid4()) for _ in range(3))
        params = ["--project", project]
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_call") as service,
        ):
            service.side_effect = CollabError("CONTRIBUTION_SERVICE_UNAVAILABLE", "暂时不可用", status=503)
            queued = knowledge.execute(self.parser.parse_args([
                "contribute", "--file", str(path), "--key", operation,
                "--resource-path", "learnings/Atlas.md", *params,
            ]))
            self.assertEqual(queued, {"queued": True, "operation_id": operation})
            service.side_effect = None
            service.return_value = {"items": [{"id": server_id}], "queued_visible": [operation]}
            received = as_object(knowledge.execute(self.parser.parse_args(["recall", "Atlas", *params])))
            rows = received["items"]
            self.assertIsInstance(rows, list)
            assert isinstance(rows, list)
            self.assertEqual([as_object(item)["id"] for item in rows], [operation, server_id])
            self.assertEqual(service.call_args.kwargs["payload"]["queued_learnings"], [{
                "operation_id": operation, "project_id": project, "path": "learnings/Atlas", "matches": True,
            }])
            self.assertNotIn("Local body stays here.", str(service.call_args.kwargs))
            service.return_value["queued_visible"] = []
            promoted = knowledge.execute(self.parser.parse_args(["recall", "Atlas", *params]))
            self.assertEqual(promoted, {"items": [{"id": server_id}]})
            service.side_effect = CollabError("KNOWLEDGE_FORBIDDEN", "无权限")
            with self.assertRaises(CollabError):
                knowledge.execute(self.parser.parse_args(["recall", "Atlas", *params]))

    def test_project_reconciliation_and_candidate_follow_platform_contract(self) -> None:
        project, source, code = (str(uuid4()) for _ in range(3))
        rule = self.root / "rule.md"
        rule.write_text(
            f"---\nrelated:\n  MAPS_TO: {code}\n  REFERENCES: {source}\n---\nLedgerAccount",
            encoding="utf-8",
        )
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_call", return_value={"changed": False}) as request,
        ):
            options = ["codebase", "--project", project, "--reconcile"]
            knowledge.execute(self.parser.parse_args([*options, "--dry-run"]))
            request.assert_called_once_with(self.client, project, "/codebases/reconcile", method="GET")
            request.reset_mock()
            knowledge.execute(self.parser.parse_args(options))
            request.assert_called_once_with(self.client, project, "/codebases/reconcile", method="POST")
            request.reset_mock()
            knowledge.execute(self.parser.parse_args([
                "contribute", "--project", project, "--entry", source, "--base-revision", "1",
                "--file", str(rule), "--kind", "rule", "--maps-to", code,
            ]))
            self.assertEqual(request.call_args.args[2], f"/{source}/revisions")
            self.assertEqual(request.call_args.kwargs["payload"]["relations"], [
                {"kind": "MAPS_TO", "target": code}, {"kind": "REFERENCES", "target": source},
            ])

    def test_team_path_declarations_survive_offline_queue_and_review_preview(self) -> None:
        project, source, pinned = (str(uuid4()) for _ in range(3))
        note = self.root / "related.md"
        note.write_text(
            f"---\nrelated:\n  references: [docs/runbook, {pinned}, 42]\n  DEPENDS_ON: rules/check\n---\n退款经验",
            encoding="utf-8",
        )
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_call", side_effect=CollabError("WEORA_UNREACHABLE", "离线")),
        ):
            queued = as_object(knowledge.execute(self.parser.parse_args([
                "contribute", "--file", str(note), "--project", project,
            ])))
            self.assertTrue(queued["queued"])

        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_call", return_value={"relation_findings": []}) as request,
        ):
            knowledge._sync(self.client, project)
            payload = request.call_args.kwargs["payload"]
            self.assertEqual(payload["relations"], [{"kind": "REFERENCES", "target": pinned}])
            self.assertEqual(payload["related"], {"references": ["docs/runbook", None], "DEPENDS_ON": None})
            knowledge.execute(self.parser.parse_args([
                "review", source, "preview", "--revision", "2", "--project", project,
            ]))
            request.assert_called_with(self.client, project, f"/{source}/review", revision=2)

    def test_get_team_path_reuses_the_session_attribution_endpoint(self) -> None:
        project = str(uuid4())
        path = "teamwiki/evidence/code/example/modules/api.md"
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_call", return_value={"kind": "code"}) as request,
        ):
            knowledge.execute(self.parser.parse_args(["get", path, "--project", project]))
            request.assert_called_once_with(self.client, project, "/by-path", path=path)
            request.reset_mock()
            knowledge.execute(self.parser.parse_args(["get", path, "--project", project, "--session", "root"]))
            request.assert_called_once_with(
                self.client, project, "/by-path/open", method="POST",
                payload={"session": "root", "recall_mode": False, "path": path},
            )

    def test_malformed_related_is_submitted_for_platform_findings(self) -> None:
        note = self.root / "invalid-related.md"
        note.write_text("---\nrelated:\n  REFERENCES: [docs/unclosed\n---\n经验正文\n", encoding="utf-8")
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_call", return_value={"status": "active"}) as request,
        ):
            knowledge.execute(self.parser.parse_args(["contribute", "--file", str(note)]))
            payload = request.call_args.kwargs["payload"]
            self.assertEqual(payload["body"], "经验正文\n")
            self.assertIs(payload["related"], True)

    def test_env_wins_and_project_instructions_use_root_session(self) -> None:
        project, session = str(uuid4()), str(uuid4())
        with (
            patch.object(Path, "cwd", return_value=self.root),
            patch.object(knowledge_api, "request_json", return_value={"items": []}),
            patch.dict(os.environ, {"WEORA_RECALL_DISABLED": "0"}),
        ):
            settings = SessionSettings(self.client, self.root)
            self.assertEqual(knowledge.session_recall_hint(settings, session), "")
            self.assertTrue(settings.configure("recall", "enable", project=project)["enabled"])
            hint = knowledge.session_recall_hint(SessionSettings(self.client, self.root / "src"), session)
            self.assertIn(f"--session {session} --project {project}", hint)
            self.assertNotIn("weora-recall", hint)
            parent = knowledge.session_recall_hint(settings, session, delegate_to="weora-recall")
            self.assertIn("先单独调用 weora-recall", parent)
            self.assertIn("角色不可用时直接查询", parent)
            self.assertNotIn("--recall-actor", parent)
            with patch.dict(os.environ, {"WEORA_RECALL_DISABLED": "1"}):
                self.assertEqual(knowledge.session_recall_hint(settings, session, delegate_to="weora-recall"), "")

    def test_plan_reminder_configure_updates_only_supplied_fields(self) -> None:
        message = self.root / "plan.md"
        message.write_text("团队召回提醒。\n", encoding="utf-8")
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch("weora_cli.knowledge_plan_reminder.call", return_value={"enabled": True}) as request,
        ):
            knowledge.execute(self.parser.parse_args(["plan-reminder", "configure", "--message-file", str(message)]))
            self.assertEqual(request.call_args.kwargs, {"method": "PATCH", "payload": {"message": "团队召回提醒。"}})
            knowledge.execute(self.parser.parse_args(["plan-reminder", "disable"]))
            self.assertEqual(request.call_args.kwargs, {"method": "PATCH", "payload": {"enabled": False}})
            knowledge.execute(self.parser.parse_args(["plan-reminder", "configure", "--default-message",
                                                       "--language", "default"]))
            self.assertEqual(
                request.call_args.kwargs, {"method": "PATCH", "payload": {"language": None, "message": None}},
            )
            knowledge.execute(self.parser.parse_args(["plan-reminder", "status"]))
            self.assertFalse(request.call_args.kwargs)
            with self.assertRaises(CollabError):
                knowledge.execute(self.parser.parse_args(["plan-reminder", "configure"]))

    def test_team_policy_changes_disable_automatic_attribution_but_keep_manual_reads(self) -> None:
        session, entry = str(uuid4()), str(uuid4())
        argv = ["search", "回归知识", "--session", session]
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(Path, "cwd", return_value=self.root),
            patch.dict(os.environ, {"WEORA_RECALL_DISABLED": "0"}),
            patch("weora_cli.knowledge_settings.call", return_value={
                "recall": {"enabled": True, "source": "workspace"},
                "share-reminder": {"enabled": True, "source": "default"},
            }) as policy,
            patch.object(knowledge, "_call", return_value={"items": []}) as request,
        ):
            knowledge.execute(self.parser.parse_args([*argv, "--automatic"]))
            self.assertEqual(request.call_args.args[-1], "/recall")
            policy.return_value["recall"]["enabled"] = False
            settings = SessionSettings(self.client, self.root)
            self.assertEqual(settings.effective("share-reminder"), {"enabled": False, "source": "recall"})
            knowledge.execute(self.parser.parse_args([*argv, "--automatic"]))
            self.assertNotIn("payload", request.call_args.kwargs)
            knowledge.execute(self.parser.parse_args(["get", entry, "--session", session, "--automatic"]))
            self.assertEqual(request.call_args.args[-1], f"/{entry}")
            knowledge.execute(self.parser.parse_args(argv))
            self.assertEqual(request.call_args.args[-1], "/recall")

    def test_recall_check_uses_published_scope_and_five_results_by_default(self) -> None:
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_queued_learnings", return_value=[]) as queued,
            patch.object(knowledge, "_call", return_value={"relevant": False, "score": 0, "threshold": 4}) as request,
        ):
            result = knowledge.execute(self.parser.parse_args(["recall", "QueueLease", "--check"]))
            self.assertEqual(result, {"relevant": False, "score": 0, "threshold": 4})
            request.assert_called_once_with(
                self.client, None, query="QueueLease", limit=5, check=1,
                recall_mode=1, depth="context", check_details=1,
            )
            queued.assert_called_once_with(self.client, None)

    def test_recall_depth_is_forwarded_for_read_only_and_recorded_calls(self) -> None:
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_call", return_value={"items": [], "code_route": []}) as request,
        ):
            route = knowledge.execute(self.parser.parse_args(["recall", "账单", "--depth", "route"]))
            self.assertEqual(route, {"items": [], "code_route": []})
            self.assertEqual(request.call_args.kwargs["depth"], "route")
            knowledge.execute(self.parser.parse_args(["recall", "账单", "--depth", "lookup", "--session", "s1"]))
            self.assertEqual(request.call_args.args[-1], "/recall")
            self.assertEqual(request.call_args.kwargs["payload"]["depth"], "lookup")

    def test_graph_reads_a_single_published_document_with_existing_open_attribution(self) -> None:
        project, codebase, entry, session, actor = (str(uuid4()) for _ in range(5))
        pages = {"topic": "g2", "items": [{"codebase_id": codebase, "entry_id": entry}], "truncated": False}
        document = {"id": entry, "title": "G2 调用链与数据流", "body": "A calls B"}
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_automatic_recall_allowed", return_value=True),
            patch.object(knowledge, "_call", side_effect=[pages, document]) as request,
        ):
            result = knowledge.execute(self.parser.parse_args([
                "graph", "g2", "--project", project, "--codebase", codebase,
                "--session", session, "--actor", actor, "--automatic",
            ]))
            self.assertEqual(result, pages | {"status": "ready", "document": document})
            self.assertEqual(request.call_count, 2)
            catalog, opened = request.call_args_list
            self.assertEqual(catalog.args, (self.client, project, "/codebases/graph-documents/g2"))
            self.assertEqual(catalog.kwargs, {"codebase_id": codebase})
            self.assertEqual(opened.args, (self.client, project, f"/{entry}/open"))
            self.assertEqual(opened.kwargs, {
                "method": "POST", "payload": {"recall_mode": True, "session": session, "actor": actor},
            })

    def test_graph_selection_and_missing_cases_do_not_open_or_search(self) -> None:
        project = str(uuid4())
        for status, pages in (
            ("missing", {"items": [], "truncated": False}),
            ("choose", {"items": [{"entry_id": str(uuid4())}, {"entry_id": str(uuid4())}], "truncated": False}),
        ):
            with (
                self.subTest(status=status),
                patch.object(knowledge, "paired_client", return_value=self.client),
                patch.object(knowledge, "_call", return_value=pages) as request,
            ):
                result = knowledge.execute(self.parser.parse_args(["graph", "g5", "--project", project]))
                self.assertEqual(result, pages | {"status": status})
                request.assert_called_once_with(self.client, project, "/codebases/graph-documents/g5")

    def test_maintenance_prune_preview_follows_empty_pages_and_sorts_candidates(self) -> None:
        first, second, project = (str(uuid4()) for _ in range(3))
        pages = [
            {"items": [], "next_after": first},
            {"items": [{"id": first, "confidence": 0.14}], "next_after": second},
            {"items": [{"id": second, "confidence": 0.04}], "next_after": None},
        ]
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch("weora_cli.knowledge_maintenance.call", side_effect=pages) as request,
        ):
            result = as_object(knowledge.execute(self.parser.parse_args([
                "maintenance", "--prune", "--archive", "--threshold", "0.15", "--dry-run", "--project", project,
            ])))
        self.assertTrue(result["preview"])
        self.assertEqual(result["items"], [{"id": second, "confidence": 0.04}, {"id": first, "confidence": 0.14}])
        self.assertEqual([entry.kwargs.get("after") for entry in request.call_args_list], [None, first, second])
        self.assertTrue(all(entry.kwargs["method"] == "GET" for entry in request.call_args_list))

    def test_confidence_writeback_cli_uses_post_only_when_requested(self) -> None:
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch("weora_cli.knowledge_maintenance.call", return_value={"items": []}) as request,
        ):
            action = ["maintenance", "--confidence-writeback"]
            knowledge.execute(self.parser.parse_args([*action, "--dry-run"]))
            request.assert_called_once_with(self.client, None, "/maintenance/confidence", method="GET")
            request.reset_mock()
            knowledge.execute(self.parser.parse_args(action))
            request.assert_called_once_with(self.client, None, "/maintenance/confidence", method="POST")

    def test_contribution_keeps_rule_frontmatter_wildcards(self) -> None:
        rule = self.root / "rule.md"
        rule.write_text("---\ntitle: 发布\npaths: **/*.py # Python rules\n---\n保留审计记录。", encoding="utf-8")
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_call", return_value={"status": "pending"}) as request,
        ):
            knowledge.execute(self.parser.parse_args(["contribute", "--kind", "rule", "--file", str(rule)]))
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["rule_paths"], "**/*.py")
        self.assertEqual(payload["body"], "保留审计记录。")

    def test_adoption_score_previews_valid_versions_and_writes_only_labels(self) -> None:
        project, session, identifier = str(uuid4()), str(uuid4()), str(uuid4())
        evidence = self.root / "evidence.md"
        evidence.write_text("只授权评分 Agent 看到的会话摘要。", encoding="utf-8")
        candidates = {"items": [{"id": identifier, "revision": 2, "title": "规则", "excerpt": "固定正文"}]}
        verdicts = [{"entry_id": identifier, "revision": 2, "label": "adopted"}]
        reply = json.dumps({"verdicts": verdicts})
        command = [
            "adoption", "score", "--project", project, "--session", session,
            "--evidence", str(evidence), "--driver", "claude",
        ]
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge_adoption, "call", return_value=candidates) as request,
            patch.object(knowledge_adoption, "_answer", return_value=reply) as judge,
        ):
            preview = knowledge.execute(self.parser.parse_args(command))
            self.assertEqual(as_object(preview)["verdicts"], verdicts)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(judge.call_args.args[0], "claude")
            self.assertIn("只授权评分 Agent 看到的会话摘要", judge.call_args.args[1])

            request.side_effect = [candidates, {"applied": [identifier], "skipped": []}]
            written = knowledge.execute(self.parser.parse_args([*command, "--apply"]))
            self.assertEqual(as_object(written)["applied"], [identifier])
            self.assertEqual(request.call_args.args[-1], "/adoption/verdicts")
            self.assertEqual(request.call_args.kwargs["payload"], {"session": session, "verdicts": verdicts})

            request.reset_mock(side_effect=True)
            judge.return_value = json.dumps({"verdicts": [verdicts[0] | {"revision": 3}]})
            with self.assertRaises(CollabError) as invalid:
                knowledge.execute(self.parser.parse_args([*command, "--apply"]))
            self.assertEqual(invalid.exception.code, "KNOWLEDGE_ADOPTION_VERDICT_INVALID")
            self.assertEqual(request.call_count, 1)

    def test_team_adoption_template_preserves_judge_label_and_uses_logical_evidence_path(self) -> None:
        identifier, template = str(uuid4()), str(uuid4())
        evidence = self.root / "transcript.md"
        evidence.write_text("Used the mirror.", encoding="utf-8")
        rubric = {
            "id": template, "revision": 4, "source_hash": "a" * 64, "name": "teamai:adoption:fixture",
            "prompt": "Reference recalled/example.md.", "evidence_path": "recalled/example.md",
            "tool": "claude", "model": "fixture-model", "passing_score": 3,
            "labels": [{"value": "partial", "score": 3}, {"value": "missing", "score": 0}],
        }
        candidate = {"id": identifier, "revision": 2, "excerpt": "Use the mirror.", "rubric": rubric}
        args = self.parser.parse_args([
            "adoption", "score", "--session", str(uuid4()), "--evidence", str(evidence),
        ])
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge_adoption, "call", return_value={"items": [candidate]}) as transport,
            patch.object(knowledge_session_scoring, "_answer", return_value='{"label": "partial"}') as judge,
        ):
            answer = as_object(knowledge.execute(args))
            self.assertEqual(answer["verdicts"], [{"entry_id": identifier, "revision": 2, "label": "adopted"}])
            self.assertEqual(as_object(answer["scorers"])[identifier], {
                "id": template, "revision": 4, "source_hash": "a" * 64, "label": "partial",
            })
            self.assertTrue(transport.call_args.kwargs["payload"]["include_rubric"])
            self.assertEqual(judge.call_args.args[0], "claude")
            self.assertEqual(judge.call_args.kwargs, {"model": "fixture-model"})
            model_input = json.loads(judge.call_args.args[1].split("\n", 1)[1])
            self.assertEqual(model_input["evidence_files"], [
                {"path": "recalled/example.md", "content": "Use the mirror."},
            ])
            self.assertEqual(model_input["transcript"][0]["text"], "Used the mirror.")
            self.assertNotIn(str(evidence), str(model_input))

            second = str(uuid4())
            other = rubric | {"name": "teamai:adoption:other", "evidence_path": "recalled/other.md"}
            transport.return_value = {"items": [candidate, {
                "id": second, "revision": 1, "excerpt": "Use the furnace.", "rubric": other,
            }]}
            judge.reset_mock()
            judge.return_value = json.dumps({
                rubric["name"]: {"label": "partial"}, other["name"]: {"label": "missing"},
            })
            group = as_object(knowledge.execute(args))
            self.assertEqual(group["verdicts"], [
                {"entry_id": identifier, "revision": 2, "label": "adopted"},
                {"entry_id": second, "revision": 1, "label": "ignored"},
            ])
            judge.assert_called_once()
            batch_input = json.loads(judge.call_args.args[1].split("\n", 1)[1])
            self.assertEqual(
                [question["evidence_files"][0]["path"] for question in batch_input["rubrics"]],
                ["recalled/example.md", "recalled/other.md"],
            )

    def test_adoption_uses_a_pinned_archive_in_preview_and_reviewed_label_file(self) -> None:
        project, session, artifact, entry = (str(uuid4()) for _ in range(4))
        options = ["--session", session, "--project", project]
        archive = {"archive_id": artifact, "revision": 2}
        receipt = {"id": artifact, "revision": 2, "digest": "f" * 64}
        turns = [{"role": "assistant", "text": "Evidence from archived revision two."}]
        plan = {"items": [{"id": entry, "revision": 1, "excerpt": "Stable knowledge."}], "archive": receipt}
        verdicts = [{"entry_id": entry, "revision": 1, "label": "ignored"}]
        command = ["adoption", "score", *options, "--archive", artifact, "--archive-revision", "2"]
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge_adoption, "call", side_effect=[plan, receipt | {"turns": turns}]) as transport,
            patch.object(knowledge_adoption, "_answer", return_value=json.dumps({"verdicts": verdicts})) as judge,
        ):
            answer = as_object(knowledge.execute(self.parser.parse_args(command)))
            self.assertEqual(answer["archive"], archive)
            self.assertEqual(answer["verdicts"], verdicts)
            self.assertEqual(transport.call_args_list[0].kwargs["payload"]["archive"], archive)
            self.assertEqual(transport.call_args.args[-1], f"/shared-sessions/{artifact}")
            self.assertEqual(transport.call_args.kwargs, {"revision": 2})
            self.assertIn("Evidence from archived revision two.", judge.call_args.args[1])

            file = self.root / "reviewed.json"
            file.write_text(json.dumps({"data": answer}), encoding="utf-8")
            transport.side_effect = [{"applied": [entry], "skipped": []}]
            knowledge.execute(self.parser.parse_args(["adoption", "write", *options, "--verdicts", str(file)]))
            self.assertEqual(transport.call_args.kwargs["payload"], {
                "session": session, "verdicts": verdicts, "archive": archive,
            })

    def test_mr_overlap_checks_recent_queue_locally_and_deduplicates_published_retry(self) -> None:
        project = str(uuid4())
        staged = knowledge._queue(self.client)
        body = "限流队列租约"
        same_operation = str(uuid4())
        other_project = str(uuid4())
        for key, title, scope, text in (
            (same_operation, "已确认的离线稿", project, body),
            (str(uuid4()), "共享离线稿", None, "限流队列"),
            (str(uuid4()), "其他项目", other_project, body),
            (str(uuid4()), "同题异文", project, "http client"),
            (str(uuid4()), "旧经验", project, body),
        ):
            file = staged / f"{key}.json"
            write_json(file, {
                "project": scope,
                "payload": {"title": title, "kind": "learning", "body": text, "source": {"type": "manual"}},
            })
            if title == "旧经验":
                old = (datetime.now(UTC) - timedelta(days=15)).timestamp()
                os.utime(file, (old, old))
        (staged / "damaged.json").write_text("{", encoding="utf-8")
        remote = {"items": [{"title": "平台已发布", "overlap": 1.0, "operation_id": same_operation}]}
        with patch.object(knowledge, "_call", return_value=remote) as call:
            self.assertEqual(
                knowledge._review_overlaps(self.client, project, body), (["平台已发布", "共享离线稿"], None),
            )
            call.assert_called_once_with(self.client, project, "/overlaps", method="POST", payload={"body": body})
        with patch.object(knowledge, "_call", side_effect=CollabError("WEORA_UNREACHABLE", "离线")):
            self.assertEqual(
                knowledge._review_overlaps(self.client, project, body),
                (["已确认的离线稿", "共享离线稿"], "WEORA_UNREACHABLE"),
            )
        with patch.object(knowledge, "_call", side_effect=CollabError("PROJECT_NOT_FOUND", "无项目权限")):
            self.assertEqual(knowledge._review_overlaps(self.client, project, body), ([], "PROJECT_NOT_FOUND"))

    def test_import_drops_personal_and_keeps_id_stable(self) -> None:
        path = self.root / "manifest.json"
        path.write_text(json.dumps([{"kind": "personal"}, {
            "kind": "rule", "title": "Deploy", "body": "Weekdays.",
            "source": {"type": "file", "ref": ".claude/rules/deploy.md", "resource_path": "runbooks/deploy.md"},
        }]), encoding="utf-8")
        items = select_imports(path, accept_all=True, project=None)
        self.assertEqual(len(items), 1)
        self.assertEqual(as_object(as_object(items[0])["source"])["resource_path"], "runbooks/deploy.md")
        self.assertEqual(items, select_imports(path, accept_all=True, project=None))
