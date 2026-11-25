"""从本地团队源快照解析 Codex 规则并管理已写入的内容。"""

import hashlib
import json
import os
import sqlite3
import subprocess
from base64 import b64decode
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import cast

from weora_cli.collab_client import CollabError, as_object, items, write_json
from weora_cli.commands import JsonValue
from weora_cli.skill_local import unsafe

type Object = dict[str, JsonValue]
START = "<!-- [weora:team-rules:start] -->"
END = "<!-- [weora:team-rules:end] -->"
HOOK = "weora _resource-hook"


def state_root() -> Path:
    return Path(os.environ.get("WEORA_RESOURCE_HOME", str(Path.home() / ".weora" / "team-resources")))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()


def read(path: Path) -> Object:
    try:
        return as_object(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise CollabError("RESOURCE_STATE_INVALID", f"无法读取 {path.name}。") from exc


@contextmanager
def state_lock() -> Iterator[None]:
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "sync.sqlite3", timeout=16)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except sqlite3.OperationalError as exc:
        raise CollabError("RESOURCE_STATE_BUSY", "本机团队规则正在同步，请稍后重试。") from exc
    finally:
        connection.close()


def git_path(cwd: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", *arguments],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.rstrip("\r\n") if result.returncode == 0 else None


def installed(cwd: Path, *, user: bool = False) -> tuple[Path, Path]:
    if user:
        return state_root() / "user.json", codex_home()
    workspace = git_path(cwd, "--show-toplevel")
    anchor = git_path(cwd, "--git-common-dir")
    if not workspace or not anchor:
        raise CollabError("RESOURCE_WORKSPACE_REQUIRED", "请在需要使用规则的 Git 仓库中运行该命令。")
    # Linked worktree 共用安装和项目选择，各 worktree 单独记录投递文件。
    key = hashlib.sha256(os.path.normcase(str(Path(anchor).resolve())).encode()).hexdigest()
    return state_root() / "workspaces" / f"{key}.json", Path(workspace).resolve()


def values(value: JsonValue) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CollabError("RESOURCE_STATE_INVALID", "规则范围的数据格式无效。")
    return cast(list[str], value)


def project_rows(snapshot: Object, kind: str) -> list[Object]:
    return items({"items": snapshot[kind]})


def effective_roles(snapshot: Object, config: Object) -> list[str]:
    return (
        values(snapshot.get("member_roles", []))
        if snapshot.get("members_configured")
        else values(config.get("roles", []))
    )


def select_rules(snapshot: Object, config: Object) -> list[tuple[Object, str | None]]:
    role_ids = set(effective_roles(snapshot, config))
    project_ids = set(values(config.get("projects", [])))
    projects = project_rows(snapshot, "projects")
    namespaces = {
        namespace
        for kind, active in (("roles", role_ids), ("projects", project_ids))
        for area in project_rows(snapshot, kind)
        if area["id"] in active
        for namespace in values(area["knowledge"])
    }
    scoped = bool(role_ids or project_ids or projects or snapshot.get("members_configured"))
    tags = set(values(config.get("tags", [])))
    removed = set(values(config.get("pending_removals", [])))
    filtered: list[tuple[Object, str | None]] = []
    for rule in project_rows(snapshot, "rules"):
        reason = None
        if rule["path"] in removed:
            reason = "pending_removal"
        elif scoped and rule["namespace"] is not None and rule["namespace"] not in namespaces:
            reason = "namespace"
        elif tags and (rule_tags := set(values(rule["tags"]))) and not rule_tags.intersection(tags):
            reason = "tags"
        filtered.append((rule, reason))
    # 只有实际通过标签筛选的一层命名空间规则才覆盖共享同名规则。
    overridden = {
        str(rule["name"]).split("/")[1]
        for rule, reason in filtered
        if scoped and reason is None and str(rule["name"]).count("/") == 1
    }
    return [
        (rule, reason or ("replaced" if rule["namespace"] is None and rule["name"] in overridden else None))
        for rule, reason in filtered
    ]


def write_text(path: Path, content: str) -> None:
    write_bytes(path, content.encode("utf-8"))


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as handle:
        handle.write(content)
        staged = Path(handle.name)
    try:
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def file_bytes(content: JsonValue) -> bytes:
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, dict) and isinstance(encoded := content.get("base64"), str):
        try:
            return b64decode(encoded, validate=True)
        except ValueError as exc:
            raise CollabError("RESOURCE_STATE_INVALID", "规则附件的二进制内容无效。") from exc
    raise CollabError("RESOURCE_STATE_INVALID", "规则附件的格式无效。")


def safe_child(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"/", "..", "."} or ":" in part or "\\" in part for part in parts):
        raise CollabError("INVALID_RESOURCE_PATH", "规则辅助文件路径无效。")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()) or any(
        unsafe(p) for p in (path, *path.parents) if p != root.parent
    ):
        raise CollabError("INVALID_RESOURCE_PATH", "规则辅助文件不能穿过链接。")
    return path


def asset_root(config_file: Path, root: Path, *, user: bool) -> Path:
    return config_file.parent / "rules" if user else root / ".weora" / "delivered" / "rules"


def worktree_ledger(config_file: Path, root: Path) -> Path:
    key = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
    return config_file.parent / "delivery" / f"{key}.json"


def exclude_generated(root: Path, patterns: set[str]) -> None:
    if not patterns or not (ignore := git_path(root, "--git-path", "info/exclude")):
        return
    file = Path(ignore)
    safe_child(file.parent, file.name)
    existing = file.read_text(encoding="utf-8") if file.exists() else ""
    missing = patterns.difference(existing.splitlines())
    if missing:
        write_text(file, existing.rstrip("\r\n") + "\n" + "\n".join(sorted(missing)) + "\n")


def apply_assets(
    config_file: Path, root: Path, selected: list[Object], *, user: bool, retain: set[str] | None = None
) -> None:
    directory = asset_root(config_file, root, user=user)
    ledger_file = worktree_ledger(config_file, root)
    old = read(ledger_file)
    desired = {
        f"{rule['name']}/{name}": file_bytes(data)
        for rule in selected
        for name, data in as_object(rule["assets"]).items()
    }
    paths = {relative: safe_child(directory, relative) for relative in old.keys() | desired.keys()}
    tracked: Object = {relative: hashlib.sha256(body).hexdigest() for relative, body in desired.items()}
    # 先检查所有冲突，避免其中一个附件被手工修改时只应用半套规则。
    observed: dict[str, str] = {}
    for relative, path in paths.items():
        if path.is_file():
            observed[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        if (
            relative in desired
            and path.exists()
            and (relative not in observed or observed[relative] not in (old.get(relative), tracked[relative]))
        ):
            raise CollabError("RESOURCE_FILE_CONFLICT", f"本地文件已修改：{relative}。")
    if desired and not user:
        exclude_generated(root, {"/.weora/delivered/rules/"})
    for relative, body in desired.items():
        if observed.get(relative) != tracked[relative]:
            write_bytes(paths[relative], body)
        elif relative not in old:
            # 可以引用内容相同的本地文件，但清理时只删除 Weora 安装过的文件。
            del tracked[relative]
    for relative in old.keys() - tracked.keys():
        if relative in observed and observed[relative] == old[relative]:
            if any(relative.startswith(name + "/") for name in retain or ()):
                tracked[relative] = old[relative]
            else:
                paths[relative].unlink()
    write_json(ledger_file, tracked)


def assets_ready(directory: Path, rule: Object) -> bool:
    try:
        return all(
            safe_child(directory, f"{rule['name']}/{relative}").read_bytes() == file_bytes(body)
            for relative, body in as_object(rule["assets"]).items()
        )
    except (CollabError, OSError):
        return False


def render(selected: list[Object], directory: Path | str) -> str:
    blocks = []
    for rule in selected:
        body = str(rule["body"]).strip().replace(START, "").replace(END, "")
        if body:
            paths = values(rule["paths"])
            scope = f"Applies to files matching: {', '.join(paths)}\n" if paths else ""
            blocks.append(scope + body.replace("${TEAMAI_RULE_DIR}", f"{Path(directory).as_posix()}/{rule['name']}"))
    return "\n\n".join(blocks)


def write_user_block(text: str, target: Path) -> None:
    safe_child(target.parent, target.name)
    existing = target.read_bytes().decode("utf-8") if target.exists() else ""
    before, marker, remainder = existing.partition(START)
    if marker:
        _, close, after = remainder.partition(END)
        if not close or START in after:
            raise CollabError("RESOURCE_FILE_CONFLICT", f"{target.name} 中的 Weora 管理标记无效。")
        updated = (
            before + f"{START}\n\n{text}\n\n{END}" + after
            if text
            else before.removesuffix("\n\n") + after.removeprefix("\n")
        )
    else:
        updated = existing if not text else existing + ("\n\n" if existing else "") + f"{START}\n\n{text}\n\n{END}\n"
    if updated != existing:
        write_text(target, updated)


def hook_present(groups: JsonValue, command: str = HOOK) -> bool:
    return isinstance(groups, list) and any(
        isinstance(group, dict)
        and not group.get("matcher")
        and isinstance(handlers := group.get("hooks"), list)
        and any(
            isinstance(item, dict)
            and item.get("type") == "command"
            and item.get("command") == command
            and item.get("additionalContextLimit") == 0
            for item in handlers
        )
        for group in groups
    )


def hook_checks(target: Path, command: str) -> Object:
    hooks = as_object(read(target).get("hooks", {}))
    return {event: hook_present(hooks.get(event), command) for event in ("SessionStart", "SubagentStart")}


def install_hook(target: Path, command: str) -> None:
    safe_child(target.parent, target.name)
    record = read(target)
    hooks = as_object(record.get("hooks", {}))
    changed = False
    for event in ("SessionStart", "SubagentStart"):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise CollabError("RESOURCE_HOOK_INVALID", "Codex 启动钩子的格式无效。")
        if hook_present(groups, command):
            continue
        entry: Object = {"type": "command", "command": command, "additionalContextLimit": 0, "timeout": 15}
        updated: list[JsonValue] = []
        for group in groups:
            if isinstance(group, dict) and isinstance(handlers := group.get("hooks"), list):
                remaining = [
                    handler
                    for handler in handlers
                    if not isinstance(handler, dict) or handler.get("command") != command
                ]
                if remaining:
                    updated.append({**group, "hooks": remaining})
            else:
                updated.append(group)
        hooks[event] = [*updated, {"hooks": [entry]}]
        changed = True
    if changed:
        write_json(target, {**record, "hooks": hooks})
