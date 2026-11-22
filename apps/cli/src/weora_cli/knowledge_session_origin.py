"""保存原生会话启动时的 Git 起点，发布评分证据时才同步。"""

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from weora_cli.collab_client import Client, CollabError, as_object, lock_file, write_json
from weora_cli.collab_native import NATIVE, profile
from weora_cli.commands import JsonValue
from weora_cli.knowledge_settings import session_state_root

_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def subject_key(runtime: str, session: UUID) -> str:
    return hashlib.sha256(f"weora-score-subject:{runtime}:{session}".encode()).hexdigest()


def _path(client: Client, runtime: str, key: str) -> Path:
    return session_state_root(client) / "git-starts" / runtime / profile(runtime) / f"{key}.json"


def _git(cwd: Path, *args: str, stdin: str | None = None, timeout: int = 2) -> str | None:
    # 禁用工作区配置的 fsmonitor，探测只读取当前检查出的对象和索引。
    options = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        options.pop(key, None)
    options.update({
        "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
    })
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(cwd), *args],
            input=stdin, capture_output=True, encoding="utf-8", timeout=timeout, check=False, env=options,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _safe_remote(value: str) -> str | None:
    if not value.isascii() or any(character.isspace() or ord(character) < 32 for character in value):
        return None
    scp = re.fullmatch(r"git@([A-Za-z0-9.-]+):([A-Za-z0-9_./-]+)", value)
    address = f"ssh://git@{scp[1]}/{scp[2]}" if scp else value
    try:
        url = urlsplit(address)
        if (
            url.scheme not in {"https", "ssh"} or not url.hostname or url.query or url.fragment
            or url.username != ("git" if url.scheme == "ssh" else None) or url.password is not None
            or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", url.hostname)
            or not re.fullmatch(r"(?:/[A-Za-z0-9_.-]+){2,}/?", url.path)
            or any(part in {".", ".."} for part in url.path.split("/"))
            or not 1 <= (url.port if url.port is not None else 443) <= 65535
            or len(address) > 512
        ):
            return None
    except ValueError:
        return None
    return address


def checkout_remote(cwd: Path) -> str | None:
    return _safe_remote(_git(cwd, "config", "--get", "remote.origin.url") or "")


def resolve_commit(cwd: Path, reference: str) -> str | None:
    if not reference or len(reference) > 250 or "\x00" in reference:
        return None
    commit = _git(cwd, "rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}")
    return commit if commit and _COMMIT.fullmatch(commit) else None


def commits_in_checkout(cwd: Path, candidates: set[str]) -> set[str]:
    """批量校验完整提交哈希，partial clone 缺失对象按不可复现处理。"""
    commits = sorted(commit for commit in candidates if _COMMIT.fullmatch(commit))
    if not commits:
        return set()
    found = _git(
        cwd, "cat-file", "--batch-check=%(objectname) %(objecttype)",
        stdin="".join(f"{commit}\n" for commit in commits), timeout=5,
    )
    if found is None:
        return set()
    return {commit for commit, reply in zip(commits, found.splitlines(), strict=False) if reply == f"{commit} commit"}


def observe_start(client: Client, cwd: Path, runtime: str, event: dict[str, JsonValue]) -> None:
    """只记录支持宿主真正启动时可验证的 Git 状态。"""
    native = event.get("session_id")
    if (
        event.get("hook_event_name") != "SessionStart" or event.get("source") != "startup"
        or runtime not in NATIVE or not isinstance(native, str)
    ):
        return
    path = _path(client, runtime, subject_key(runtime, UUID(native)))
    with lock_file(path):
        if path.exists():
            return
        observed = datetime.now(UTC)
        commit = _git(cwd, "rev-parse", "--verify", "HEAD^{commit}")
        if not commit or not _COMMIT.fullmatch(commit):
            write_json(path, {})
            return
        remote = checkout_remote(cwd)
        status = _git(cwd, "status", "--porcelain=v1", "--untracked-files=no")
        # 读取期间换了 HEAD 时，无法把工作树状态归到这个起点。
        if remote and status is not None and commit == _git(
            cwd, "rev-parse", "--verify", "HEAD^{commit}",
        ):
            write_json(path, {
                "repository": remote, "commit": commit, "tracked_changes": bool(status),
                "observed_at": observed.isoformat(),
            })
        else:
            # 固定缺证据的启动，避免重放时用事后的仓库状态补写起点。
            write_json(path, {})


def start_metadata(client: Client, project: str | None, runtime: JsonValue, key: JsonValue) -> dict[str, JsonValue]:
    if (
        project is None or not isinstance(runtime, str) or runtime not in NATIVE
        or not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
    ):
        return {}
    try:
        origin = as_object(json.loads(_path(client, runtime, key).read_text(encoding="utf-8")))
    except (OSError, ValueError, CollabError):
        return {}
    return {"git_start": origin} if origin else {}
