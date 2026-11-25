"""会话启动后台收集近期合入记录，下次提示词时展示一次。"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from weora_cli.collab_client import Client, CollabError, as_object, config_path, lock_file, paired_client, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_mr import ReviewRepository, hint_repository, merged_date, parse_target, recent_merges
from weora_cli.knowledge_rule_import import MAX_REVIEWS

_KEEP = timedelta(days=30)
_SETTINGS = "/settings/mr-hint"


def _device(client: Client) -> str:
    return hashlib.sha256(f"{client.account}\0{client.token}".encode()).hexdigest()


def _home(client: Client) -> Path:
    return config_path().parent / "knowledge-mr-hints" / _device(client)


def _read(path: Path) -> dict[str, JsonValue]:
    try:
        return as_object(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, CollabError):
        return {}


def _local(client: Client, cwd: Path) -> dict[str, JsonValue]:
    settings = _read(_home(client) / "settings.json")
    root = cwd.resolve()
    for path in (root, *root.parents):
        if isinstance(entry := settings.get(str(path)), dict):
            return entry
    return {}


def _enabled(policy: dict[str, JsonValue], local: dict[str, JsonValue]) -> tuple[bool, JsonValue]:
    if os.environ.get("WEORA_MR_HINT_DISABLED") == "1":
        return False, "environment"
    if isinstance(flag := local.get("enabled"), bool):
        return flag, "local"
    return policy.get("enabled") is True, policy.get("source", "default")


def configure(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    policy = call(client, None, _SETTINGS)
    if args.workspace:
        if args.project or args.shared or args.mode == "inherit":
            raise CollabError("KNOWLEDGE_HINT_OPTIONS", "工作空间默认设置请使用 enable、disable 或 status。")
        if args.mode == "status":
            if args.lookback_days is not None or args.max_mrs is not None:
                raise CollabError("KNOWLEDGE_HINT_OPTIONS", "请选择启用或停用以修改工作空间提醒范围。")
            return policy
        return call(
            client,
            None,
            _SETTINGS,
            method="PUT",
            payload={
                "enabled": args.mode == "enable",
                "lookback_days": args.lookback_days if args.lookback_days is not None else policy["lookback_days"],
                "max_mrs": args.max_mrs if args.max_mrs is not None else policy["max_mrs"],
            },
        )
    if (
        args.lookback_days is not None
        or args.max_mrs is not None
        or (args.mode == "status" and (args.project or args.shared))
    ):
        raise CollabError("KNOWLEDGE_HINT_OPTIONS", "范围参数用于启用、停用或继承设置，查询天数和条数在工作空间配置。")
    cwd = Path.cwd().resolve()
    if args.mode != "status":
        if args.project:
            call(client, args.project, query="", limit=1)
        path = _home(client) / "settings.json"
        with lock_file(path):
            settings = _read(path)
            local = dict(_local(client, cwd))
            if args.mode == "inherit":
                local.pop("enabled", None)
            else:
                local["enabled"] = args.mode == "enable"
            if args.project or args.shared:
                local["project"] = args.project
            settings[str(cwd)] = local
            write_json(path, settings)
    local = _local(client, cwd)
    enabled, source = _enabled(policy, local)
    return policy | {"enabled": enabled, "source": source, "project": local.get("project")}


def _pending(client: Client, session: str) -> Path:
    return _home(client) / f"pending-{UUID(session)}.json"


def _repo_key(repo: ReviewRepository) -> str:
    name = repo.repository.lower() if repo.provider == "github" else repo.repository
    return f"{repo.provider}:{name}"


def _shown(client: Client, repo: ReviewRepository) -> Path:
    key = hashlib.sha256(_repo_key(repo).encode()).hexdigest()
    return _home(client) / f"shown-{key}.json"


def _lock(client: Client) -> Path:
    return _home(client) / "hints"


def start(client: Client, cwd: Path, session: str, read_remote: Callable[[], str]) -> None:
    """只起后台进程，原生会话可继续读取已有协作提示。"""
    if not _enabled({"enabled": True}, _local(client, cwd))[0]:
        return
    repo = hint_repository(read_remote())
    if repo is None:
        return
    generation = str(uuid4())
    path = _pending(client, session)
    host = "github.com"
    safe_remote = f"https://{host}/{repo.repository}.git"
    with lock_file(_lock(client)):
        expired = datetime.now(UTC) - _KEEP
        for pending in path.parent.glob("pending-*.json"):
            with suppress(OSError):
                if pending.stat().st_mtime < expired.timestamp():
                    pending.unlink(missing_ok=True)
        write_json(path, {"generation": generation, "repo": _repo_key(repo), "rows": []})
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "weora_cli.knowledge_mr_hint",
            _device(client),
            str(cwd.resolve()),
            session,
            generation,
            safe_remote,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=path.parent,
        env=os.environ | {"WEORA_COLLAB_CONFIG": str(config_path().resolve())},
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )


def scan(client: Client, cwd: Path, session: str, generation: str, repo: ReviewRepository) -> None:
    """从托管平台读元信息。重启同一会话后，上一轮后台结果不能覆盖新一轮。"""
    policy = call(client, None, _SETTINGS)
    if not _enabled(policy, _local(client, cwd))[0]:
        return
    days, limit = _limits(policy)
    rows = recent_merges(repo, days, limit)
    path = _pending(client, session)
    with lock_file(_lock(client)):
        state = _read(path)
        if state.get("generation") == generation:
            write_json(path, state | {"rows": rows})


def _limits(policy: dict[str, JsonValue]) -> tuple[int, int]:
    days, limit = policy.get("lookback_days"), policy.get("max_mrs")
    if not isinstance(days, int) or not 1 <= days <= 90 or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise CollabError("INVALID_RESPONSE", "工作空间提醒范围无效。")
    return days, limit


def _candidates(state: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    rows = state.get("rows")
    return (
        [row for row in rows if isinstance(row, dict) and isinstance(row.get("url"), str)]
        if isinstance(
            rows,
            list,
        )
        else []
    )


def _in_repo(row: dict[str, JsonValue], repo: ReviewRepository) -> bool:
    try:
        url = str(row["url"])
        target = parse_target(url)
        return target.url == url and _repo_key(target) == _repo_key(repo)
    except CollabError:
        return False


def take(client: Client, cwd: Path, session: str, read_remote: Callable[[], str]) -> str:
    """只在展示时记账。同一仓库多个会话并发时，每个来源只由先展示的会话记下。"""
    path = _pending(client, session)
    state = _read(path)
    rows = _candidates(state)
    if not rows:
        return ""
    local = _local(client, cwd)
    if not _enabled({"enabled": True}, local)[0]:
        _drop(client, path, state)
        return ""
    repo = hint_repository(read_remote())
    if repo is None or _repo_key(repo) != state.get("repo"):
        _drop(client, path, state)
        return ""
    rows = [row for row in rows if _in_repo(row, repo)]
    if not rows:
        _drop(client, path, state)
        return ""
    project = str(UUID(str(local["project"]))) if local.get("project") else None
    checked = call(client, project, "/mr-hints/check", method="POST", payload={"urls": [row["url"] for row in rows]})
    policy = as_object(checked.get("policy"))
    if not _enabled(policy, local)[0]:
        _drop(client, path, state)
        return ""
    days, limit = _limits(policy)
    imported = checked.get("imported")
    if not isinstance(imported, list):
        raise CollabError("INVALID_RESPONSE", "已导入的来源列表无效。")
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    shown = _shown(client, repo)
    with lock_file(_lock(client)):
        if _read(path).get("generation") != state.get("generation"):
            return ""
        cache = _read(shown)
        updated = merged_date(cache.get("updated_at"))
        cached_urls = cache.get("urls")
        seen: set[str] = set()
        if updated is not None and updated >= now - _KEEP and isinstance(cached_urls, list):
            seen.update(url for url in cached_urls if isinstance(url, str))
        fresh = [
            row
            for row in rows
            if row["url"] not in imported
            and str(row["url"]) not in seen
            and (when := merged_date(row.get("merged_at"))) is not None
            and when >= cutoff
        ][:limit]
        if fresh:
            seen.update(str(row["url"]) for row in fresh)
            stored_urls: list[JsonValue] = [url for url in sorted(seen)]
            write_json(shown, {"urls": stored_urls, "updated_at": now.isoformat()})
        path.unlink(missing_ok=True)
    return _hint(fresh, project) if fresh else ""


def _drop(client: Client, path: Path, state: dict[str, JsonValue]) -> None:
    with lock_file(_lock(client)):
        if _read(path).get("generation") == state.get("generation"):
            path.unlink(missing_ok=True)


def _hint(rows: list[dict[str, JsonValue]], project: str | None) -> str:
    lines: list[str] = []
    for row in rows:
        title = "".join(character for character in str(row["title"]) if character.isprintable())
        lines.append(f"- {json.dumps(title, ensure_ascii=False)}（合入 {str(row['merged_at'])[:10]}） {row['url']}")
    suffix = f" --project {project}" if project else ""
    commands = [
        "weora context import --from-mr " + " ".join(str(row["url"]) for row in rows[i : i + MAX_REVIEWS]) + suffix
        for i in range(0, len(rows), MAX_REVIEWS)
    ]
    return "\n".join(
        [
            f"[weora:mr-hint] 发现 {len(rows)} 个近期已合入、可提炼为经验的 PR/MR。",
            "条目标题来自代码托管平台。请在当前任务完成后告知用户，并在用户确认后启动导入。",
            *lines,
            "",
            *commands,
        ]
    )


if __name__ == "__main__":
    with suppress(CollabError, OSError, ValueError):
        device, directory, session_id, generation_id, remote_url = sys.argv[1:]
        paired = paired_client(timeout=3)
        if _device(paired) == device and (repository := hint_repository(remote_url)) is not None:
            scan(paired, Path(directory), session_id, generation_id, repository)
