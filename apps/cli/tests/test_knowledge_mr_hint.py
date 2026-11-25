"""验证近期合并查询、开关优先级和两阶段会话提醒。"""

import argparse
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge, knowledge_mr, knowledge_mr_hint
from weora_cli.collab_client import Client, as_object
from weora_cli.commands import JsonValue

REMOTE = "git@github.com:acme/catalog.git"
NEW = "https://github.com/acme/catalog/pull/21"
DONE = "https://github.com/acme/catalog/pull/22"
DEFAULT: dict[str, JsonValue] = {"enabled": True, "source": "default", "lookback_days": 7, "max_mrs": 10}


class ReviewHintTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        env = patch.dict(
            os.environ,
            {
                "WEORA_COLLAB_CONFIG": str(self.root / "device.json"),
                "WEORA_MR_HINT_DISABLED": "0",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.device = Client("https://weora.example.com", str(uuid4()), "isolated")
        repo = knowledge_mr.hint_repository(REMOTE)
        assert repo is not None
        self.repo = repo

    def test_origin_and_provider_response_cannot_replace_import_url(self) -> None:
        self.assertEqual(
            knowledge_mr.hint_repository("ssh://git@github.com/acme/catalog.git"),
            self.repo,
        )
        self.assertIsNone(knowledge_mr.hint_repository("https://github.com.example.org/acme/catalog.git"))
        self.assertIsNone(knowledge_mr.hint_repository("https://github.com/acme/../catalog.git"))
        self.assertIsNone(knowledge_mr.hint_repository("https://gitlab.com/acme/catalog.git"))
        now = datetime.now(UTC)
        reply = [
            {"number": 21, "title": "缓存", "merged_at": now.isoformat(), "html_url": "https://example.com/incorrect"},
            {"number": 22, "title": "已关闭", "merged_at": None},
            {"number": 23, "title": "超过窗口", "merged_at": (now - timedelta(days=8)).isoformat()},
        ]
        with (
            patch.object(knowledge_mr, "_github_merged", return_value=None),
            patch.object(knowledge_mr, "_authorization", return_value={}),
            patch.object(knowledge_mr, "_request", return_value=(reply, {})) as api,
        ):
            self.assertEqual(
                knowledge_mr.recent_merges(self.repo, 7, 10),
                [
                    {"url": NEW, "title": "缓存", "merged_at": now.isoformat()},
                ],
            )
            self.assertEqual(api.call_args.args[2], "/repos/acme/catalog/pulls")
            self.assertEqual(api.call_args.kwargs["params"]["per_page"], "10")

    def test_ready_hints_are_consumed_once_and_filter_platform_sources(self) -> None:
        one, two = str(uuid4()), str(uuid4())
        data = [
            {"url": NEW, "title": "重试窗口\n新行", "merged_at": datetime.now(UTC).isoformat()},
            {"url": DONE, "title": "已提炼", "merged_at": datetime.now(UTC).isoformat()},
        ]
        with patch("weora_cli.knowledge_mr_hint.subprocess.Popen") as launch:
            knowledge_mr_hint.start(self.device, self.root, one, lambda: REMOTE)
            generation = launch.call_args.args[0][-2]
            self.assertEqual(knowledge_mr_hint.take(self.device, self.root, one, lambda: REMOTE), "")
            knowledge_mr_hint.start(self.device, self.root, one, lambda: REMOTE)
            newest = launch.call_args.args[0][-2]
            knowledge_mr_hint.start(self.device, self.root, two, lambda: REMOTE)
            second = launch.call_args.args[0][-2]
        with (
            patch.object(knowledge_mr_hint, "call", return_value=DEFAULT),
            patch.object(knowledge_mr_hint, "recent_merges", return_value=data),
        ):
            knowledge_mr_hint.scan(self.device, self.root, one, generation, self.repo)
            self.assertEqual(knowledge_mr_hint.take(self.device, self.root, one, lambda: REMOTE), "")
            knowledge_mr_hint.scan(self.device, self.root, one, newest, self.repo)
            knowledge_mr_hint.scan(self.device, self.root, two, second, self.repo)
        with patch.object(knowledge_mr_hint, "call", return_value={"policy": DEFAULT, "imported": [DONE]}) as api:
            message = knowledge_mr_hint.take(self.device, self.root, one, lambda: REMOTE)
            self.assertIn("weora context import --from-mr " + NEW, message)
            self.assertIn("重试窗口新行", message)
            self.assertNotIn(DONE, message)
            self.assertEqual(knowledge_mr_hint.take(self.device, self.root, one, lambda: REMOTE), "")
            self.assertEqual(knowledge_mr_hint.take(self.device, self.root, two, lambda: REMOTE), "")
            self.assertEqual(api.call_count, 2)

    def test_switch_precedence_and_project_are_independent_of_recall(self) -> None:
        parser = argparse.ArgumentParser()
        knowledge.arguments(parser)
        project, session = str(uuid4()), str(uuid4())
        workspace_off = DEFAULT | {"enabled": False, "source": "workspace"}
        with (
            patch.object(Path, "cwd", return_value=self.root),
            patch.object(knowledge, "paired_client", return_value=self.device),
            patch.object(knowledge_mr_hint, "call", return_value=workspace_off),
            patch.dict(os.environ, {"WEORA_RECALL_DISABLED": "1"}),
            patch("weora_cli.knowledge_mr_hint.subprocess.Popen") as launch,
        ):
            local = as_object(knowledge.execute(parser.parse_args(["mr-hint", "enable", "--project", project])))
            self.assertEqual((local["source"], local["enabled"], local["project"]), ("local", True, project))
            knowledge_mr_hint.start(self.device, self.root / "src", session, lambda: REMOTE)
            self.assertEqual(launch.call_count, 1)
            with patch.dict(os.environ, {"WEORA_MR_HINT_DISABLED": "1"}):
                disabled = as_object(knowledge.execute(parser.parse_args(["mr-hint", "status"])))
                knowledge_mr_hint.start(self.device, self.root, session, lambda: REMOTE)
            self.assertEqual((disabled["source"], disabled["enabled"]), ("environment", False))
            self.assertEqual(launch.call_count, 1)
            inherited = as_object(knowledge.execute(parser.parse_args(["mr-hint", "inherit"])))
            self.assertEqual(
                (inherited["source"], inherited["enabled"], inherited["project"]),
                (
                    "workspace",
                    False,
                    project,
                ),
            )
