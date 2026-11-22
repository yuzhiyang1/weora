"""验证本地目录采集边界和实际 CLI 参数分发。"""

import argparse
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge, knowledge_local_code
from weora_cli.collab_client import Client, CollabError, as_object


class LocalCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "demo"
        self.root.mkdir()
        self.parser = argparse.ArgumentParser()
        knowledge.arguments(self.parser)

    def test_preview_lists_only_selected_paths_without_reading_or_authenticating(self) -> None:
        (self.root / "app.py").write_text("def 起点(): pass", encoding="utf-8")
        (self.root / "module.py").write_text("another", encoding="utf-8")
        (self.root / "module.test.ts").write_text("excluded", encoding="utf-8")
        (self.root / ".credential.py").write_text("excluded", encoding="utf-8")
        for directory in ("node_modules", "tests"):
            folder = self.root / directory
            folder.mkdir()
            (folder / "main.py").write_text("excluded", encoding="utf-8")
        args = self.parser.parse_args(["import", "--dir", str(self.root), "--dry-run", "--max-files", "1"])
        with (
            patch.object(knowledge, "paired_client") as client,
            patch.object(knowledge_local_code, "_read") as read,
        ):
            result = as_object(knowledge.execute(args))
            self.assertEqual(
                result,
                {
                    "name": "demo",
                    "candidates": 2,
                    "max_files": 1,
                    "preview": True,
                    "files": ["app.py"],
                },
            )
        client.assert_not_called()
        read.assert_not_called()

    @unittest.skipUnless(shutil.which("git"), "需要实际 Git 命令")
    def test_checkout_reports_relative_subdirectory_and_ignores_credential_origin(self) -> None:
        def git(*arguments: str) -> str:
            return subprocess.check_output(["git", "-C", str(self.root), *arguments], text=True).strip()

        git("init", "-q")
        git("remote", "add", "origin", "git@github.com:acme/service.git")
        child = self.root / "src"
        child.mkdir()
        source = child / "app.py"
        source.write_bytes(b"def checkout(): pass\n")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        first = git("rev-parse", "HEAD")
        clean = {"origin": "ssh://git@github.com/acme/service.git", "commit": first, "subdir": "", "dirty": False}
        preview = knowledge_local_code.local_snapshot(self.root, limit=200, preview=True, incremental=False)
        self.assertEqual(preview["checkout"], clean)
        self.assertEqual(preview["files"], ["src/app.py"])
        source.write_bytes(b"def local_change(): pass\n")
        snapshot = knowledge_local_code.local_snapshot(child, limit=200, preview=False, incremental=True)
        self.assertEqual(snapshot["checkout"], clean | {"subdir": "src", "dirty": True})
        self.assertEqual(snapshot["files"], [{"path": "app.py", "text": "def local_change(): pass\n"}])
        self.assertNotIn(str(self.root), json.dumps(snapshot))
        git("remote", "set-url", "origin", "https://credential@example.invalid/acme/service.git")
        unsafe = knowledge_local_code.local_snapshot(child, limit=200, preview=True, incremental=False)
        self.assertNotIn("checkout", unsafe)

    def test_publish_uploads_relative_paths_and_rejects_binary_source(self) -> None:
        source = self.root / "app.py"
        source.write_text("def checkout(): pass", encoding="utf-8")
        project = str(uuid4())
        client = Client("https://example.invalid", str(uuid4()), "fixture")
        args = self.parser.parse_args(
            ["import", "--dir", str(self.root), "--project", project, "--incremental", "--skip-enrich"],
        )
        with (
            patch.object(knowledge, "paired_client", return_value=client),
            patch.object(
                knowledge,
                "_call",
                return_value={"indexed": 1},
            ) as request,
        ):
            self.assertEqual(knowledge.execute(args), {"indexed": 1})
            self.assertEqual(request.call_args.args, (client, project, "/codebases/local"))
            payload = request.call_args.kwargs["payload"]
            self.assertEqual(payload["files"], [{"path": "app.py", "text": "def checkout(): pass"}])
            self.assertTrue(payload["incremental"])
            self.assertEqual(len(payload["source_key"]), 64)
            self.assertNotIn(str(self.root), json.dumps(payload))
            source.write_bytes(b"\x00binary")
            with self.assertRaises(CollabError) as error:
                knowledge.execute(args)
            self.assertEqual(error.exception.code, "KNOWLEDGE_LOCAL_UNREADABLE")
            self.assertEqual(request.call_count, 1)
