"""验证规则导入的范围、隐私筛选以及断点续做。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from weora_cli import knowledge_rule_import
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.knowledge_classifier import parse_classification


class RuleImportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = patch.object(knowledge_rule_import, "config_path", return_value=self.root / "collaboration.json")
        config.start()
        self.addCleanup(config.stop)
        self.device = Client("https://weora.example.com", str(uuid4()), "fixture-device")

    def test_scanning_uses_tool_home_and_returns_only_rule_excerpts(self) -> None:
        homes = {"claude": self.root / "relocated-claude", "cursor": self.root / "cursor"}
        claude = homes["claude"] / "rules" / "release"
        cursor = homes["cursor"] / "rules"
        claude.mkdir(parents=True)
        cursor.mkdir(parents=True)
        (claude / "release.md").write_text("规" * 3400, encoding="utf-8")
        (claude / "large.md").write_bytes(b"z" * (knowledge_rule_import.MAX_RULE_BYTES + 1))
        (claude / "private.json").write_text("{}", encoding="utf-8")
        (cursor / "team.MDC").write_text("---\nglobs: '*.ts'\ndomain: ops\n---\nCursor rule", encoding="utf-8")
        with patch.object(
            knowledge_rule_import, "tool_home", side_effect=lambda tool: homes.get(tool, self.root / tool)
        ):
            candidates = [as_object(row) for row in knowledge_rule_import.scan_rules()]
        self.assertEqual(
            [row["ref"] for row in candidates], ["claude/rules/release/release.md", "cursor/rules/team.MDC"]
        )
        self.assertEqual(candidates[0]["excerpt"], "规" * 3000)
        self.assertEqual(candidates[1]["rule_paths"], "*.ts")
        self.assertEqual(candidates[1]["domain"], "ops")

    def test_classification_and_publish_resume_without_reopening_previous_decisions(self) -> None:
        staged = [
            {"ref": "a.md", "excerpt": "team rule", "rule_paths": "src/**", "domain": "neutral"},
            {"ref": "b.md", "excerpt": "private"},
        ]
        publisher = Mock(side_effect=[CollabError("WEORA_UNREACHABLE", "离线"), {"items": ["ok"]}])
        reviewed = {
            "kind": "rule",
            "title": "上线规则",
            "body": "在工作日发布。",
            "source": {"type": "file", "ref": "a.md"},
        }

        def fail_second(driver: str, ref: str, excerpt: str) -> object:
            if ref == "b.md":
                raise CollabError("KNOWLEDGE_CLASSIFIER_FAILED", "分类失败")
            return reviewed

        project = str(uuid4())
        with (
            patch.object(knowledge_rule_import, "scan_rules", return_value=staged),
            patch.object(knowledge_rule_import, "classify_rule", side_effect=fail_second),
            self.assertRaises(CollabError) as incomplete,
        ):
            knowledge_rule_import.run_import(self.device, project=project, accept_all=True, publish=publisher)
        run = str(as_object(incomplete.exception.data)["run_id"])
        publisher.assert_not_called()
        with patch.object(knowledge_rule_import, "classify_rule", return_value=None) as classifier:
            with self.assertRaises(CollabError) as offline:
                knowledge_rule_import.run_import(self.device, resume=run, publish=publisher)
            classifier.assert_called_once_with("claude", "b.md", "private")
            self.assertEqual(as_object(offline.exception.data)["run_id"], run)
            result = knowledge_rule_import.run_import(self.device, resume=run, publish=publisher)
            classifier.assert_called_once()
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(publisher.call_args_list[0], publisher.call_args_list[1])
        self.assertEqual(publisher.call_args.args[0], project)
        self.assertEqual(publisher.call_args.args[1][0]["rule_paths"], "src/**")
        self.assertEqual(publisher.call_args.args[1][0]["domain"], "neutral")
        self.assertEqual(knowledge_rule_import.run_import(self.device, resume=run, publish=publisher), result)
        self.assertEqual(publisher.call_count, 2)

    def test_classification_requires_explicit_public_decision_and_never_stores_excerpt(self) -> None:
        result = {"type": "doc", "title": "接口", "summary": "分页使用游标。", "tags": ["分页"], "isPersonal": False}
        ref = "claude/rules/api.md"
        payload = parse_classification(json.dumps(result), ref)
        self.assertEqual(
            payload,
            {
                "kind": "doc",
                "title": "接口",
                "body": "分页使用游标。",
                "tags": ["分页"],
                "source": {"type": "file", "ref": ref},
            },
        )
        self.assertIsNone(parse_classification(json.dumps(result | {"isPersonal": True}), ref))
        self.assertIsNone(parse_classification("未返回 JSON", ref))

    def test_output_works_without_a_device_and_never_replaces_a_local_file(self) -> None:
        text = {"type": "rule", "title": "发布", "summary": "工作日发布。", "tags": [], "isPersonal": False}
        payload = parse_classification(json.dumps(text), "claude/rules/deploy.md")
        publish = Mock()
        with (
            patch.object(knowledge_rule_import, "scan_rules", return_value=[{"ref": "deploy.md", "excerpt": "sample"}]),
            patch.object(knowledge_rule_import, "classify_rule", return_value=payload),
        ):
            result = knowledge_rule_import.run_import(
                None,
                accept_all=True,
                output=self.root / "out",
                publish=publish,
            )
        written = result["written"]
        assert isinstance(written, list)
        generated = Path(str(written[0]))
        self.assertIn("工作日发布。", generated.read_text(encoding="utf-8"))
        generated.write_text("本机修改", encoding="utf-8")
        self.assertEqual(
            knowledge_rule_import.run_import(None, resume=str(result["run_id"]), publish=publish),
            result,
        )
        self.assertEqual(generated.read_text(encoding="utf-8"), "本机修改")
        publish.assert_not_called()
