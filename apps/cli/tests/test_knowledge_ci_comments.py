"""覆盖 GitHub PR 评论请求路径、作者归属和独立否决。"""

import unittest
from unittest.mock import Mock, patch

from weora_cli.collab_client import CollabError
from weora_cli.knowledge_ci_comments import ReviewComments, comment_marker, comment_text
from weora_cli.knowledge_mr import ReviewTarget


class CiCommentsTest(unittest.TestCase):
    def test_publish_updates_only_matching_author_and_reuses_exact_body(self) -> None:
        target = ReviewTarget("github", "https://api.github.com", "owner/repo", "2", "unused")
        review = ("/repos/owner/repo", "/repos/owner/repo/pulls/2", "", {"head": {"sha": "a" * 40}})
        marker = comment_marker("workspace-project", "custom", "summary")
        desired = comment_text(marker, "评审建议", "/merge\n正文")
        self.assertIn("&#47;merge", desired)
        self.assertNotIn("\n/merge\n", desired)
        other = {"id": 10, "body": desired, "user": {"login": "spoofed"}}
        old = {"id": 20, "body": marker + "\n之前的建议", "user": {"login": "ci-bot"}}
        renew = Mock()
        with (
            patch("weora_cli.knowledge_ci_comments._authorization", return_value={}),
            patch("weora_cli.knowledge_ci_comments._review_detail", return_value=review),
            patch("weora_cli.knowledge_ci_comments._pages", side_effect=[[other], [other, old], [other, old | {
                "body": desired,
            }]]),
            patch("weora_cli.knowledge_ci_comments._request", return_value=({"login": "ci-bot"}, {})) as request,
        ):
            comments = ReviewComments(target, "a" * 40)
            self.assertEqual(comments.publish({marker: desired}, renew)[0], {"marker": marker, "status": "created"})
            self.assertEqual(comments.publish({marker: desired}, renew)[0], {"marker": marker, "status": "updated"})
            self.assertEqual(comments.publish({marker: desired}, renew)[0], {"marker": marker, "status": "unchanged"})
        writes = [(entry.args[2], entry.kwargs["method"]) for entry in request.call_args_list if entry.kwargs]
        self.assertEqual(writes, [
            ("/repos/owner/repo/issues/2/comments", "POST"), ("/repos/owner/repo/issues/comments/20", "PATCH"),
        ])
        self.assertEqual(renew.call_count, 2)

    def test_automatic_comment_rechecks_open_state_before_mutating_each_note(self) -> None:
        target = ReviewTarget("github", "https://api.github.com", "owner/repo", "2", "unused")
        head = "a" * 40
        opened = ("/repos/owner/repo", "pr", "", {"head": {"sha": head}, "state": "open"})
        closed = (*opened[:3], opened[3] | {"state": "closed"})
        renew = Mock()
        with (
            patch("weora_cli.knowledge_ci_comments._authorization", return_value={}),
            patch("weora_cli.knowledge_ci_comments._review_detail", side_effect=[opened, opened, closed]),
            patch("weora_cli.knowledge_ci_comments._pages", return_value=[]),
            patch("weora_cli.knowledge_ci_comments._request", return_value=({"login": "ci-bot"}, {})) as request,
        ):
            comments = ReviewComments(target, head, require_open=True)
            with self.assertRaises(CollabError) as stopped:
                comments.publish({"first": "建议", "second": "图谱"}, renew)
        self.assertEqual(stopped.exception.code, "KNOWLEDGE_CI_REVIEW_INACTIVE")
        self.assertEqual(renew.call_count, 1)
        writes = [item.kwargs["method"] for item in request.call_args_list if "method" in item.kwargs]
        self.assertEqual(writes, ["POST"])

    def test_github_veto_and_read_failure(self) -> None:
        reactions = [{"content": "-1"}]
        with (
            patch("weora_cli.knowledge_ci_comments._authorization", return_value={}),
            patch("weora_cli.knowledge_ci_comments._review_detail", return_value=(
                "/repos/owner/repo", "/repos/owner/repo/pulls/2", "", {"sha": "b" * 40},
            )),
            patch("weora_cli.knowledge_ci_comments._pages", return_value=reactions) as pages,
        ):
            target = ReviewTarget("github", "https://api.github.com", "owner/repo", "2", "unused")
            comment = ReviewComments(target, "b" * 40, "bot")
            self.assertFalse(comment.rejected(None))
            self.assertTrue(comment.rejected({"id": 24}))
            self.assertEqual(pages.call_args.args[2], "/repos/owner/repo/issues/comments/24/reactions")
            self.assertTrue(pages.call_args.kwargs["strict"])
            pages.side_effect = CollabError("FORGE_UNAVAILABLE", "反应列表读取失败")
            with self.assertRaises(CollabError):
                comment.rejected({"id": 24})
    def test_explicit_author_mismatch_prevents_any_comment_write(self) -> None:
        target = ReviewTarget("github", "https://api.github.com", "team/repo", "5", "unused")
        with (
            patch("weora_cli.knowledge_ci_comments._authorization", return_value={}),
            patch("weora_cli.knowledge_ci_comments._review_detail", return_value=(
                "", "/repos/team/repo/pulls/5", "", {"sha": "c" * 40},
            )),
            patch("weora_cli.knowledge_ci_comments._request", return_value=({"username": "other-account"}, {})) as user,
        ):
            client = ReviewComments(target, "c" * 40, "ci-bot")
            with self.assertRaises(CollabError) as rejected:
                client.publish({"marker": "内容"}, Mock())
            self.assertEqual(rejected.exception.code, "KNOWLEDGE_CI_COMMENT_AUTHOR")
            self.assertEqual(user.call_count, 1)
