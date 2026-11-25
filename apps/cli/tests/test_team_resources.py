"""验证仓库选择、规则覆盖与 Codex 原生钩子输出。"""

import io
import json
import subprocess
from base64 import b64encode
from pathlib import Path

import pytest

from weora_cli import resource_delivery, resources, rule_adapters, rule_import, rule_install, skill_adapters
from weora_cli.cli import build_parser, main
from weora_cli.collab_client import Client
from weora_cli.resource_delivery import Object


def rule(name: str, body: str, tags: list[str] | None = None) -> Object:
    namespace, _, _ = name.rpartition("/")
    return {
        "name": name,
        "namespace": namespace or None,
        "path": f"rules/{name}.md",
        "body": body,
        "description": "",
        "paths": ["src/**/*.java"] if namespace == "backend" else [],
        "tags": tags or [],
        "assets": {},
    }


def test_codex_hook_uses_bound_checkout_and_preserves_existing_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("WEORA_RESOURCE_HOME", str(tmp_path / "state"))
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    home.mkdir()
    (home / "hooks.json").write_text(
        '{"hooks":{"SessionStart":[{"hooks":[{"command":"personal"}]}]}}', encoding="utf-8"
    )
    checkout = tmp_path / "HSK-java"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    monkeypatch.chdir(checkout)
    client = Client("http://127.0.0.1:8999", "00000000-0000-0000-0000-000000000001", "test")
    monkeypatch.setenv("WEORA_COLLAB_URL", client.base)
    monkeypatch.setenv("WEORA_COLLAB_WORKSPACE", client.workspace)
    monkeypatch.setattr(resources, "paired_client", lambda: client)
    rule_snapshot: Object = {
        "revision": "published",
        "files": {},
        "members_configured": False,
        "member_roles": [],
        "roles": [],
        "projects": [{"id": "hsk-java", "knowledge": ["backend"]}],
        "rules": [
            rule("vo", "SHARED"),
            rule("backend/vo", "HSK_VO_RULE", ["java"]),
            rule("frontend/vo", "FRONTEND_ONLY"),
        ],
    }
    monkeypatch.setattr(resources, "snapshot", lambda *args: rule_snapshot)

    def execute(*args: str) -> None:
        resources.execute(build_parser().parse_args(list(args)))

    execute("init", "--agent", "codex")
    execute("projects", "set", "hsk-java")
    execute("pull")
    hook_file = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
    assert hook_file["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "personal"
    assert hook_file["hooks"]["SubagentStart"][0]["hooks"][0]["additionalContextLimit"] == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart", "cwd": str(checkout)})))
    assert main(["_resource-hook"]) == 0
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "HSK_VO_RULE" in output and "src/**/*.java" in output
    assert "SHARED" not in output and "FRONTEND_ONLY" not in output
    assert not (checkout / "AGENTS.md").exists()

    execute("tags", "subscribe", "python")
    path, _ = resource_delivery.installed(checkout)
    assert [item["name"] for item in resources.selected(resource_delivery.read(path))] == ["vo"]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart", "cwd": str(tmp_path)})))
    assert resources.hook() == {}


def test_rule_asset_conflict_keeps_other_existing_files(tmp_path: Path) -> None:
    config = tmp_path / "state" / "workspace.json"
    selected = rule("backend/reference", "Read ${TEAMAI_RULE_DIR}/a.md")
    selected["assets"] = {"a.md": "original-a", "b.md": "original-b"}
    resource_delivery.apply_assets(config, tmp_path, [selected], user=True)
    folder = resource_delivery.asset_root(config, tmp_path, user=True) / "backend" / "reference"
    (folder / "b.md").write_text("local edit", encoding="utf-8")
    updated: Object = {**selected, "assets": {"a.md": "new-a", "b.md": "new-b"}}
    with pytest.raises(resources.CollabError, match="本地文件已修改"):
        resource_delivery.apply_assets(config, tmp_path, [updated], user=True)
    assert (folder / "a.md").read_text(encoding="utf-8") == "original-a"
    assert not resource_delivery.assets_ready(folder.parents[1], selected)
    resource_delivery.apply_assets(config, tmp_path, [], user=True)
    assert not (folder / "a.md").exists()
    assert (folder / "b.md").read_text(encoding="utf-8") == "local edit"


def test_native_rule_matrix_writes_and_removes_managed_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def home(runtime: str) -> Path:
        return tmp_path / "home" / runtime

    monkeypatch.setattr(rule_adapters, "home", home)
    monkeypatch.setattr(rule_install, "home", home)
    monkeypatch.setattr(skill_adapters, "tool_home", home)
    root = tmp_path / "checkout"
    root.mkdir()
    config_path = tmp_path / "state" / "rules.json"
    tools = list(rule_adapters.RULE_TOOLS)
    project_tools = [
        tool for tool in tools if rule_adapters.RULE_TOOLS[tool].project or rule_adapters.RULE_TOOLS[tool].hook
    ]
    source = rule("backend/java", "# 接口规则\n\n说明见 ${TEAMAI_RULE_DIR}/wire.bin。")
    source["path"] = "rules/backend/java/RULE.md"
    binary = bytes([0x00, 0x01, 0xFF, 0x82])
    source["assets"] = {"wire.bin": {"base64": b64encode(binary).decode("ascii")}}
    config: Object = {
        "agents": [*project_tools],
        "snapshot": {
            "files": {
                "rules/backend/java/RULE.md": (
                    "---\npaths: ['src/**/*.java']\n---\n# 接口规则\n\n说明见 ${TEAMAI_RULE_DIR}/wire.bin。"
                )
            }
        },
    }

    resource_delivery.apply_assets(config_path, root, [source], user=False)
    rule_install.apply_native(config_path, root, config, [source], user=False)
    skill_adapters.install_hook("zcode")
    rule_install.install_hooks(project_tools, user=False)
    assert (root / ".weora/delivered/rules/backend/java/wire.bin").read_bytes() == binary
    assert "alwaysApply: false" in (root / ".cursor/rules/backend/java.mdc").read_text(encoding="utf-8")
    assert 'applyTo: "src/**/*.java"' in (root / ".github/instructions/backend/java.instructions.md").read_text(
        encoding="utf-8"
    )
    assert "inclusion: fileMatch" in (root / ".kiro/steering/backend.java.md").read_text(encoding="utf-8")
    assert "description:" in (root / ".omp/rules/backend.java.md").read_text(encoding="utf-8")
    assert json.loads((root / ".opencode/opencode.json").read_text(encoding="utf-8"))["instructions"] == [
        ".opencode/rules/**/*.md"
    ]
    skill_adapters.install_hook("dsh")
    patch = (home("dsh") / "cordis.patch.yml").read_text(encoding="utf-8")
    assert patch.count("id: weora-skills-hooks-claude-code") == 1
    dsh = resource_delivery.read(skill_adapters.dsh_hook_path(home("dsh")))
    assert len(resource_delivery.items({"items": resource_delivery.as_object(dsh["hooks"])["SessionStart"]})) == 2
    zcode = json.loads((home("zcode") / "cli/config.json").read_text(encoding="utf-8"))["hooks"]
    assert zcode["enabled"] and "SessionStart" not in zcode
    assert len(zcode["events"]["SessionStart"]) == 2
    assert "systemPrompt" in (home("pi") / "extensions/weora-rules.ts").read_text(encoding="utf-8")
    for runtime in project_tools:
        descriptor = rule_adapters.RULE_TOOLS[runtime]
        if descriptor.project:
            output = root / descriptor.project / rule_adapters.native_filename(descriptor.format, "backend/java")
            assert "# 接口规则" in output.read_text(encoding="utf-8"), runtime

    edited = root / ".cursor/rules/backend/java.mdc"
    edited.write_text("用户修改", encoding="utf-8")
    rule_install.apply_native(config_path, root, config, [], user=False)
    assert edited.read_text(encoding="utf-8") == "用户修改"
    assert not (root / ".claude/rules/backend/java.md").exists()
    assert json.loads((root / ".opencode/opencode.json").read_text(encoding="utf-8"))["instructions"] == []
    retained = rule_install.retained_rules(config_path, root)
    assert retained == {"backend/java"}
    resource_delivery.apply_assets(config_path, root, [], user=False, retain=retained)
    assert (root / ".weora/delivered/rules/backend/java/wire.bin").read_bytes() == binary

    user_config: Object = {**config, "agents": [*tools]}
    rule_install.apply_native(config_path, tmp_path, user_config, [source], user=True)
    for runtime, descriptor in rule_adapters.RULE_TOOLS.items():
        if descriptor.user:
            dest = home(runtime) / descriptor.user / rule_adapters.native_filename(descriptor.format, "backend/java")
            assert dest.is_file(), runtime
        if target := rule_adapters.inline_file(runtime):
            assert "# 接口规则" in target.read_text(encoding="utf-8"), runtime


def test_import_classifies_and_publishes_three_types_without_existing_tool_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEORA_RESOURCE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(rule_adapters, "home", lambda runtime: tmp_path / runtime)
    folder = tmp_path / "cursor" / "rules"
    folder.mkdir(parents=True)
    for name in ("rule", "doc", "learning", "personal", "managed"):
        (folder / f"{name}.mdc").write_text(f"文档 {name}", encoding="utf-8")
    path, root = resource_delivery.installed(tmp_path, user=True)
    resource_delivery.write_json(path, {"workspace": "fixture", "base": "http://test"})
    resource_delivery.write_json(rule_install.native_ledger(path, root), {"files": {str(folder / "managed.mdc"): {}}})
    calls: list[tuple[str, str, Object]] = []

    def classify(candidates: list[rule_import.Candidate]) -> list[rule_import.Classified]:
        assert {item.file.stem for item in candidates} == {"rule", "doc", "learning", "personal"}
        return [
            classified
            for item in candidates
            if (
                classified := rule_import.classify_reply(
                    item,
                    json.dumps(
                        {
                            "type": item.file.stem,
                            "title": "接口约定",
                            "summary": item.text,
                            "isPersonal": item.file.stem == "personal",
                        }
                    ),
                )
            )
        ]

    def request(_config: Object, method: str, endpoint: str, data: Object) -> Object:
        calls.append((method, endpoint, data))
        return {"revision": "new"}

    monkeypatch.setattr(rule_import, "classify", classify)
    monkeypatch.setattr(resources, "snapshot", lambda *args: {"revision": "old", "files": {}})
    monkeypatch.setattr(resources, "request", request)
    parser = build_parser()
    result = resources.execute(parser.parse_args(["import", "--scope", "user", "--from-tools", "--all"]))
    assert isinstance(result, dict) and result["revision"] == "new"
    assert len(calls) == 1
    method, endpoint, payload = calls[0]
    assert (method, endpoint, payload["base"]) == ("PUT", "/default", "old")
    files = resource_delivery.as_object(payload["files"])
    assert {name.split("/")[0] for name in files} == {"rules", "docs", "learnings"}
    output = tmp_path / "export"
    resources.execute(
        parser.parse_args(["import", "--scope", "user", "--from-tools", "--all", "--output", str(output)])
    )
    assert len(calls) == 1
    assert len(list(output.glob("*.md"))) == 3


def test_opencode_activation_preserves_jsonc_and_personal_entries(tmp_path: Path) -> None:
    target = tmp_path / ".opencode" / "opencode.jsonc"
    target.parent.mkdir()
    target.write_text(
        '{\n  // 模型配置\n  "endpoint": "http://example.test",\n  "instructions": ["personal.md",],\n}\n',
        encoding="utf-8",
    )
    paths = {target.parent / "rules" / "backend" / "java.md"}
    owned = rule_install.opencode_instructions(tmp_path, paths, {}, user=False)
    assert ".opencode/rules/**/*.md" in target.read_text(encoding="utf-8")
    rule_install.opencode_instructions(tmp_path, set(), owned, user=False)
    restored = target.read_text(encoding="utf-8")
    assert '"instructions": ["personal.md"]' in restored
    assert "// 模型配置" in restored
    assert '"endpoint": "http://example.test"' in restored


def test_native_push_keeps_team_frontmatter_and_only_sends_local_body(tmp_path: Path) -> None:
    path = tmp_path / "config" / "workspace.json"
    root = tmp_path / "checkout"
    source = rule("backend/vo", "旧正文")
    team_header = '---\npaths: ["src/**/*.java"]\ndescription: 团队约定\n---'
    snapshot: Object = {
        "files": {"rules/backend/vo.md": f"{team_header}\n\n旧正文\n"},
        "rules": [source],
    }
    config: Object = {"agents": ["cursor"], "snapshot": snapshot}
    rule_install.apply_native(path, root, config, [source], user=False)
    assert resources.scan_local(path, root, config, snapshot, user=False) == []
    native_copy = root / ".cursor/rules/backend/vo.mdc"
    native_copy.write_text("---\nglobs: other\n---\n\n只编辑正文。\n", encoding="utf-8")
    assert resources.scan_local(path, root, config, snapshot, user=False) == [
        ("backend/vo", "rules/backend/vo.md", f"{team_header}\n\n只编辑正文。\n")
    ]
