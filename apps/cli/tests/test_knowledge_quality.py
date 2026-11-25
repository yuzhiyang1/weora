"""验证质量候选只预览、隔离失败、保留人工草稿及明确提交。"""

import argparse
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge, knowledge_quality
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue


class QualityTests(unittest.TestCase):
    def test_preview_generate_partial_failure_preserve_edit_and_submit(self) -> None:
        client = Client("https://example.invalid", str(uuid4()), "device")
        project, good, bad = (str(uuid4()) for _ in range(3))
        parser = argparse.ArgumentParser()
        knowledge.arguments(parser)
        rows: list[JsonValue] = [
            {"id": good, "title": "规则", "revision": 2, "recalls": 5},
            {"id": bad, "title": "技术说明", "revision": 1, "recalls": 6},
        ]

        def platform(
            _client: Client,
            _project: str | None,
            suffix: str,
            *,
            method: str = "GET",
            **_: object,
        ) -> dict[str, JsonValue]:
            self.assertEqual(_project, project)
            if suffix == "/maintenance/quality":
                return {"items": rows}
            if method == "POST":
                return {"revision_status": "pending"}
            if suffix.endswith(bad):
                raise CollabError("KNOWLEDGE_CONFLICT", "基准已变化。")
            return {"revision": 2, "title": "规则", "body": "旧正文"}

        with (
            TemporaryDirectory() as folder,
            patch.object(knowledge, "paired_client", return_value=client),
            patch.object(knowledge_quality, "call", side_effect=platform) as service,
            patch.object(knowledge_quality, "draft_quality", return_value="新的可操作正文。") as author,
        ):
            directory = Path(folder) / "drafts"
            args = ["maintenance", "--update-quality", "--project", project, "--output", str(directory)]
            report = as_object(knowledge.execute(parser.parse_args([*args, "--dry-run"])))
            self.assertEqual(report["items"], list(reversed(rows)))
            self.assertFalse(directory.exists())
            author.assert_not_called()

            for _ in range(2):
                with self.assertRaises(CollabError) as failure:
                    knowledge.execute(parser.parse_args(args))
                self.assertEqual(failure.exception.code, "KNOWLEDGE_QUALITY_INCOMPLETE")
                draft = directory / f"{good}.r2.draft.md"
                draft.write_text(
                    draft.read_text(encoding="utf-8").replace("新的可操作正文。", "人工核对后的正文。"),
                    encoding="utf-8",
                )
            author.assert_called_once()
            options = ["maintenance", "--submit-quality", str(draft), "--project", project]
            self.assertEqual(knowledge.execute(parser.parse_args(options)), {"revision_status": "pending"})
            data = service.call_args.kwargs["payload"]
            self.assertEqual(data["body"], "人工核对后的正文。\n")
            self.assertEqual(data["base_revision"], 2)
            knowledge.execute(parser.parse_args(options))
            self.assertEqual(service.call_args.kwargs["payload"], data)

    def test_shared_registry_draft_keeps_origin_and_immutable_version(self) -> None:
        client = Client("https://example.invalid", str(uuid4()), "device")
        project, skill, base = (str(uuid4()) for _ in range(3))
        row = {"id": skill, "title": "Release checklist", "skill_version_id": base, "recalls": 5, "project_id": None}
        body = "Use the reviewed checklist."
        with (
            TemporaryDirectory() as folder,
            patch.object(knowledge_quality, "call", side_effect=[
                {"items": [row]}, row | {"body": "Old body"}, {"revision_status": "pending"},
            ]) as service,
            patch.object(knowledge_quality, "draft_quality", return_value=body),
        ):
            directory = Path(folder)
            result = knowledge_quality.generate(
                client, project, driver="codex", preview=False, output=directory, after=None,
            )
            draft = directory / f"{skill}.v{base}.draft.md"
            self.assertTrue(draft.exists())
            self.assertIn("base_skill_version_id", draft.read_text(encoding="utf-8"))
            self.assertEqual(result["items"], [{
                "id": skill, "base_skill_version_id": base, "draft": str(draft), "status": "drafted",
                "project_id": None,
            }])
            with self.assertRaises(CollabError) as wrong_scope:
                knowledge_quality.submit(client, draft, project)
            self.assertEqual(wrong_scope.exception.code, "KNOWLEDGE_QUALITY_SCOPE")
            self.assertEqual(knowledge_quality.submit(client, draft, None), {"revision_status": "pending"})
            args, kwargs = service.call_args
            self.assertEqual(args[1:], (None, f"/maintenance/quality/{skill}"))
            self.assertEqual(kwargs["payload"]["base_skill_version_id"], base)
            self.assertNotIn("base_revision", kwargs["payload"])
            self.assertEqual(kwargs["payload"]["body"], f"{body}\n")
