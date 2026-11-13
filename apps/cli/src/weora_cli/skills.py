"""TeamAI 风格的 Skill 配对绑定、同步与本地事件上报。"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from uuid import UUID, uuid4

import keyring

from weora_cli.collab_client import (
    SERVICE,
    Client,
    CollabError,
    _NoRedirect,
    as_object,
    items,
    paired_client,
    read_config,
)
from weora_cli.commands import JsonValue
from weora_cli.skill_adapters import TOOLS, USER_ONLY, destination, install_hook, launch, skill_roots, tool_home
from weora_cli.skill_local import change_files, conflict, database, inventory, now, queue, recover, temporary_package

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_DEVICE_REVOKED = {"SKILL_DEVICE_NOT_AUTHORIZED", "AGENT_DEVICE_UNAUTHORIZED"}


class SkillClient(Client):
    @property
    def path(self) -> str:
        return f"{self.base.rstrip('/')}/api/v1/workspaces/{self.workspace}/skill-distribution"

    def download(self, version_id: str, expected: str, output: Path) -> None:
        version = UUID(version_id)
        request = Request(self.path + f"/versions/{version}/package", headers={"Authorization": f"Bearer {self.token}"})
        checksum = hashlib.sha256()
        size = 0
        try:
            with build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response, output.open("xb") as file:
                while chunk := response.read(128 * 1024):
                    size += len(chunk)
                    if size > 50 * 1024**2:
                        raise CollabError("SKILL_PACKAGE_TOO_LARGE", "Skill 包超过本机可安装的上限。")
                    file.write(chunk)
                    checksum.update(chunk)
        except HTTPError as exc:
            raise CollabError(f"HTTP_{exc.code}", "Skill 包下载失败。") from exc
        except (URLError, TimeoutError) as exc:
            raise CollabError("WEORA_UNREACHABLE", "连接 Weora 失败。") from exc
        if not size or checksum.hexdigest() != expected:
            raise CollabError("SKILL_PACKAGE_CHECKSUM", "下载的 Skill 包摘要不符。")


def project_root(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=4,
        check=False,
    )
    if result.returncode:
        raise CollabError("SKILL_PROJECT_REQUIRED", "请选择 Git 项目目录，或使用 --scope user。")
    return Path(os.fsdecode(result.stdout.strip())).resolve()


def is_current_binding(binding: sqlite3.Row, config: dict[str, JsonValue]) -> bool:
    """Skill 文件和队列会留在本机，只有当前登录设备可使用对应绑定。"""
    return (binding["base"], binding["workspace"], binding["device"]) == (
        config.get("base"), config.get("workspace"), config.get("device_id")
    )


def client_for(binding: sqlite3.Row) -> SkillClient:
    if not is_current_binding(binding, read_config()):
        raise CollabError("SKILL_DEVICE_NOT_PAIRED", "请登录后重新运行 weora skill bind 绑定此范围。")

    base, workspace, device = binding["base"], binding["workspace"], binding["device"]
    override = os.environ.get("WEORA_COLLAB_TOKEN")
    if override and (base, workspace) == (
        os.environ.get("WEORA_COLLAB_URL", base).rstrip("/"),
        os.environ.get("WEORA_COLLAB_WORKSPACE", workspace),
    ):
        return SkillClient(base, workspace, override)

    token = keyring.get_password(SERVICE, f"{base}/{workspace}/{device}")
    if not token:
        raise CollabError("SKILL_DEVICE_NOT_PAIRED", "系统钥匙串找不到绑定设备，请重新配对。")
    return SkillClient(base, workspace, token)


def lookup(db: sqlite3.Connection, runtime: str, scope: str, root: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM binding WHERE root=? AND runtime=? AND scope=? AND profile=?",
        (root, runtime, scope, str(tool_home(runtime))),
    ).fetchone()
    if row is None:
        raise CollabError("SKILL_NOT_BOUND", "请先运行 weora skill bind 绑定此安装范围。")
    return cast(sqlite3.Row, row)


def _manifest(client: SkillClient, runtime: str, scope: str) -> dict[str, JsonValue]:
    return client.request("GET", "/manifest?" + urlencode({"runtime": runtime, "scope": scope}))


def bind(runtime: str, scope: str, root: str) -> dict[str, JsonValue]:
    current = paired_client()
    client = SkillClient(current.base, current.workspace, current.token)
    manifest = _manifest(client, runtime, scope)
    if manifest.get("complete") is not True:
        raise CollabError("SKILL_MANIFEST_INCOMPLETE", "服务端返回的分发清单不完整。")
    device_id = str(UUID(str(manifest["device_id"])))
    profile = str(tool_home(runtime))
    with database() as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM binding WHERE root=? AND runtime=? AND scope=? AND profile=?",
            (root, runtime, scope, profile),
        ).fetchone()
        if row and (row["base"], row["workspace"]) != (client.base, client.workspace):
            raise CollabError("SKILL_BINDING_CONFLICT", "当前范围已绑定其他服务或工作空间。")
        if row and row["device"] != device_id:
            _rebind_device(db, row, device_id)
        if not row:
            db.execute(
                "INSERT INTO binding(id, root, runtime, scope, profile, base, workspace, device) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), root, runtime, scope, profile, client.base, client.workspace, device_id),
            )
    hook = install_hook(runtime)
    with database() as db:
        return {
            "hook": hook,
            "workspace": client.workspace,
            "server": client.base,
            "scope": scope,
            "target": str(destination(runtime, Path(root) if root else None)),
            "sync": pull(db, lookup(db, runtime, scope, root), client),
        }


def _installations(db: sqlite3.Connection, binding: sqlite3.Row) -> dict[str, sqlite3.Row]:
    return {row["slug"]: row for row in db.execute("SELECT * FROM installation WHERE binding=?", (binding["id"],))}


def _rebind_device(db: sqlite3.Connection, binding: sqlite3.Row, device_id: str) -> None:
    """同空间重新登录后重新登记已有副本，旧设备的离线记录不得归给新身份。"""
    cutoff = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    if db.execute(
        "SELECT 1 FROM sync_lease WHERE binding=? AND acquired_at>=?",
        (binding["id"], cutoff),
    ).fetchone():
        raise CollabError("SKILL_SYNC_IN_PROGRESS", "当前正在同步 Skill，请完成后重新绑定。")
    recover(db)
    db.execute("DELETE FROM pending WHERE binding=?", (binding["id"],))
    db.execute("UPDATE binding SET device=?, usage=0, checked=NULL WHERE id=?", (device_id, binding["id"]))
    for slug, row in _installations(db, binding).items():
        target = Path(row["target"])
        if not target.exists():
            db.execute("DELETE FROM installation WHERE binding=? AND slug=?", (binding["id"], slug))
            continue
        if inventory(target) != json.loads(row["files"]):
            raise conflict(target)
        slot, generation = str(uuid4()), str(uuid4())
        _receipt(db, binding, None, slot, str(uuid4()), generation, row["version_id"])
        db.execute(
            "UPDATE installation SET slot=?, generation=? WHERE binding=? AND slug=?",
            (slot, generation, binding["id"], slug),
        )


def _target(binding: sqlite3.Row, slug: str) -> Path:
    if not _SLUG.fullmatch(slug):
        raise CollabError("SKILL_SLUG_INVALID", "Skill 标识格式不正确。")
    root = Path(binding["root"]) if binding["scope"] == "project" else None
    return destination(binding["runtime"], root) / slug


def _ignore_project(binding: sqlite3.Row, target: Path) -> None:
    if binding["scope"] != "project":
        return
    root = Path(binding["root"])
    relative = target.relative_to(root).as_posix()
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--", relative],
        capture_output=True,
        timeout=4,
        check=True,
    ).stdout
    if tracked:
        raise conflict(target)
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
        capture_output=True,
        timeout=4,
        check=True,
    )
    path = Path(os.fsdecode(result.stdout.strip()))
    line = f"/{relative}/".encode()
    previous = path.read_bytes() if path.exists() else b""
    if line not in previous.splitlines():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as stream:
            stream.write((b"\n" if previous and not previous.endswith(b"\n") else b"") + line + b"\n")


def _receipt(
    db: sqlite3.Connection,
    binding: sqlite3.Row,
    row: sqlite3.Row | None,
    slot: str,
    operation: str,
    generation: str | None,
    version_id: str | None,
) -> None:
    queue(
        db,
        binding["id"],
        "receipt",
        {
            "operation_id": operation,
            "slot_id": slot,
            "previous_id": row["generation"] if row else None,
            "installation_id": generation,
            "version_id": version_id,
            "runtime": binding["runtime"],
            "occurred_at": now(),
        },
    )


def install(
    db: sqlite3.Connection,
    binding: sqlite3.Row,
    client: SkillClient,
    release: dict[str, JsonValue],
    *,
    pinned: bool,
    following: bool = False,
) -> str:
    slug = str(release["slug"])
    target = _target(binding, slug)
    version_id = str(UUID(str(release["version_id"])))
    expected = str(release["package_sha256"])
    with temporary_package() as package:
        downloaded = False
        while True:
            try:
                db.execute("BEGIN IMMEDIATE")
                recover(db)
                result = _install_locked(
                    db,
                    binding,
                    target,
                    release,
                    version_id,
                    expected,
                    pinned,
                    package if downloaded else None,
                    following=following,
                )
                db.commit()
                db.execute("BEGIN IMMEDIATE")
                recover(db)
                db.commit()
                return result
            except _DownloadRequired:
                db.rollback()
                client.download(version_id, expected, package)
                downloaded = True
            except Exception:
                db.rollback()
                raise


class _DownloadRequired(Exception):
    """退出本机写锁后再进行网络下载。"""


def _install_locked(
    db: sqlite3.Connection,
    binding: sqlite3.Row,
    target: Path,
    release: dict[str, JsonValue],
    version_id: str,
    expected: str,
    pinned: bool,
    package: Path | None,
    *,
    following: bool,
) -> str:
    slug = str(release["slug"])
    row = _installations(db, binding).get(slug)
    if following and (
        (row and row["pinned"])
        or db.execute(
            "SELECT 1 FROM excluded WHERE binding=? AND slug=?",
            (binding["id"], slug),
        ).fetchone()
    ):
        return "skipped"
    if row and Path(row["target"]) != target:
        # 工具配置移走目标时，先显式释放旧副本，避免把旧归属用于另一个目录。
        raise conflict(target)
    siblings = db.execute(
        "SELECT i.*, b.base, b.workspace FROM installation i JOIN binding b ON b.id=i.binding "
        "WHERE i.target=? AND i.binding<>?",
        (str(target), binding["id"]),
    ).fetchall()
    if any(s["workspace"] != binding["workspace"] or s["base"] != binding["base"] for s in siblings):
        raise conflict(target)
    if any(s["pinned"] and s["version_id"] != version_id for s in siblings):
        raise conflict(target)
    actual = inventory(target) if target.exists() else None
    known = [r for r in [row, *siblings] if r and actual == json.loads(r["files"])]
    if target.exists() and not known:
        raise conflict(target)
    root = Path(binding["root"]) if binding["root"] else None
    if any(
        path != target.parent and (path / slug / "SKILL.md").exists()
        for path in skill_roots(binding["runtime"], root or Path.cwd(), root)
    ):
        raise conflict(target)
    db.execute("DELETE FROM excluded WHERE binding=? AND slug=?", (binding["id"], slug))
    ready = any(r["digest"] == expected for r in known)
    if ready and row and row["version_id"] == version_id:
        db.execute("UPDATE installation SET pinned=? WHERE binding=? AND slug=?", (pinned, binding["id"], slug))
        return "current"
    if not ready and package is None:
        raise _DownloadRequired
    _ignore_project(binding, target)
    operation = uuid4().hex
    files = actual if ready else change_files(db, target, package, slug, operation, actual)
    assert files is not None
    generation = str(uuid4())
    slot = row["slot"] if row else str(uuid4())
    _receipt(db, binding, row, slot, operation, generation, version_id)
    db.execute(
        "INSERT OR REPLACE INTO installation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            binding["id"],
            slug,
            str(target),
            slot,
            generation,
            version_id,
            str(release["version"]),
            expected,
            json.dumps(files),
            pinned,
        ),
    )
    return "installed" if row is None else "updated"


def remove(db: sqlite3.Connection, binding: sqlite3.Row, slug: str, *, exclude: bool) -> str:
    db.execute("BEGIN IMMEDIATE")
    try:
        recover(db)
        row = _installations(db, binding).get(slug)
        if row:
            target = Path(row["target"])
            siblings = db.execute(
                "SELECT files FROM installation WHERE target=? AND binding<>?",
                (row["target"], binding["id"]),
            ).fetchall()
            if target.exists() and inventory(target) not in [json.loads(r["files"]) for r in (row, *siblings)]:
                raise conflict(target)
            operation = uuid4().hex
            if target.exists() and not siblings:
                change_files(db, target, None, slug, operation, json.loads(row["files"]))
            _receipt(db, binding, row, row["slot"], operation, None, None)
            db.execute("DELETE FROM installation WHERE binding=? AND slug=?", (binding["id"], slug))
        if exclude:
            db.execute("INSERT OR IGNORE INTO excluded VALUES (?, ?)", (binding["id"], slug))
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        recover(db)
        db.commit()
        return "removed" if row else "absent"
    except Exception:
        db.rollback()
        raise


def flush(db: sqlite3.Connection, binding: sqlite3.Row, client: SkillClient) -> int:
    with db:
        db.execute("BEGIN IMMEDIATE")
        recover(db)
    sent = 0
    receipts = db.execute(
        "SELECT * FROM pending WHERE binding=? AND kind='receipt' ORDER BY id LIMIT 100",
        (binding["id"],),
    ).fetchall()
    # 回执按代次顺序提交；传输期间不占用 SQLite 写锁，Hook 仍可记录新事件。
    for row in receipts:
        try:
            client.request("POST", "/installation-receipts", as_object(json.loads(row["payload"])))
        except CollabError as exc:
            if exc.code in _DEVICE_REVOKED:
                disable_usage(db, binding)
            raise
        with db:
            db.execute("DELETE FROM pending WHERE id=?", (row["id"],))
        sent += 1
    if len(receipts) == 100:
        return sent
    pending = db.execute(
        "SELECT * FROM pending WHERE binding=? AND kind='usage' ORDER BY id LIMIT 100",
        (binding["id"],),
    ).fetchall()
    if not pending:
        return sent
    try:
        events: list[JsonValue] = [as_object(json.loads(row["payload"])) for row in pending]
        response = client.request("POST", "/usage-events/batch", {"events": events})
    except CollabError as exc:
        if exc.code in _DEVICE_REVOKED or exc.code == "SKILL_USAGE_DISABLED":
            disable_usage(db, binding)
        if exc.code != "SKILL_USAGE_DISABLED":
            raise
        return sent
    outcomes = items(response)
    if len(outcomes) != len(pending):
        raise CollabError("INVALID_RESPONSE", "服务端上报结果数量不正确。")
    with db:
        for row, outcome in zip(pending, outcomes, strict=True):
            status = outcome["status"]
            if status == "retry":
                continue
            if status not in {"accepted", "duplicate", "rejected"}:
                raise CollabError("INVALID_RESPONSE", "服务端返回了未知的上报状态。")
            db.execute("DELETE FROM pending WHERE id=?", (row["id"],))
            sent += 1
    return sent


def record_sync_result(db: sqlite3.Connection, binding: sqlite3.Row, result: dict[str, JsonValue]) -> None:
    with db:
        db.execute("INSERT OR REPLACE INTO sync_result VALUES (?, ?)", (binding["id"], json.dumps(result)))


def disable_usage(db: sqlite3.Connection, binding: sqlite3.Row) -> None:
    """撤销设备或统计授权后，不再采集，也不保留未发送的用量。"""
    with db:
        db.execute("UPDATE binding SET usage=0 WHERE id=?", (binding["id"],))
        db.execute("DELETE FROM pending WHERE binding=? AND kind='usage'", (binding["id"],))


def pull(
    db: sqlite3.Connection,
    binding: sqlite3.Row,
    client: SkillClient,
    *,
    manifest: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """串行处理同一绑定的清单，避免旧响应覆盖已经装上的新版本。"""
    owner = uuid4().hex
    with db:
        db.execute("BEGIN IMMEDIATE")
        cutoff = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        db.execute("DELETE FROM sync_lease WHERE binding=? AND acquired_at<?", (binding["id"], cutoff))
        if not db.execute(
            "INSERT OR IGNORE INTO sync_lease VALUES (?, ?, ?)",
            (binding["id"], owner, now()),
        ).rowcount:
            return {"status": "sync_in_progress"}
    try:
        result = _pull_locked(db, binding, client, owner, manifest)
        record_sync_result(db, binding, result)
        return result
    except CollabError as exc:
        if exc.code in _DEVICE_REVOKED:
            disable_usage(db, binding)
        record_sync_result(db, binding, {"code": exc.code, "message": str(exc)})
        raise
    finally:
        with db:
            db.execute("DELETE FROM sync_lease WHERE binding=? AND owner=?", (binding["id"], owner))


def _pull_locked(
    db: sqlite3.Connection,
    binding: sqlite3.Row,
    client: SkillClient,
    owner: str,
    manifest: dict[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    # 只有完整的同设备清单才允许清理本机已撤回的副本。
    manifest = manifest or _manifest(client, binding["runtime"], binding["scope"])
    if manifest.get("complete") is not True or manifest.get("device_id") != binding["device"]:
        raise CollabError("SKILL_MANIFEST_INCOMPLETE", "分发清单不完整或设备身份不一致。")
    desired = {str(entry["slug"]): entry for entry in items(manifest)}
    current = _installations(db, binding)
    excluded = {row[0] for row in db.execute("SELECT slug FROM excluded WHERE binding=?", (binding["id"],))}
    changes: dict[str, JsonValue] = {}
    for slug in sorted(desired.keys() | current.keys()):
        with db:
            if not db.execute(
                "UPDATE sync_lease SET acquired_at=? WHERE binding=? AND owner=?",
                (now(), binding["id"], owner),
            ).rowcount:
                raise CollabError("SKILL_SYNC_SUPERSEDED", "这次同步已被较新的操作替代。")
        row = current.get(slug)
        try:
            if slug not in desired:
                changes[slug] = remove(db, binding, slug, exclude=False)
            elif slug not in excluded and not (row and row["pinned"]):
                changes[slug] = install(db, binding, client, desired[slug], pinned=False, following=True)
        except CollabError as exc:
            if exc.code != "SKILL_LOCAL_CONFLICT":
                raise
            changes[slug] = {"status": "conflict", "message": str(exc)}
    with db:
        db.execute(
            "UPDATE binding SET checked=?, usage=? WHERE id=?",
            (now(), manifest.get("usage_enabled") is True, binding["id"]),
        )
    if manifest.get("usage_enabled") is not True:
        disable_usage(db, binding)
    return {"changes": changes, "reported": flush(db, binding, client)}


def _binding_scope(args: argparse.Namespace) -> tuple[str, str]:
    if args.scope == "project" and args.runtime in USER_ONLY:
        raise CollabError("SKILL_USER_SCOPE_REQUIRED", "该工具从个人配置目录加载 Skill，请使用 --scope user。")
    return args.scope, str(project_root(Path(args.project))) if args.scope == "project" else ""


def arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("bind", "pull", "install", "update", "follow", "uninstall", "status", "install-hook", "launch"):
        command = actions.add_parser(action)
        command.add_argument("--runtime", choices=TOOLS, required=True)
        command.add_argument("--scope", choices=("project", "user"), default="project")
        command.add_argument("--project", default=".")
        if action in {"install", "update", "follow", "uninstall"}:
            command.add_argument("skill")
        if action == "update":
            command.add_argument("--to")
        if action == "launch":
            command.add_argument("--executable", help="原生工具的启动程序路径")
            command.add_argument("host_args", nargs=argparse.REMAINDER)
    usage = actions.add_parser("usage").add_subparsers(dest="usage_action", required=True)
    drain = usage.add_parser("flush")
    drain.add_argument("--runtime", choices=TOOLS, default="codex")
    drain.add_argument("--scope", choices=("project", "user"), default="project")
    drain.add_argument("--project", default=".")
    hook = actions.add_parser("hook")
    hook.add_argument("--runtime", choices=TOOLS, required=True)
    hook.add_argument("--event", choices=("start", "prompt", "tool", "stop", "sync", "drain"), required=True)


def execute(args: argparse.Namespace) -> JsonValue:
    if args.action == "install-hook":
        return install_hook(args.runtime)
    scope, root = _binding_scope(args)
    if args.action == "bind":
        return bind(args.runtime, scope, root)
    with database() as db:
        binding = lookup(db, args.runtime, scope, root)
        if args.action == "status":
            result = db.execute("SELECT payload FROM sync_result WHERE binding=?", (binding["id"],)).fetchone()
            return {
                "items": [
                    {k: row[k] for k in ("slug", "target", "version", "pinned")}
                    for row in _installations(db, binding).values()
                ],
                "last_checked": binding["checked"],
                "last_sync": json.loads(result["payload"]) if result else None,
                "usage_enabled": bool(binding["usage"]),
                "profile": binding["profile"],
                "pending": db.execute("SELECT count(*) FROM pending WHERE binding=?", (binding["id"],)).fetchone()[0],
            }
        if args.action == "uninstall":
            return {"status": remove(db, binding, args.skill, exclude=True)}
        client = client_for(binding)
        if args.action == "usage":
            return {"reported": flush(db, binding, client)}
        if args.action == "pull":
            return pull(db, binding, client)
        slug, _, pin = args.skill.partition("@")
        if args.action == "update":
            pin = args.to or pin
            installed = _installations(db, binding).get(slug)
            if installed and installed["pinned"] and not pin:
                return {"status": "pinned", "version": installed["version"]}
        query = {"slug": slug}
        if pin:
            query["version"] = pin
        release = client.request("GET", "/resolve?" + urlencode(query))
        return {
            "status": install(db, binding, client, release, pinned=bool(pin)),
            "version": release["version"],
            "reported": flush(db, binding, client),
        }


def launch_bound(args: argparse.Namespace) -> int:
    scope, root = _binding_scope(args)
    with database() as db:
        binding = lookup(db, args.runtime, scope, root)
        try:
            pull(db, binding, client_for(binding))
        except CollabError as exc:
            if exc.code != "WEORA_UNREACHABLE":
                raise
    host_args = args.host_args[1:] if args.host_args[:1] == ["--"] else args.host_args
    return launch(args.runtime, host_args, Path(args.project).resolve(), args.executable)
