"""团队规则通过 Weora 服务发布，Codex 启动时使用已同步快照。"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from base64 import b64encode
from contextlib import suppress
from pathlib import Path
from time import time
from urllib.parse import urlencode
from uuid import uuid4

from weora_cli import resource_delivery as delivery
from weora_cli import rule_import, rule_install
from weora_cli.collab_client import CollabError, as_object, items, paired_client, read_config, request_json, write_json
from weora_cli.commands import JsonValue
from weora_cli.resource_delivery import Object
from weora_cli.rule_adapters import RULE_TOOLS, home, render_native, rule_directory, split_markdown, stem
from weora_cli.skill_local import unsafe

COMMANDS = {
    "init",
    "projects",
    "roles",
    "tags",
    "pull",
    "push",
    "list",
    "status",
    "doctor",
    "remove",
    "review",
    "uninstall",
    "recall",
    "import",
}


def arguments(parser: argparse.ArgumentParser, command: str) -> None:
    parser.add_argument("--scope", choices=("workspace", "user"))
    if command == "init":
        parser.add_argument("--agent", "--agents", action="append")
    elif command == "pull":
        parser.add_argument("--team-ref")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--silent", action="store_true")
    elif command in {"projects", "roles", "tags"}:
        actions = ("subscribe", "unsubscribe", "list") if command == "tags" else ("set", "list")
        parser.add_argument("action", choices=actions)
        parser.add_argument("values", nargs="*")
    elif command == "push":
        parser.add_argument("--from-dir", type=Path)
        parser.add_argument("--branch")
        parser.add_argument("--all", action="store_true")
        location = parser.add_mutually_exclusive_group()
        location.add_argument("--project")
        location.add_argument("--role")
        location.add_argument("--shared", action="store_true")
    elif command == "list":
        parser.add_argument("resource", choices=("rules",), nargs="?", default="rules")
    elif command == "recall":
        parser.add_argument("query", nargs="+")
    elif command == "remove":
        parser.add_argument("resource", choices=("rules",))
        parser.add_argument("names", nargs="+")
        parser.add_argument("--force", action="store_true")
    elif command == "review":
        parser.add_argument("--branch", required=True)
        parser.add_argument("--merge", action="store_true")
    elif command == "import":
        parser.add_argument("--from-tools", "--from-claude", action="store_true", required=True)
        parser.add_argument("--all", action="store_true")
        parser.add_argument("--output", type=Path)


def source_identity() -> tuple[str, str]:
    config = read_config()
    return (
        os.environ.get("WEORA_COLLAB_URL", str(config.get("base") or "")).rstrip("/"),
        os.environ.get("WEORA_COLLAB_WORKSPACE", str(config.get("workspace") or "")),
    )


def request(config: Object, method: str, path: str, data: Object | None = None) -> Object:
    client = paired_client()
    if config.get("workspace") != client.workspace or config.get("base") != client.base:
        raise CollabError("RESOURCE_WORKSPACE_CHANGED", "请先登录此检出绑定的业务空间，或重新运行 weora init。")
    return request_json(
        method,
        f"{client.base}/api/v1/workspaces/{client.workspace}/team-resources{path}",
        data,
        token=client.token,
        timeout=client.timeout,
    )


def snapshot(config: Object, ref: str = "main") -> Object:
    return request(config, "GET", "/snapshot?" + urlencode({"ref": ref}))


def active(cwd: Path, scope: str | None) -> tuple[Path, Path, bool]:
    if scope == "user":
        path, root = delivery.installed(cwd, user=True)
        return path, root, True
    try:
        path, root = delivery.installed(cwd)
        if scope == "workspace" or path.exists():
            return path, root, False
    except CollabError:
        if scope == "workspace":
            raise
    path, root = delivery.installed(cwd, user=True)
    return path, root, True


def selected(config: Object) -> list[Object]:
    data = as_object(config.get("snapshot", {}))
    if not data:
        return []
    return [rule for rule, reason in delivery.select_rules(data, config) if reason is None]


def sync(path: Path, root: Path, user: bool, config: Object, ref: str | None) -> Object:
    if ref is not None and user:
        raise CollabError("RESOURCE_PIN_UNAVAILABLE", "固定修订只适用于项目范围。")
    wanted = ref if ref is not None else str(config.get("ref") or "main")
    try:
        current = snapshot(config, wanted)
        if wanted != "main":
            # 分配角色始终来自默认引用，试用候选分支不改变成员身份。
            published = snapshot(config)
            for field in ("member_roles", "members_configured"):
                current[field] = published[field]
    except CollabError as exc:
        if exc.code in {"AGENT_DEVICE_UNAUTHORIZED", "TEAM_RESOURCE_FORBIDDEN"}:
            write_json(path, {**config, "snapshot": {}})
            rule_install.apply_native(path, root, config, [], user=user)
            delivery.apply_assets(path, root, [], user=user, retain=rule_install.retained_rules(path, root))
        raise
    updated: Object = {**config, "ref": wanted, "snapshot": current, "synced_at": int(time())}
    if wanted == "main" and config.get("pending_removals"):
        updates = as_object(current["files"])
        updated["pending_removals"] = [name for name in delivery.values(config["pending_removals"]) if name in updates]
    desired = selected(updated)
    delivery.apply_assets(path, root, desired, user=user, retain=rule_install.retained_rules(path, root))
    rule_install.apply_native(path, root, updated, desired, user=user)
    rule_install.install_hooks(rule_install.agents(updated), user=user)
    write_json(path, updated)
    return {
        "revision": current["revision"],
        "ref": wanted,
        "scope": "user" if user else "workspace",
        "delivered": [rule["name"] for rule in desired],
    }


def list_selection(args: argparse.Namespace, path: Path, config: Object) -> Object:
    current = snapshot(config)
    kind = args.operation
    if args.action == "list":
        if kind == "tags":
            return {"subscribed": list[JsonValue](delivery.values(config.get("tags", [])))}
        active_ids = (
            list[JsonValue](delivery.effective_roles(current, config))
            if kind == "roles"
            else config.get("projects", [])
        )
        return {"items": current[kind], "active": active_ids}
    if kind == "roles" and current.get("members_configured"):
        raise CollabError("RESOURCE_ROLES_ASSIGNED", "当前团队从 manifest/members.yaml 分配成员角色。")
    requested = list(dict.fromkeys(value for item in args.values for value in item.split(",") if value))
    if kind == "tags":
        tags = delivery.values(config.get("tags", []))
        requested = (
            list(dict.fromkeys(tags + requested))
            if args.action == "subscribe"
            else [tag for tag in tags if tag not in requested]
        )
    else:
        known = {item["id"] for item in delivery.project_rows(current, kind)}
        if any(value not in known for value in requested):
            raise CollabError("RESOURCE_SCOPE_UNKNOWN", "团队源中找不到选择的项目或角色。")
    write_json(path, {**config, kind: requested, "synced_at": 0})
    return {"active": requested}


def read_source(directory: Path) -> dict[str, JsonValue]:
    if not directory.is_dir() or unsafe(directory):
        raise CollabError("RESOURCE_SOURCE_INVALID", "找不到可读取的团队规则源目录。")
    files: dict[str, JsonValue] = {}
    for folder, subdirs, names in os.walk(directory, followlinks=False):
        location = Path(folder)
        subdirs[:] = [name for name in subdirs if not name.startswith(".") and not unsafe(location / name)]
        for filename in names:
            file = location / filename
            relative = file.relative_to(directory).as_posix()
            if unsafe(file) or filename.startswith("."):
                continue
            if not (
                relative.startswith(("rules/", "manifest/", "docs/", "learnings/"))
                or relative in {"teamai.yaml", "tags.yaml"}
            ):
                continue
            if len(files) >= 512 or file.stat().st_size > 2 * 1024 * 1024:
                raise CollabError("RESOURCE_TREE_LIMIT", "团队源超出文件数量或大小限制。")
            contents = file.read_bytes()
            try:
                data: JsonValue = contents.decode("utf-8")
            except UnicodeError as exc:
                if not relative.startswith("rules/"):
                    raise CollabError(
                        "RESOURCE_SOURCE_INVALID", f"规则和团队配置必须是 UTF-8 文本：{relative}。"
                    ) from exc
                data = {"base64": b64encode(contents).decode("ascii")}
            files[relative] = data
    return files


def publish(
    path: Path,
    config: Object,
    current: Object,
    updates: Object,
    branch: str | None = None,
    *,
    remember: bool = True,
) -> Object:
    if str(config.get("ref") or "main") != "main":
        raise CollabError("RESOURCE_PINNED", "固定修订期间不能提交。先执行 weora pull --team-ref main。")
    open_changes = items(request(config, "GET", "/changes"))
    remembered = config.get("branch")
    pending = next((item for item in open_changes if item["branch"] == (branch or remembered)), None)
    candidate = branch or str(pending["branch"] if pending else f"weora/push/{uuid4().hex[:12]}")
    if remember:
        write_json(path, {**config, "branch": candidate})
    return request(
        config,
        "POST",
        "/changes",
        {
            "branch": candidate,
            "base": current["revision"],
            "expected": pending["revision"] if pending else None,
            "files": updates,
        },
    )


def push_namespace(args: argparse.Namespace, current: Object, config: Object) -> str | None:
    if args.shared:
        return None
    if args.project or args.role:
        kind, target = ("projects", args.project) if args.project else ("roles", args.role)
        matching = [row for row in delivery.project_rows(current, kind) if row["id"] == target]
        if not matching:
            raise CollabError("RESOURCE_SCOPE_UNKNOWN", "团队源中没有该项目或角色。")
        candidates = delivery.values(matching[0]["knowledge"])
        if not candidates:
            raise CollabError("RESOURCE_SCOPE_UNKNOWN", "目标未声明 knowledge 命名空间。")
    else:
        ids = {
            "projects": delivery.values(config.get("projects", [])),
            "roles": delivery.effective_roles(current, config),
        }
        candidates = [
            namespace
            for kind, active_ids in ids.items()
            for row in delivery.project_rows(current, kind)
            if row["id"] in active_ids
            for namespace in delivery.values(row["knowledge"])
        ]
    distinct = list(dict.fromkeys(candidates))
    if not distinct:
        return None
    if len(distinct) == 1:
        return distinct[0]
    if not sys.stdin.isatty():
        raise CollabError("RESOURCE_SCOPE_AMBIGUOUS", "有多个可用知识范围，请明确选择项目、角色或 --shared。")
    print("发布命名空间：", *distinct, "shared（共享根目录）", sep="\n", file=sys.stderr)
    chosen = input("输入命名空间：").strip()
    if chosen != "shared" and chosen not in distinct:
        raise CollabError("RESOURCE_SCOPE_UNKNOWN", "请选择列出的命名空间。")
    return chosen if chosen != "shared" else None


def check_native_edit_base(
    source_path: str,
    delivered: Object,
    team_text: str,
    team_rule: Object | None,
    format_id: str,
    assets: Path,
) -> None:
    """确认本地编辑仍以团队当前文件为基线，支持旧版投递记录。"""
    expected_hash = delivered.get("source_hash")
    if isinstance(expected_hash, str):
        baseline_matches = expected_hash == hashlib.sha256(team_text.encode("utf-8")).hexdigest()
    else:
        # 旧账本只有工具副本的摘要，用当前团队内容重新渲染后比较。
        baseline_matches = (
            team_rule is not None
            and delivered.get("hash")
            == hashlib.sha256(render_native(format_id, team_rule, team_text, assets).encode("utf-8")).hexdigest()
        )
    if not baseline_matches:
        raise CollabError(
            "RESOURCE_EDIT_BASE_CHANGED",
            f"团队规则已更新：{source_path}。请对照最新团队版本合并本地编辑后重新提交。",
        )


def scan_local(
    path: Path, root: Path, config: Object, current: Object, *, user: bool
) -> list[tuple[str, str | None, str]]:
    """扫描当前检出的本地规则，核对投递基线后返回可发布的修改。"""
    # 读取此检出的账本。其他 worktree 刷新的共享快照不能替换编辑基线。
    tracked = as_object(delivery.read(rule_install.native_ledger(path, root)).get("files", {}))
    placements = as_object(config.get("placements", {}))
    files = as_object(current["files"])
    team_rules = {str(rule["name"]): rule for rule in delivery.project_rows(current, "rules")}
    folder_names = {name for name, rule in team_rules.items() if str(rule["path"]).endswith("/RULE.md")}
    pending_removals = set(delivery.values(config.get("pending_removals", [])))
    assets = delivery.asset_root(path, root, user=user)
    sources = {
        directory: RULE_TOOLS[runtime].format
        for runtime in ["claude", *rule_install.agents(config)]
        if (directory := rule_directory(runtime, None if user else root)) is not None
    }
    # 按团队源路径收集编辑，转换格式的副本只回写正文。
    changes: list[tuple[str, str | None, str]] = []
    for directory, format_id in sources.items():
        for file in rule_import.rule_files(directory, limit=2 * 1024 * 1024):
            native_stem = stem(file.relative_to(directory).as_posix())
            previous = as_object(tracked.get(str(file), {}))
            raw = file.read_bytes()
            if previous.get("hash") == hashlib.sha256(raw).hexdigest():
                continue
            name = str(previous.get("name") or native_stem)
            if name in {"teamai-recall", "teamai-context"} or name.endswith(".draft"):
                continue
            if any(name == folder or name.startswith(folder + "/") for folder in folder_names):
                continue
            team_rule = team_rules.get(name)
            owned_source = previous.get("source")
            placed_source = str(placements.get(name) or "")
            target = str(team_rule["path"]) if team_rule else None
            if placed_source in files:
                target = placed_source
            if isinstance(owned_source, str):
                target = owned_source
            elif previous and target is None:
                target = placed_source or f"rules/{name}.md"
            if target is not None and target in pending_removals:
                continue
            team_text = files.get(target) if target else None
            if previous and not isinstance(team_text, str):
                raise CollabError("RESOURCE_EDIT_BASE_CHANGED", f"本地副本对应的团队文件已移除或迁移：{target}。")

            try:
                text = raw.decode("utf-8")
            except UnicodeError as exc:
                raise CollabError("RESOURCE_SOURCE_INVALID", f"规则必须是 UTF-8 文本：{file.name}。") from exc
            if format_id != "verbatim":
                if not isinstance(team_text, str):
                    # 私有原生规则需要经导入分类，不能作为新团队规则直接回推。
                    continue
                header, team_body = split_markdown(team_text)
                _, body = split_markdown(text)
                if body == team_body:
                    continue
                text = (header.rstrip() + "\n\n" if header else "") + body + "\n"
            if team_text == text:
                continue
            if previous and target is not None and isinstance(team_text, str):
                check_native_edit_base(target, previous, team_text, team_rule, format_id, assets)
            changes.append((name, target, text))
    return changes


def push(args: argparse.Namespace, path: Path, root: Path, config: Object, *, user: bool) -> Object:
    """准备待发布路径，核对编辑基线和删除状态，再创建或更新候选分支。"""
    # 只用最新发布修订承接已通过基线检查的变更，保留其他文件的并发更新。
    current = snapshot(config)
    placements = as_object(config.get("placements", {})).copy()
    if args.from_dir:
        updates = read_source(args.from_dir.resolve())
    else:
        changes = scan_local(path, root, config, current, user=user)
        namespace = push_namespace(args, current, config) if any(target is None for _, target, _ in changes) else None
        updates = {}
        for name, target, body in changes:
            if target is None:
                prefix = f"{namespace}/" if namespace and "/" not in name else ""
                target = f"rules/{prefix}{name}.md"
                if prefix:
                    placements[name] = target
            if target in updates and updates[target] != body:
                raise CollabError("RESOURCE_SOURCE_CONFLICT", f"多个本地工具修改了同一条规则：{target}。")
            updates[target] = body
    # 过滤本机已经发起删除的内容，避免目录仍留有副本时被重新发布。
    main = as_object(current["files"])
    pending_removals = delivery.values(config.get("pending_removals", []))
    for name in [*updates]:
        if name in pending_removals or any(
            removed.endswith("/RULE.md") and name.startswith(removed.removesuffix("RULE.md"))
            for removed in pending_removals
        ):
            updates.pop(name)
    if args.from_dir and (last_sync := as_object(config.get("snapshot", {}))):
        last_files = as_object(last_sync["files"])
        conflicts = sorted(
            name for name, value in updates.items() if main.get(name) not in (last_files.get(name), value)
        )
        if conflicts:
            raise CollabError(
                "RESOURCE_EDIT_BASE_CHANGED",
                f"团队源已更新：{', '.join(conflicts)}。请对照最新团队版本合并本地编辑后重新提交。",
            )
    if not args.branch and not config.get("branch"):
        updates = {key: value for key, value in updates.items() if main.get(key) != value}
    if not updates:
        return {"status": "unchanged"}

    # 先确认变更，再记录候选分支和新规则的放置位置。
    if not args.all:
        if not sys.stdin.isatty():
            raise CollabError("RESOURCE_CONFIRMATION_REQUIRED", "非交互环境提交规则需要传入 --all。")
        print("将提交以下团队源文件：", *sorted(updates), sep="\n", file=sys.stderr)
        if input("创建合并请求？[y/N] ").strip().lower() != "y":
            return {"status": "cancelled"}
    change = publish(path, config, current, updates, args.branch)
    if placements != config.get("placements", {}):
        write_json(path, {**delivery.read(path), "placements": placements})
    return change


def import_rules(args: argparse.Namespace, path: Path, config: Object) -> Object:
    if not args.output and config.get("ref") not in {None, "main"}:
        raise CollabError("RESOURCE_PINNED", "固定修订期间不能导入到团队源。")
    if not args.all and not sys.stdin.isatty():
        raise CollabError("RESOURCE_CONFIRMATION_REQUIRED", "非交互导入需要传入 --all。")
    user_path, user_root = delivery.installed(Path.cwd(), user=True)
    owned = as_object(delivery.read(rule_install.native_ledger(user_path, user_root)).get("files", {}))
    candidates = rule_import.scan(owned)
    if not candidates:
        return {"written": []}
    accepted = rule_import.review(rule_import.classify(candidates), accept_all=args.all)
    if args.output:
        return {"written": list[JsonValue](rule_import.write_local(args.output.absolute(), accepted))}
    current = snapshot(config) if accepted else {}
    files = rule_import.documents(accepted, set(as_object(current.get("files", {}))), by_type=True)
    if not files:
        return {"written": []}
    committed = request(config, "PUT", "/default", {"base": current["revision"], "files": dict[str, JsonValue](files)})
    return {"revision": committed["revision"], "written": list[JsonValue](files)}


def remove(args: argparse.Namespace, path: Path, root: Path, config: Object, *, user: bool) -> Object:
    current = snapshot(config)
    files = as_object(current["files"])
    wanted = set(args.names)
    found = [rule for rule in delivery.project_rows(current, "rules") if rule["name"] in wanted]
    if {rule["name"] for rule in found} != wanted:
        raise CollabError("RESOURCE_RULE_NOT_FOUND", "找不到要删除的规则，请使用 list rules 查看完整名称。")
    updates: Object = {}
    for rule in found:
        rule_path = str(rule["path"])
        if rule_path.endswith("/RULE.md"):
            updates.update({key: None for key in files if key.startswith(rule_path.removesuffix("RULE.md"))})
        else:
            updates[rule_path] = None
    if not args.force and (not sys.stdin.isatty() or input("提交规则删除请求？[y/N] ").strip().lower() != "y"):
        return {"status": "cancelled"}
    deletion = json.dumps([current["revision"], sorted(wanted), str(path.resolve())], ensure_ascii=False)
    branch = "weora/remove/" + hashlib.sha256(deletion.encode()).hexdigest()[:16]
    change = publish(path, config, current, updates, branch, remember=False)
    # 本机先停止下发待删除的规则，其他成员仍以团队默认引用为准。
    marked = list(
        dict.fromkeys([*delivery.values(config.get("pending_removals", [])), *(str(rule["path"]) for rule in found)])
    )
    modified: Object = {**config, "pending_removals": list[JsonValue](marked)}
    write_json(path, modified)
    current_rules = selected(modified)
    rule_install.apply_native(path, root, modified, current_rules, user=user)
    delivery.apply_assets(path, root, current_rules, user=user, retain=rule_install.retained_rules(path, root))
    return change


def uninstall(path: Path, root: Path, config: Object, *, user: bool) -> Object:
    """清理所有属于这次安装的副本，全部完成后解除共享绑定，失败时保留重试入口。"""
    # Git 共用一份绑定，各检出只有存在安装账本时才需要清理。
    affected = [root]
    if not user:
        for checkout in delivery.worktree_roots(root):
            if checkout == root:
                continue
            delivered = (
                rule_install.native_ledger(path, checkout).exists() or delivery.worktree_ledger(path, checkout).exists()
            )
            if not delivered:
                continue
            try:
                checkout_config, _ = delivery.installed(checkout)
            except CollabError as exc:
                raise CollabError("RESOURCE_WORKTREE_UNAVAILABLE", f"无法读取已投递的检出：{checkout}。") from exc
            if checkout_config != path:
                raise CollabError("RESOURCE_WORKTREE_UNAVAILABLE", f"原检出已不属于此 Git 仓库：{checkout}。")
            affected.append(checkout)

    # 保留个人编辑及其附件，所有已管理投递清理后才能移除共享配置。
    for checkout in affected:
        rule_install.apply_native(path, checkout, config, [], user=user)
        retained = rule_install.retained_rules(path, checkout)
        delivery.apply_assets(path, checkout, [], user=user, retain=retained)
    path.unlink()
    return {"status": "uninstalled"}


def execute(args: argparse.Namespace) -> JsonValue:
    command = args.operation
    cwd = Path.cwd()
    if command == "init":
        user = args.scope == "user"
        path, _ = delivery.installed(cwd, user=user)
        client = paired_client()
        with delivery.state_lock():
            previous = delivery.read(path)
            if previous and (previous.get("workspace") != client.workspace or previous.get("base") != client.base):
                raise CollabError("RESOURCE_WORKSPACE_CHANGED", "此范围已经绑定其他团队源，请先解除原安装。")
            options = args.agent or (rule_install.agents(previous) if previous else ["codex"])
            agents = list(dict.fromkeys(item.strip() for option in options for item in option.split(",")))
            if not agents or any(tool not in RULE_TOOLS for tool in agents):
                raise CollabError("RESOURCE_RUNTIME_UNKNOWN", f"可用工具：{', '.join(sorted(RULE_TOOLS))}。")
            if not user and any(RULE_TOOLS[tool].project is None and RULE_TOOLS[tool].hook is None for tool in agents):
                raise CollabError("RESOURCE_SCOPE_UNSUPPORTED", "Hermes 和 OpenClaw 的规则仅支持 --scope user。")
            rule_install.install_hooks(agents, user=user)
            write_json(path, {**previous, "base": client.base, "workspace": client.workspace, "agents": [*agents]})
        return {"workspace": client.workspace, "scope": "user" if user else "workspace"}
    path, root, user = active(cwd, args.scope)
    with delivery.state_lock():
        config = delivery.read(path)
        if not config:
            raise CollabError("RESOURCE_NOT_INITIALIZED", "请先运行 weora init --agent codex。")
        if command == "pull":
            return sync(path, root, user, config, args.team_ref)
        if command in {"projects", "roles", "tags"}:
            return list_selection(args, path, config)
        if command == "push":
            return push(args, path, root, config, user=user)
        if command == "import":
            return import_rules(args, path, config)
        if command == "remove":
            return remove(args, path, root, config, user=user)
        if command == "review":
            changes = items(request(config, "GET", "/changes"))
            change = next((row for row in changes if row["branch"] == args.branch), None)
            if change is None:
                raise CollabError("RESOURCE_CHANGE_NOT_FOUND", "找不到该分支的待合并请求。")
            identifier = str(change["id"])
            if args.merge:
                return request(config, "POST", f"/changes/{identifier}/merge", {"expected": change["revision"]})
            return request(config, "GET", f"/changes/{identifier}")
        if command == "uninstall":
            return uninstall(path, root, config, user=user)
        if command == "list":
            return {"items": snapshot(config, str(config.get("ref") or "main"))["rules"]}
        if command == "recall":
            words = " ".join(args.query).casefold().split()
            matches = [
                rule
                for rule in selected(config)
                if all(word in (f"{rule['name']} {rule['description']} {rule['body']}".casefold()) for word in words)
            ]
            return {"items": list[JsonValue](matches[:20])}
        current = as_object(config.get("snapshot", {}))
        evaluated: list[JsonValue] = (
            [
                {"name": rule["name"], "path": rule["path"], "excluded": reason}
                for rule, reason in delivery.select_rules(current, config)
            ]
            if current
            else []
        )
        result: Object = {
            "revision": current.get("revision"),
            "ref": config.get("ref") or "main",
            "scope": "user" if user else "workspace",
            "items": evaluated,
        }
        if command == "doctor":
            agents = rule_install.agents(config)
            result["checks"] = {
                "synced": bool(current),
                "codex_hooks": delivery.hook_checks(home("codex") / "hooks.json", delivery.HOOK)
                if not user and "codex" in agents
                else None,
                "codex_user_override": (home("codex") / "AGENTS.override.md").exists()
                if user and "codex" in agents
                else None,
                "agents": [*agents],
            }
        return result


def background_pull(root: Path) -> None:
    # 独立进程刷新远端，启动钩子即时返回已验证的本地规则正文。
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from weora_cli.cli import main; raise SystemExit(main())",
            "pull",
            "--scope",
            "workspace",
            "--silent",
        ],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def hook(runtime: str = "codex") -> Object:
    try:
        payload = as_object(json.loads(sys.stdin.read(1024 * 1024)))
        event = payload.get("hook_event_name", "SessionStart")
        cwd = payload.get("cwd")
        roots = payload.get("workspace_roots")
        if (not isinstance(cwd, str) or not cwd.strip()) and isinstance(roots, list):
            cwd = next((item for item in roots if isinstance(item, str) and item.strip()), None)
        if (
            event not in {"SessionStart", "SubagentStart", "sessionStart"}
            or not isinstance(cwd, str)
            or payload.get("source") == "resume"
        ):
            return {}
        cwd = cwd.strip()
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", cwd):
            cwd = cwd[1:]
        user = runtime == "cursor"
        if user and Path(cwd).resolve().is_relative_to(Path.home().resolve()):
            return {}
        path, root = delivery.installed(Path(cwd), user=user)
        config = delivery.read(path)
        if (
            not config
            or runtime not in rule_install.agents(config)
            or (config.get("base"), config.get("workspace")) != source_identity()
        ):
            return {}
        desired = selected(config)
        directory = delivery.asset_root(path, root, user=user)
        ready = [rule for rule in desired if delivery.assets_ready(directory, rule)]
        text = delivery.render(ready, directory)
        if user and len(text.encode("utf-16-le")) // 2 > 10_000:
            # Cursor 超过原生上下文上限会丢弃整份钩子回复。
            return {}
        synced_at = config.get("synced_at")
        if (
            not user
            and event == "SessionStart"
            and (len(ready) != len(desired) or not isinstance(synced_at, int) or time() - synced_at > 60)
        ):
            with suppress(OSError):
                background_pull(root)
        if not text:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart" if event == "sessionStart" else event,
                "additionalContext": "Team rules (from Weora):\n\n" + text,
            }
        }
    except (CollabError, KeyError, OSError, ValueError, TypeError):
        # 本机无规则或配置异常时，让 Codex 继续启动。
        return {}
