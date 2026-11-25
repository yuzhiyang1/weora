"""覆盖团队经验晋升的只读预演、Agent 选择和网络重放。"""

import json
import os
import unittest
from argparse import ArgumentParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge, knowledge_promotion
from weora_cli.collab_client import Client, CollabError
from weora_cli.commands import JsonValue


class KnowledgePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client("https://weora.example.com", str(uuid4()), "fixture-device")
        self.entry, self.project = str(uuid4()), str(uuid4())
        self.candidate: dict[str, JsonValue] = {
            "id": self.entry, "title": "分页接口约束", "body": "总是使用固定游标。",
            "revision": 4, "project_id": self.project, "kind": "rule",
            "targets": {"rule": f"rules/{self.entry}.md", "doc": f"docs/{self.entry}.md"},
        }
        self.parser = ArgumentParser()
        knowledge.arguments(self.parser)

    def command(self, *args: str) -> JsonValue:
        return knowledge.execute(self.parser.parse_args(["promote", "--project", self.project, *args]))

    def test_listing_and_preview_are_read_only(self) -> None:
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge_promotion, "call", side_effect=[
                {"items": [self.candidate]}, {"items": [self.candidate]},
                self.candidate, self.candidate | {"occupied_kinds": ["rule"]},
            ]) as api,
            patch.object(knowledge_promotion, "_answer") as model,
        ):
            self.assertEqual(self.command("--dry-run"), {"items": [self.candidate]})
            self.assertEqual(self.command("--project-only"), {"items": [self.candidate]})
            self.assertEqual(self.command(self.entry, "--category", "docs", "--dry-run"), {
                "preview": True, "source": self.entry, "source_revision": 4, "project_id": self.project,
                "kind": "doc", "target": f"docs/{self.entry}.md",
            })
            with self.assertRaises(CollabError) as occupied:
                self.command(self.entry, "--dry-run")
            self.assertEqual(occupied.exception.code, "KNOWLEDGE_CONFLICT")
        model.assert_not_called()
        self.assertEqual([call.kwargs for call in api.call_args_list], [
            {"include_shared": 1}, {}, {"include_shared": 1}, {"include_shared": 1},
        ])

    def test_interrupted_shared_submit_keeps_origin_and_original_draft(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(Path(directory) / "collaboration.json")}, clear=True),
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch("weora_cli.knowledge_promotion.shutil.which", return_value="codex"),
            patch.object(knowledge_promotion, "_answer", side_effect=[
                json.dumps({"kind": "skill", "reason": "可复用的操作流程。"}),
                json.dumps({"body": "依次核对第一页、边界页和游标。"}),
            ]) as model,
            patch.object(knowledge_promotion, "call", side_effect=[
                self.candidate | {"project_id": None}, CollabError("WEORA_UNREACHABLE", "响应中断"),
                {"status": "pending"},
            ]) as api,
        ):
            with self.assertRaises(CollabError):
                self.command(self.entry, "--via", "codex")
            with self.assertRaises(CollabError) as rejected:
                self.command(self.entry, "--via", "codex", "--project-only")
            self.assertEqual(rejected.exception.code, "KNOWLEDGE_LIFECYCLE_SCOPE")
            self.assertEqual(self.command(self.entry, "--via", "codex"), {"status": "pending"})
            self.assertEqual(list(Path(directory).rglob("*.json")), [])
        self.assertEqual(model.call_count, 2)
        self.assertEqual([item.args[1] for item in api.call_args_list], [self.project, None, None])
        first, replay = (item.kwargs["payload"] for item in api.call_args_list[1:])
        self.assertEqual(first, replay)
        self.assertEqual(first["source_revision"], 4)
        self.assertEqual(first["kind"], "skill")
        self.assertEqual(first["reason"], "可复用的操作流程。")
        self.assertEqual(first["body"], "依次核对第一页、边界页和游标。")

    def test_only_implicitly_missing_model_can_fall_back_to_the_original_body(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(Path(directory) / "collaboration.json")}, clear=True),
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch("weora_cli.knowledge_promotion.shutil.which", return_value=None),
            patch.object(knowledge_promotion, "call", side_effect=[
                self.candidate, self.candidate, {"status": "pending"},
            ]) as api,
        ):
            with self.assertRaises(CollabError) as unavailable:
                self.command(self.entry, "--via", "codex")
            self.assertEqual(unavailable.exception.code, "KNOWLEDGE_CLASSIFIER_UNAVAILABLE")
            api.assert_called_once()
            self.assertEqual(self.command(self.entry, "--category", "docs"), {"status": "pending"})
        self.assertEqual(api.call_args.kwargs["payload"]["kind"], "doc")
        self.assertEqual(api.call_args.kwargs["payload"]["body"], self.candidate["body"])
