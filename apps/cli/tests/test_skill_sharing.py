"""用独立工具目录验证 21 项配置与下载、更新、归因及恢复。"""

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from zipfile import ZipFile

from weora_cli.collab_client import Client, CollabError, as_object, config_path, write_json
from weora_cli.commands import JsonValue
from weora_cli.skill_adapters import TOOLS, destination, install_hook
from weora_cli.skill_local import change_files, database, digest, inventory, recover
from weora_cli.skill_telemetry import observe
from weora_cli.skill_upload import push
from weora_cli.skills import SkillClient, arguments, bind, client_for, execute, flush, install, lookup, pull


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

    def test_binding_upgrade_preserves_shared_state_and_sends_project_scope(self) -> None:
        state = self.root / "state"
        state.mkdir()
        project = uuid4()
        device = str(uuid4())
        client = Client("http://localhost:80", str(uuid4()), "test-device")
        root = str(self.root / "checkout")
        profile = str(self.root / "codex")
        url_calls: list[str] = []
        # 模拟旧版本已经留下绑定，验证新增字段时能保留原有绑定 ID。
        with closing(sqlite3.connect(state / "skills.sqlite3")) as db, db:
            db.execute("""
                CREATE TABLE binding (
                    id TEXT PRIMARY KEY, root TEXT NOT NULL, runtime TEXT NOT NULL, scope TEXT NOT NULL,
                    profile TEXT NOT NULL, base TEXT NOT NULL, workspace TEXT NOT NULL, device TEXT NOT NULL,
                    checked TEXT, usage INTEGER NOT NULL DEFAULT 0, UNIQUE(root, runtime, scope, profile)
                )
            """)
            db.execute(
                "INSERT INTO binding(id, root, runtime, scope, profile, base, workspace, device) "
                "VALUES ('original', ?, 'codex', 'project', ?, ?, ?, ?)",
                (root, profile, client.base, client.workspace, device),
            )

        def request(method: str, url: str, *args: object, **kwargs: object) -> dict[str, JsonValue]:
            url_calls.append(url)
            value = parse_qs(urlparse(url).query).get("project_id", [None])[0]
            return {"device_id": device, "project_id": value, "complete": True, "items": []}

        with (
            patch("weora_cli.skills.paired_client", return_value=client),
            patch("weora_cli.collab_client.request_json", side_effect=request),
        ):
            self.assertEqual(bind("codex", "project", root, project_id=project)["project_id"], str(project))
            self.assertEqual(bind("codex", "project", root)["project_id"], str(project))
            with self.assertRaises(CollabError) as wrong_project:
                bind("claude", "project", root, project_id=uuid4())
            self.assertEqual(bind("claude", "project", root)["project_id"], str(project))
            self.assertEqual(wrong_project.exception.code, "SKILL_BINDING_CONFLICT")
            with self.assertRaises(CollabError) as user_scope:
                bind("codex", "user", "", project_id=project)
            self.assertEqual(user_scope.exception.code, "SKILL_PROJECT_SCOPE_REQUIRED")
        with database() as db:
            bound = lookup(db, "codex", "project", root)
            self.assertEqual((bound["id"], bound["project_id"]), ("original", str(project)))
        self.assertIn(f"runtime=codex&scope=project&project_id={project}", url_calls[0])
        scoped = SkillClient(client.base, client.workspace, client.token, project_id=str(project))
        self.assertEqual(
            scoped._scoped_path("/versions/example/package"), f"/versions/example/package?project_id={project}",
        )

    def test_push_of_an_installed_skill_follows_the_original_binding(self) -> None:
        project, version = uuid4(), uuid4()
        client = Client("http://localhost:80", str(uuid4()), "test-device")
        device = str(uuid4())
        root = self.root / "checkout"
        target = destination("codex", root) / "release-check"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(
            "---\nname: verification-checklist\ndescription: Team check.\n---\n\nEdited.", encoding="utf-8",
        )
        write_json(config_path(), {"base": client.base, "workspace": client.workspace, "device_id": device})
        with database() as db, db:
            db.execute(
                "INSERT INTO binding(id, root, runtime, scope, profile, base, workspace, device, project_id) "
                "VALUES ('bound', ?, 'codex', 'project', ?, ?, ?, ?, ?)",
                (str(root), os.environ["CODEX_HOME"], client.base, client.workspace, device, str(project)),
            )
            db.execute(
                "INSERT INTO installation VALUES ('bound', 'release-check', ?, ?, ?, ?, '1.0.0', '', '{}', 0)",
                (str(target), str(uuid4()), str(uuid4()), str(version)),
            )
        with (
            patch("weora_cli.skill_upload.paired_client", return_value=client),
            patch("weora_cli.skill_upload.request_json", return_value={"status": "pending_review"}) as send,
        ):
            self.assertEqual(push(str(target))["status"], "pending_review")
            self.assertIn(f"project_id={project}", send.call_args.args[1])
            body = send.call_args.kwargs["body"]
            self.assertIn(f'name="source_version_id"\r\n\r\n{version}'.encode(), body)
            self.assertIn(b"release-check/SKILL.md", body)
            with self.assertRaises(CollabError) as wrong_project:
                push(str(target), project=uuid4())
            self.assertEqual(wrong_project.exception.code, "SKILL_CONTRIBUTION_ORIGIN")
            write_json(config_path(), {"base": client.base, "workspace": client.workspace, "device_id": str(uuid4())})
            with self.assertRaises(CollabError) as wrong_device:
                push(str(target))
            self.assertEqual(wrong_device.exception.code, "SKILL_CONTRIBUTION_ORIGIN")
            self.assertEqual(send.call_count, 1)

    def test_bind_pin_usage_update_rollback_and_local_conflict(self) -> None:
        first, first_archive, first_body = self.package("1.0.0")
        second, second_archive, second_body = self.package("2.0.0")
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
        parser = argparse.ArgumentParser()
        arguments(parser)
        with (
            patch("weora_cli.skills.client_for", return_value=client),
            patch("weora_cli.skills.publish_activity", return_value={"reported": 1, "pending": 0}) as publish_activity,
        ):
            result = execute(parser.parse_args(["pull", "--runtime", "codex", "--project", str(project)]))
            self.assertEqual(as_object(result)["activity"], {"reported": 1, "pending": 0})
            publish_activity.assert_called_once_with(client, project.resolve())
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
            manifest_file.write_text("local unpublished edit", encoding="utf-8")
            with self.assertRaises(CollabError) as mismatch:
                install(db, bound, client, second, pinned=False)
            self.assertEqual(mismatch.exception.code, "SKILL_LOCAL_CONFLICT")
            self.assertEqual(manifest_file.read_text(encoding="utf-8"), "local unpublished edit")
            # 审核发布与本地提交一致时，只更新安装记录和回执。
            manifest_file.write_bytes(second_body.encode())
            unchanged_at = manifest_file.stat().st_mtime_ns
            self.assertEqual(install(db, bound, client, second, pinned=False), "updated")
            self.assertEqual(manifest_file.stat().st_mtime_ns, unchanged_at)
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
