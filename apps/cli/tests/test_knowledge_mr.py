"""验证评审来源隔离、可选上下文和逐项续做。"""

import json
import os
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from weora_cli import knowledge_classifier, knowledge_mr, knowledge_rule_import
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue
from weora_cli.knowledge_import import review_import

FIRST = "https://github.com/acme/catalog/pull/7"
SECOND = "https://github.com/acme/catalog/pull/8"


class ReviewImportTests(unittest.TestCase):
    def test_restricted_claude_inherits_only_the_configured_model_provider(self) -> None:
        with TemporaryDirectory() as folder:
            config = Path(folder) / "settings.json"
            config.write_text(json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "https://gateway.example.test",
                    "ANTHROPIC_AUTH_TOKEN": "fixture-gateway-token",
                    "ANTHROPIC_MODEL": "fixture-model",
                    "UNRELATED_ENV": "omitted",
                },
                "apiKeyHelper": "must-not-run",
                "hooks": {"SessionStart": ["must-not-run"]},
            }), encoding="utf-8")
            reply = json.dumps({"structured_output": {"body": "sample"}})
            process = Mock(returncode=0, stdout=reply)
            host_env = {"CLAUDE_CONFIG_DIR": folder, "WEORA_COLLAB_TOKEN": "fixture-weora"}
            with (
                patch.dict(os.environ, host_env, clear=True),
                patch.object(knowledge_classifier.shutil, "which", return_value="claude"),
                patch.object(knowledge_classifier.subprocess, "run", return_value=process) as run,
            ):
                self.assertEqual(json.loads(knowledge_classifier._answer("claude", "review", "{}")), {"body": "sample"})
                args = run.call_args.args[0]
                env = run.call_args.kwargs["env"]
                self.assertIn("--restricted", args)
                self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://gateway.example.test")
                self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "fixture-gateway-token")
                self.assertEqual(env["ANTHROPIC_MODEL"], "fixture-model")
                self.assertNotIn("UNRELATED_ENV", env)
                self.assertNotIn("WEORA_COLLAB_TOKEN", env)
                self.assertNotIn("--settings", args)

                with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://override.example.test"}):
                    knowledge_classifier._answer("claude", "review", "{}")
                env = run.call_args.kwargs["env"]
                self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://override.example.test")
                self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_restricted_claude_reports_auth_failure_without_client_output(self) -> None:
        with (
            patch.object(knowledge_classifier, "_inherit_claude_provider"),
            patch.object(knowledge_classifier.shutil, "which", return_value="claude"),
            patch.object(knowledge_classifier.subprocess, "run", return_value=Mock(
                returncode=1, stdout=json.dumps({"is_error": True, "result": "Not logged in · Private client detail"}),
            )),
            self.assertRaises(CollabError) as caught,
        ):
            knowledge_classifier._answer("claude", "review", "{}")
        self.assertEqual(caught.exception.code, "KNOWLEDGE_CLASSIFIER_AUTH_REQUIRED")
        self.assertNotIn("Private client detail", str(caught.exception))

    def test_source_url_is_bound_to_provider_origin(self) -> None:
        for url in (
            "https://gitlab.com/group/catalog/-/merge_requests/35",
            "https://git.woa.com/acme/catalog/merge_requests/9",
            "https://github.com@evil.test/acme/catalog/pull/7",
            "https://github.com/acme/%2E%2E/pull/7",
            "https://github.com/acme/catalog/pull/7/commits",
            "http://github.com/acme/catalog/pull/7",
        ):
            with self.subTest(url=url), self.assertRaises(CollabError):
                knowledge_mr.parse_target(url)
        self.assertEqual(knowledge_mr.parse_target(FIRST + "/?tab=files#note").url, FIRST)

    def test_github_pagination_and_author_replies_without_generated_comments(self) -> None:
        root = "/repos/acme/catalog"
        detail = root + "/pulls/7"
        marker = "x" * 600 + "<!-- teamai: "
        responses: list[tuple[JsonValue, dict[str, str]]] = [
            ({"number": 7, "title": "Retry budget", "user": {"login": "alice"}}, {}),
            (
                [{"sha": "first", "commit": {"message": "first page\nlonger message"}}],
                {
                    "link": '<https://untrusted.example.test/pagination>; rel="next"',
                },
            ),
            ([{"sha": "second", "commit": {"message": "second page"}}], {}),
            ("diff" * 800, {}),
            (
                [
                    {"id": 1, "body": "Add a retry cap?", "path": "api.py", "line": 23, "user": {"login": "bob"}},
                    {"id": 2, "in_reply_to_id": 1, "body": "Applied cap", "user": {"login": "alice"}},
                ],
                {},
            ),
            (
                [
                    {"state": "APPROVED", "body": "Matches production policy", "user": {"login": "bob"}},
                    {"state": "PENDING", "body": "private pending review"},
                ],
                {},
            ),
            ([{"id": 3, "body": marker}], {}),
        ]
        with (
            patch.object(knowledge_mr, "_authorization", return_value={}),
            patch.object(knowledge_mr, "_request", side_effect=responses) as request,
        ):
            facts = knowledge_mr.read_review(knowledge_mr.parse_target(FIRST), discussions=True)
        self.assertEqual(facts["commits"], "first first page\nsecond second page")
        self.assertEqual(len(str(facts["diff"])), 3000)
        self.assertEqual(request.call_args_list[2].args[2], detail + "/commits")
        self.assertEqual(request.call_args_list[2].kwargs["params"]["page"], "2")
        self.assertEqual(request.call_args_list[-1].args[2], root + "/issues/7/comments")
        threads = facts["discussions"]
        assert isinstance(threads, list)
        self.assertEqual(len(threads), 2)
        inline = as_object(threads[0])
        self.assertEqual(inline["path"], "api.py")
        self.assertEqual(
            inline["notes"],
            [
                {"author": "bob", "role": "reviewer", "body": "Add a retry cap?"},
                {"author": "alice", "role": "author", "body": "Applied cap"},
            ],
        )
        self.assertEqual(as_object(threads[1])["state"], "APPROVED")

    def test_draft_keeps_canonical_source_even_if_agent_invents_one(self) -> None:
        facts: dict[str, JsonValue] = {"title": "Original title", "author": "alice", "date": "2026-11-18"}
        reply = {"title": None, "body": "## Solution\nCap retries.", "tags": ["retry"], "source": SECOND}
        with patch.object(knowledge_classifier, "_answer", return_value=json.dumps(reply)) as answer:
            draft = knowledge_classifier.draft_review("claude", FIRST, facts, "en")
        self.assertIn("English", answer.call_args.args[1])
        self.assertEqual(draft["kind"], "learning")
        self.assertEqual(draft["title"], "Original title")
        self.assertEqual(
            draft["source"],
            {
                "type": "pull_request",
                "ref": FIRST,
                "review_author": "alice",
                "review_date": "2026-11-18",
            },
        )

    def test_manual_review_displays_overlap_and_allows_edit_or_skip(self) -> None:
        draft: dict[str, JsonValue] = {
            "title": "重试",
            "kind": "learning",
            "body": "限定次数。",
            "source": {"type": "pull_request", "ref": FIRST},
        }
        with (
            patch("sys.stderr", new_callable=StringIO) as preview,
            patch("builtins.input", side_effect=["edit 限制网关重试", "skip"]),
        ):
            accepted = review_import(draft, similar_titles=["已有网关约定"])
            self.assertIsNone(review_import(draft))
        assert accepted is not None
        self.assertEqual(accepted["title"], "限制网关重试")
        self.assertIn("已有网关约定", preview.getvalue())

    def test_resume_reuses_successful_reads_and_published_operations(self) -> None:
        device = Client("https://weora.example.com", str(uuid4()), "fixture-device")
        publish = Mock(return_value={"items": ["confirmed"]})
        refresh = Mock(
            side_effect=[
                iter([{"status": "updated", "urls": [FIRST]}]),
                iter([{"status": "unchanged", "urls": [SECOND]}]),
            ]
        )
        project = str(uuid4())

        def drafted(ref: str) -> dict[str, JsonValue]:
            return {"title": ref, "kind": "learning", "body": "经验", "source": {"type": "pull_request", "ref": ref}}

        with (
            TemporaryDirectory() as scratch,
            patch.object(knowledge_rule_import, "config_path", return_value=Path(scratch) / "device.json"),
            patch.object(knowledge_rule_import, "read_review", return_value={"title": "评审"}) as read,
            patch.object(
                knowledge_rule_import,
                "draft_review",
                side_effect=[
                    drafted(FIRST),
                    CollabError("TEST_CLASSIFIER", "生成失败"),
                    drafted(SECOND),
                ],
            ) as draft,
        ):
            with self.assertRaises(CollabError) as incomplete:
                knowledge_rule_import.run_import(
                    device,
                    project=project,
                    accept_all=True,
                    publish=publish,
                    mr_sources=[FIRST, FIRST, SECOND],
                    refresh=refresh,
                )
            failure = as_object(incomplete.exception.data)
            self.assertEqual(incomplete.exception.code, "KNOWLEDGE_MR_IMPORT_INCOMPLETE")
            self.assertEqual(failure["failures"], [{"source": SECOND, "stage": "draft", "code": "TEST_CLASSIFIER"}])
            accepted = publish.call_args.args[1][0]
            run = str(failure["run_id"])
            result = knowledge_rule_import.run_import(
                device,
                resume=run,
                driver="codex",
                publish=publish,
                refresh=refresh,
            )
            self.assertEqual(
                knowledge_rule_import.run_import(device, resume=run, publish=publish, refresh=refresh), result
            )
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(draft.call_args.args[:2], ("codex", SECOND))
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(publish.call_args.args[0], project)
        self.assertEqual(publish.call_args.args[1][0], accepted)
        self.assertEqual([call.args[2] for call in refresh.call_args_list], [[FIRST], [SECOND]])

    def test_successful_skipped_drafts_still_refresh_and_dry_runs_do_not(self) -> None:
        client = Client("https://example.com", str(uuid4()), "isolated")
        publisher = Mock(return_value={"items": []})
        refresh = Mock(side_effect=[iter([{"status": "failed", "code": "REPOSITORY_SYNC_TIMEOUT", "urls": [FIRST]}])])
        draft = {
            "kind": "learning",
            "title": "变更",
            "body": "可审阅情况",
            "source": {"type": "pull_request", "ref": FIRST},
        }
        with (
            TemporaryDirectory() as tmp,
            patch.object(knowledge_rule_import, "config_path", return_value=Path(tmp) / "device.json"),
            patch.object(knowledge_rule_import, "_requires_terminal"),
            patch.object(knowledge_rule_import, "read_review", return_value={"title": "变更"}),
            patch.object(knowledge_rule_import, "draft_review", return_value=draft),
            patch.object(knowledge_rule_import, "review_import", return_value=None),
        ):
            preview = knowledge_rule_import.run_import(
                client,
                mr_sources=[FIRST],
                preview=True,
                publish=publisher,
                refresh=refresh,
            )
            self.assertIn("drafts", preview)
            refresh.assert_not_called()
            result = knowledge_rule_import.run_import(client, mr_sources=[FIRST], publish=publisher, refresh=refresh)
            self.assertEqual(result["accepted"], 0)
            self.assertEqual(
                result["codebase_refresh"],
                [
                    {"status": "failed", "code": "REPOSITORY_SYNC_TIMEOUT", "urls": [FIRST]},
                ],
            )
            self.assertEqual(refresh.call_args.args[2], [FIRST])
            publisher.assert_not_called()
