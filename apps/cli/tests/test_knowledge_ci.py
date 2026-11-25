"""测试 CI 预演、来源版本变动和已发布经验的索引重试。"""

import unittest
from argparse import ArgumentParser, Namespace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli.collab_client import Client, CollabError
from weora_cli.commands import JsonValue
from weora_cli.knowledge_ci import Markers, _comment_plan, arguments, execute
from weora_cli.knowledge_mr import CiReviewState, ci_diff_base, parse_target


class KnowledgeCiTest(unittest.TestCase):
    def test_comments_render_fixed_graph_delta_and_fail_open_to_paths(self) -> None:
        client = Client("https://weora.example.com", str(uuid4()))
        target = parse_target("https://github.com/team/orders/pull/42")
        state = CiReviewState("a" * 40, None, None)
        markers = Markers("summary", "learning", "graph")
        learning: dict[str, JsonValue] = {"title": "工程经验", "body": "处理过期节点。"}
        graph: dict[str, JsonValue] = {
            "base_commit": "b" * 40, "head_commit": "a" * 40,
            "nodes": {"added": [{"name": "fresh", "kind": "function", "line": 3, "path": "api.py"}],
                      "removed": [{"name": "stale", "kind": "function", "line": 8, "path": "api.py"}],
                      "updated": []},
            "counts": {"added": 1, "updated": 0, "removed": 1}, "skipped": [],
            "relations": {
                "added": [{"kind": "IMPLEMENTS", "source": "adapter.ts", "target": "port.ts"}],
                "removed": [{"kind": "REFERENCES", "source": "consumer.py", "target": "retired.py"}],
            },
            "relation_counts": {"added": 1, "removed": 1},
            "relation_coverage": {"limit": 200, "base_candidates": 3, "head_candidates": 4},
        }
        with (
            patch("weora_cli.knowledge_ci.ci_changed_paths", return_value=(
                "api.py", "tsconfig.json", "internal/go.mod", "bridge.mts", ".github/hidden.py", "README.md",
            )),
            patch("weora_cli.knowledge_ci.ci_diff_base", return_value="b" * 40),
            patch("weora_cli.knowledge_ci.call", return_value=graph) as request,
        ):
            comments, warning = _comment_plan(client, "project", target, state, learning, markers, individual=True)
            self.assertIsNone(warning)
            for content in ("新增（1）", "移除（1）", "fresh", "stale", "bbbbbbbbbbbb"):
                self.assertIn(content, comments["graph"])
            self.assertIn("接口实现 `adapter.ts` → `port.ts`", comments["graph"])
            self.assertIn("调用引用 `consumer.py` → `retired.py`", comments["graph"])
            self.assertIn("每个提交最多 200 个候选文件", comments["graph"])
            self.assertEqual(
                request.call_args.kwargs["payload"]["paths"],
                ["api.py", "tsconfig.json", "internal/go.mod", "bridge.mts"],
            )
            request.return_value = graph | {"relation_warning": "RELATIONS_TIMEOUT"}
            partial, warning = _comment_plan(client, "project", target, state, learning, markers, individual=True)
            self.assertIsNone(warning)
            self.assertIn("fresh", partial["graph"])
            self.assertIn("关系预览暂不可用（`RELATIONS_TIMEOUT`）", partial["graph"])
            request.side_effect = CollabError("REPOSITORY_SYNC_PENDING", "读取暂不可用")
            fallback, warning = _comment_plan(client, "project", target, state, learning, markers, individual=True)
            self.assertEqual(warning, "REPOSITORY_SYNC_PENDING")
            self.assertIn("api.py", fallback["graph"])
            self.assertEqual(fallback["learning"], comments["learning"])

    def test_github_preview_uses_merge_base_instead_of_branch_tip(self) -> None:
        target = parse_target("https://github.com/team/orders/pull/42")
        head, tip, base = "a" * 40, "b" * 40, "c" * 40
        with (
            patch("weora_cli.knowledge_mr._authorization", return_value={}),
            patch("weora_cli.knowledge_mr._review_detail", return_value=(
                "/repos/team/orders", "pr", "changes", {"head": {"sha": head}, "base": {"sha": tip}},
            )),
            patch("weora_cli.knowledge_mr._request", return_value=({"merge_base_commit": {"sha": base}}, {})) as req,
        ):
            self.assertEqual(ci_diff_base(target, head), base)
            self.assertEqual(req.call_args.args[2], f"/repos/team/orders/compare/{tip}...{head}")
            req.reset_mock()
            with self.assertRaises(CollabError) as changed:
                ci_diff_base(target, "d" * 40)
            self.assertEqual(changed.exception.code, "KNOWLEDGE_CI_HEAD_CHANGED")
            req.assert_not_called()

    def test_dry_run_and_merge_retry_use_one_persisted_draft(self) -> None:
        client = Client("https://weora.example.com", str(uuid4()))
        args = Namespace(url="https://github.com/team/orders/pull/42", project=uuid4(), via="codex",
                         language="zh", no_discussions=False, dry_run=True, output=None, mode="write", event_head=None,
                         comment_marker="summary", comment_author=None, individual_comments=False)
        head, merge = "a" * 40, "b" * 40
        state = CiReviewState(head, merge, datetime(2026, 11, 19, 3, tzinfo=UTC))
        draft: dict[str, JsonValue] = {"title": "索引增量同步", "body": "增量处理删除的代码证据。",
                                       "tags": ["索引"], "source": {"review_author": "developer"}}
        saved: dict[str, JsonValue] = {"status": "missing", "repository_id": str(uuid4())}
        operations: list[tuple[str, str]] = []
        updates = 0

        def api(
            _client: Client, _project: str, path: str, *, method: str = "GET",
            payload: dict[str, JsonValue] | None = None, **_params: str,
        ) -> dict[str, JsonValue]:
            nonlocal saved, updates
            operations.append((method, path))
            if method == "GET":
                return saved
            assert payload is not None
            if path.endswith("/merge"):
                updates += 1
                saved = saved | {"status": "merged", "merge_commit": merge, "entry_id": str(uuid4())}
            else:
                saved = saved | {"id": str(uuid4()), "status": "proposed", "learning": draft}
            return saved

        with (
            patch("weora_cli.knowledge_ci.paired_client", return_value=client),
            patch("weora_cli.knowledge_ci.read_ci_state", return_value=state),
            patch("weora_cli.knowledge_ci.read_review", return_value={"head_commit": head}),
            patch("weora_cli.knowledge_ci.draft_review", return_value=draft) as classify,
            patch("weora_cli.knowledge_ci.call", side_effect=api),
            patch("weora_cli.knowledge_ci.ci_changed_paths", return_value=("tsconfig.json",)) as paths,
            patch("weora_cli.knowledge_ci.refresh_review_indexes",
                  side_effect=[[{"status": "failed", "code": "SYNC_PENDING"}], [{"status": "updated"}]]) as refresh,
            TemporaryDirectory() as output,
        ):
            self.assertEqual(execute(args)["status"], "preview")
            self.assertEqual(operations, [("GET", "/ci/extractions")])
            args.dry_run = False
            self.assertEqual(execute(args)["codebase"], [{"status": "failed", "code": "SYNC_PENDING"}])
            args.output = Path(output)
            confirmed = execute(args)
            self.assertEqual(confirmed["codebase"], [{"status": "updated"}])
            self.assertIn(str(draft["body"]), (args.output / "learning.md").read_text(encoding="utf-8"))
            self.assertEqual(updates, 1)
            self.assertEqual(classify.call_count, 2)
            self.assertEqual(refresh.call_args_list[0].args[2], refresh.call_args_list[1].args[2])
            self.assertEqual(paths.call_args.kwargs["expected_head"], head)

    def test_late_automatic_comment_never_stages_or_posts_for_a_closed_or_replaced_review(self) -> None:
        parser = ArgumentParser()
        arguments(parser)
        url = "https://github.com/team/orders/pull/42"
        head = "a" * 40
        args = parser.parse_args(["extract-mr", "--url", url, "--project", str(uuid4()), "--event-head", head])
        with (
            patch("weora_cli.knowledge_ci.paired_client") as client,
            patch("weora_cli.knowledge_ci.draft_review") as classify,
            patch("weora_cli.knowledge_mr._authorization", return_value={}),
            patch("weora_cli.knowledge_mr._review_detail") as detail,
        ):
            for row, expected in (
                ({"head": {"sha": "b" * 40}, "state": "open"}, "KNOWLEDGE_CI_HEAD_CHANGED"),
                ({"head": {"sha": head}, "state": "closed"}, "KNOWLEDGE_CI_REVIEW_INACTIVE"),
                ({"head": {"sha": head}, "state": "open", "draft": True}, "KNOWLEDGE_CI_REVIEW_INACTIVE"),
            ):
                with self.subTest(state=row):
                    detail.return_value = ("repo", "pr", "changes", row)
                    self.assertEqual(execute(args), {"status": "skipped", "code": expected, "source": url})
            detail.return_value = ("repo", "pr", "changes", {"head": {"sha": head}})
            with self.assertRaises(CollabError) as error:
                execute(args)
            self.assertEqual(error.exception.code, "KNOWLEDGE_CI_REVIEW_STATE_UNKNOWN")
            client.assert_not_called()
            classify.assert_not_called()

    def test_review_closed_during_extraction_is_not_staged(self) -> None:
        parser = ArgumentParser()
        arguments(parser)
        url = "https://github.com/team/orders/pull/42"
        head = "a" * 40
        args = parser.parse_args(["extract-mr", "--url", url, "--project", str(uuid4()), "--event-head", head])
        client = Client("https://weora.example.com", str(uuid4()))
        with (
            patch("weora_cli.knowledge_ci.paired_client", return_value=client),
            patch("weora_cli.knowledge_ci.read_ci_state", side_effect=[
                CiReviewState(head, None, None, True), CiReviewState(head, None, None, False),
            ]),
            patch("weora_cli.knowledge_ci.read_review", return_value={"head_commit": head}),
            patch("weora_cli.knowledge_ci.draft_review") as classify,
            patch("weora_cli.knowledge_ci.call", return_value={"status": "missing"}) as api,
            patch("weora_cli.knowledge_ci.ReviewComments") as reviewer,
        ):
            self.assertEqual(execute(args)["code"], "KNOWLEDGE_CI_REVIEW_INACTIVE")
            classify.assert_called_once()
            api.assert_called_once_with(client, str(args.project), "/ci/extractions", url=url, head_commit=head)
            reviewer.assert_not_called()

    def test_unmerged_or_changed_source_does_not_start_classification(self) -> None:
        args = Namespace(url="https://github.com/team/orders/pull/42", project=uuid4(), via="codex",
                         language="zh", no_discussions=False, dry_run=False, output=None, mode="write", event_head=None,
                         comment_marker="summary", comment_author=None, individual_comments=False)
        client = Client("https://weora.example.com", str(uuid4()))
        with (
            patch("weora_cli.knowledge_ci.paired_client", return_value=client),
            patch("weora_cli.knowledge_ci.read_ci_state") as state,
            patch("weora_cli.knowledge_ci.read_review", return_value={"head_commit": "c" * 40}),
            patch("weora_cli.knowledge_ci.draft_review") as classify,
            patch("weora_cli.knowledge_ci.call", return_value={"status": "missing"}) as api,
        ):
            state.return_value = CiReviewState("a" * 40, None, None)
            with self.assertRaises(CollabError) as unmerged:
                execute(args)
            self.assertEqual(unmerged.exception.code, "KNOWLEDGE_CI_NOT_MERGED")
            api.assert_not_called()
            state.return_value = CiReviewState("a" * 40, "b" * 40, datetime.now(UTC))
            with self.assertRaises(CollabError) as changed:
                execute(args)
            self.assertEqual(changed.exception.code, "KNOWLEDGE_CI_HEAD_CHANGED")
            classify.assert_not_called()
