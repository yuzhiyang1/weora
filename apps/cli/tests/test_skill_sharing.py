"""用独立工具目录验证 21 项配置与下载、更新、归因及恢复。"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch
from uuid import uuid4
from zipfile import ZipFile

from weora_cli.collab_client import Client, CollabError, as_object, config_path, write_json
from weora_cli.commands import JsonValue
from weora_cli.skill_adapters import TOOLS, destination, install_hook
from weora_cli.skill_local import change_files, database, digest, inventory, recover
from weora_cli.skill_telemetry import observe
from weora_cli.skills import SkillClient, bind, client_for, flush, install, lookup, pull


class SkillSharingTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(
            os.environ,
            {
                "WEORA_SKILL_STATE": str(self.root / "state"),
                "CODEX_HOME": str(self.root / "codex"),
                "LOCALAPPDATA": str(self.root / "app-data"),
                "XDG_CONFIG_HOME": str(self.root / "xdg"),
                "OPENCLAW_HOME": str(self.root),
                "WEORA_COLLAB_CONFIG": str(self.root / "collaboration.json"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        home = patch("pathlib.Path.home", return_value=self.root / "home")
        home.start()
        self.addCleanup(home.stop)

    def test_twenty_one_adapters_keep_user_settings_and_install_idempotently(self) -> None:
        self.assertEqual(
            set(TOOLS),
            set(
                [
                    "claude",
                    "codex",
                    "cursor",
                    "codebuddy",
                    "workbuddy",
                    "copilot",
                    "opencode",
                    "pi",
                    "omp",
                    "openclaw",
                    "hermes",
                    "dsh",
                    "qoder",
                    "qoder-cn",
                    "kiro",
                    "trae",
                    "trae-cn",
                    "zcode",
                    "joycode",
                    "muse",
                    "agy",
                ]
            ),
        )
        claude = self.root / "home" / ".claude" / "settings.json"
        claude.parent.mkdir(parents=True)
        claude.write_text(
            json.dumps(
                {
                    "env": {"keep": "yes"},
                    "hooks": {
                        "SessionStart": [{"hooks": [{"type": "command", "command": "existing-command"}]}],
                    },
                }
            ),
            encoding="utf-8",
        )
        hermes = destination("hermes", None).parent / "config.yaml"
        hermes.parent.mkdir(parents=True)
        hermes.write_text("# Keep the comment\nmodel: existing\n", encoding="utf-8")
        for runtime in TOOLS:
            with self.subTest(runtime=runtime):
                result = install_hook(runtime)
                self.assertTrue(destination(runtime, None).is_relative_to(self.root))
                self.assertEqual(result["mode"], "native")
                path = Path(str(result["path"]))
                first = path.read_bytes()
                self.assertEqual(install_hook(runtime), result)
                self.assertEqual(path.read_bytes(), first)
        doc = json.loads(claude.read_text(encoding="utf-8"))
        self.assertEqual(doc["env"], {"keep": "yes"})
        self.assertEqual(doc["hooks"]["SessionStart"][0]["hooks"][0]["command"], "existing-command")
        self.assertEqual(len(doc["hooks"]["SessionStart"]), 2)
        self.assertIn("# Keep the comment", hermes.read_text(encoding="utf-8"))
        for runtime in ("trae", "trae-cn", "kiro"):
            config = json.loads(Path(str(install_hook(runtime)["path"])).read_text(encoding="utf-8"))
            hooks = config["hooks"]
            self.assertEqual(
                {row["trigger"] for row in hooks} if runtime == "kiro" else set(hooks),
                {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"},
            )

    def package(self, version: str, *, extra: str = "") -> tuple[dict[str, JsonValue], bytes, str]:
        body = f"---\nname: demo\ndescription: Demo\n---\n\n# Instructions {version}\n"
        package = self.root / f"{uuid4().hex}.zip"
        with ZipFile(package, "w") as zipped:
            zipped.writestr("demo/SKILL.md", body)
            if extra:
                zipped.writestr(extra, "unsafe")
        archive = package.read_bytes()
        return (
            {
                "slug": "demo",
                "version": version,
                "version_id": str(uuid4()),
                "package_sha256": digest(archive),
            },
            archive,
            body,
        )

    def test_bind_pin_usage_update_rollback_and_local_conflict(self) -> None:
        first, first_archive, first_body = self.package("1.0.0")
        second, second_archive, _ = self.package("2.0.0")
        bad, bad_archive, _ = self.package("3.0.0", extra="demo/../escape")
        archives = {
            first["version_id"]: first_archive,
            second["version_id"]: second_archive,
            bad["version_id"]: bad_archive,
        }
        device = str(uuid4())
        manifest: dict[str, JsonValue] = {
            "device_id": device,
            "complete": True,
            "usage_enabled": True,
            "items": [first],
        }
        report: list[str] = []
        project = self.root / "checkout"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        fake = Mock(spec=SkillClient)
        fake.base, fake.workspace, fake.token = "http://localhost:80", str(uuid4()), "test-device"
        config = {"base": fake.base, "workspace": fake.workspace}
        write_json(config_path(), config | {"device_id": device})

        def request(method: str, path: str, data: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
            if method == "GET":
                return manifest
            report.append(path)
            if path == "/usage-events/batch":
                assert data is not None
                events = data["events"]
                assert isinstance(events, list)
                self.assertEqual(len(events), 2)
                self.assertNotIn(first_body, str(events))
                self.assertNotIn(str(project), str(events))
                self.assertEqual(as_object(events[0])["turn_key"], as_object(events[1])["turn_key"])
                return {"items": [{"status": "accepted"}, {"status": "accepted"}]}
            return {"status": "accepted"}

        def download(version: str, checksum: str, output: Path) -> None:
            body = archives[version]
            self.assertEqual(digest(body), checksum)
            output.write_bytes(body)

        fake.request.side_effect = request
        fake.download.side_effect = download
        client = cast(SkillClient, fake)
        with (
            patch("weora_cli.skills.paired_client", return_value=Client(fake.base, fake.workspace, fake.token)),
            patch("weora_cli.skills.SkillClient", return_value=client),
        ):
            bind("codex", "project", str(project))
        target = project / ".agents/skills/demo"
        manifest_file = target / "SKILL.md"
        self.assertEqual(manifest_file.read_text(encoding="utf-8"), first_body)
        self.assertIn(
            "/.agents/skills/demo/",
            (project / ".git/info/exclude").read_text(encoding="utf-8"),
        )
        with database() as db:
            bound = lookup(db, "codex", "project", str(project))
            observe(db, "codex", "prompt", {"session_id": "test-session", "prompt": "$demo summarize"}, project)
            observe(
                db,
                "codex",
                "tool",
                {
                    "session_id": "test-session",
                    "tool_name": "read_file",
                    "tool_input": {"path": str(manifest_file)},
                    "tool_response": first_body,
                },
                project,
            )
            self.assertEqual(flush(db, bound, client), 2)
            self.assertEqual(report, ["/installation-receipts", "/usage-events/batch"])
            install(db, bound, client, first, pinned=True)
            manifest["items"] = [second]
            pull(db, bound, client, manifest=manifest)
            self.assertEqual(manifest_file.read_text(encoding="utf-8"), first_body)
            install(db, bound, client, second, pinned=False)
            self.assertIn("2.0.0", manifest_file.read_text(encoding="utf-8"))
            install(db, bound, client, first, pinned=True)
            self.assertEqual(manifest_file.read_text(encoding="utf-8"), first_body)
            with self.assertRaisesRegex(CollabError, "归档路径不安全"):
                install(db, bound, client, bad, pinned=False)
            self.assertEqual(manifest_file.read_text(encoding="utf-8"), first_body)
            # 模拟文件换版后、SQLite 提交前退出；下次同步需恢复旧版本。
            incoming = self.root / "incoming.zip"
            incoming.write_bytes(second_archive)
            db.execute("BEGIN IMMEDIATE")
            change_files(db, target, incoming, "demo", uuid4().hex, inventory(target))
            db.rollback()
            with db:
                db.execute("BEGIN IMMEDIATE")
                recover(db)
            self.assertEqual(manifest_file.read_text(encoding="utf-8"), first_body)

            # 旧设备的离线点名留在本机时重新登录，不得算给新设备。
            observe(db, "codex", "prompt", {"session_id": "old-device", "prompt": "$demo"}, project)
            self.assertEqual(db.execute("SELECT count(*) FROM pending WHERE kind='usage'").fetchone()[0], 1)
            old_generation = db.execute("SELECT generation FROM installation").fetchone()[0]
            manifest["device_id"] = str(uuid4())
            for stale_config in ({"base": fake.base}, config | {"device_id": manifest["device_id"]}):
                write_json(config_path(), stale_config)
                with patch("weora_cli.skills.keyring.get_password", return_value="old-device-token") as keyring:
                    with self.assertRaises(CollabError) as caught:
                        client_for(bound)
                    self.assertEqual(caught.exception.code, "SKILL_DEVICE_NOT_PAIRED")
                    keyring.assert_not_called()
                observe(db, "codex", "prompt", {"session_id": "inactive-device", "prompt": "$demo"}, project)
                self.assertEqual(db.execute("SELECT count(*) FROM pending WHERE kind='usage'").fetchone()[0], 1)
            with (
                patch("weora_cli.skills.paired_client", return_value=Client(fake.base, fake.workspace, fake.token)),
                patch("weora_cli.skills.SkillClient", return_value=client),
            ):
                bind("codex", "project", str(project))
            bound = lookup(db, "codex", "project", str(project))
            self.assertEqual(bound["device"], manifest["device_id"])
            self.assertNotEqual(db.execute("SELECT generation FROM installation").fetchone()[0], old_generation)
            self.assertEqual(db.execute("SELECT count(*) FROM pending").fetchone()[0], 0)
            self.assertEqual(manifest_file.read_text(encoding="utf-8"), first_body)
            manifest_file.write_text("personal changes", encoding="utf-8")
            manifest["items"] = []
            result = pull(db, bound, client, manifest=manifest)
            self.assertIn("conflict", str(result["changes"]))
            self.assertEqual(manifest_file.read_text(encoding="utf-8"), "personal changes")
