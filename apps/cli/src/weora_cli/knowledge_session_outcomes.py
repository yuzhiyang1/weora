"""从本机固定的 Git 起止点归因，只向控制面提交产出类别。"""

import re
from pathlib import Path

from weora_cli.collab_client import CollabError
from weora_cli.commands import JsonValue
from weora_cli.knowledge_mr import ReviewRepository, _authorization, _request, _rows, hint_repository
from weora_cli.knowledge_session_origin import _git, checkout_remote, resolve_commit

_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_IDLE_SECONDS = 6 * 3600


def _anchor(cwd: Path) -> str | None:
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return str(Path(common).resolve()) if common and Path(common).is_absolute() else None


def git_start(cwd: Path, when: float) -> dict[str, JsonValue]:
    anchor = _anchor(cwd)
    if anchor is None:
        return {"started_at": when}
    return {
        "started_at": when, "anchor": anchor, "start_head": resolve_commit(cwd, "HEAD"),
        "author": _git(cwd, "config", "--get", "user.email"), "remote": checkout_remote(cwd),
    }


def git_stop(
    cwd: Path, start: dict[str, JsonValue], previous: JsonValue,
) -> dict[str, JsonValue]:
    if start.get("anchor") is None or _anchor(cwd) != start.get("anchor"):
        return {}
    end = resolve_commit(cwd, "HEAD")
    branch = _git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch and isinstance(previous, dict):
        branch = str(previous.get("branch") or "")
    return {"end_head": end, "branch": branch or None}


def eligible(row: dict[str, JsonValue], now: float) -> bool:
    stopped = row.get("stopped")
    return (
        row.get("outcome_recorded") is not True and row.get("running") is not True
        and row.get("pending") is not True and isinstance(row.get("outcome_start"), dict)
        and isinstance(stopped, int | float) and not isinstance(stopped, bool) and stopped <= now
    )


def finished(row: dict[str, JsonValue], now: float) -> bool:
    stopped = row.get("stopped")
    return (
        row.get("mode") == "unattended"
        or (isinstance(stopped, int | float) and now - stopped >= _IDLE_SECONDS)
    )


def _checkout(cwd: Path, anchor: JsonValue) -> Path | None:
    if not isinstance(anchor, str):
        return None
    for path in (cwd, Path(anchor).parent):
        if path.is_dir() and _anchor(path) == anchor:
            return path
    return None


def _commits(cwd: Path, start: dict[str, JsonValue], end: str, stopped: float) -> set[str]:
    begin, author, started = start.get("start_head"), start.get("author"), start.get("started_at")
    if (
        not _COMMIT.fullmatch(end) or begin == end or not isinstance(author, str)
        or not author or any(char in author for char in "<>\r\n\0") or not isinstance(started, int | float)
    ):
        return set()
    ref = f"{begin}..{end}" if isinstance(begin, str) and _COMMIT.fullmatch(begin) else end
    rows = _git(cwd, "log", "--format=%H%x00%ae", "--fixed-strings", f"--author=<{author}>",
                f"--since-as-filter=@{int(started)}", f"--until=@{int(stopped)}", "--max-count=2048", ref, "--",
                timeout=5)
    return {
        sha for line in (rows or "").splitlines()
        if (parts := line.split("\0", 1)) and len(parts) == 2
        and (sha := parts[0]) and _COMMIT.fullmatch(sha) and parts[1] == author
    }


def _heads(repo: ReviewRepository, branch: str, base: str | None) -> list[str]:
    authorization = _authorization(repo.provider)
    params = {"per_page": "25"}
    heads: list[str] = []
    params.update(state="open", head=f"{repo.repository.split('/', 1)[0]}:{branch}")
    if base:
        params["base"] = base
    value, _ = _request(repo, authorization, f"/repos/{repo.repository}/pulls", params=params, timeout=6)
    for pr in _rows(value):
        head, target = pr.get("head"), pr.get("base")
        if not isinstance(head, dict) or not isinstance(target, dict):
            continue
        source_repo, target_repo = head.get("repo"), target.get("repo")
        if (
            pr.get("state") == "open" and head.get("ref") == branch and (base is None or target.get("ref") == base)
            and isinstance(source_repo, dict) and isinstance(target_repo, dict)
            and str(source_repo.get("full_name", "")).lower() == repo.repository
            and str(target_repo.get("full_name", "")).lower() == repo.repository
        ):
            heads.append(str(head.get("sha", "")))
    return [head for head in heads if _COMMIT.fullmatch(head)]


def git_outcomes(row: dict[str, JsonValue], cwd: Path) -> tuple[bool, str | None]:
    start, stop, stopped = row.get("outcome_start"), row.get("outcome_stop"), row.get("stopped")
    if not isinstance(start, dict) or not isinstance(stop, dict) or not isinstance(stopped, int | float):
        return False, None
    end, branch, remote = stop.get("end_head"), stop.get("branch"), start.get("remote")
    checkout = _checkout(cwd, start.get("anchor"))
    if not isinstance(end, str) or not _COMMIT.fullmatch(end) or checkout is None:
        return False, None
    commits = _commits(checkout, start, end, stopped)
    if not commits or not isinstance(branch, str) or not branch or not isinstance(remote, str):
        return bool(commits), None
    cached = row.get("outcome_pr")
    if isinstance(cached, dict) and cached.get("branch") == branch and str(cached.get("commit")) in commits:
        return True, str(cached["commit"])
    if checkout_remote(checkout) != remote or _git(checkout, "check-ref-format", f"refs/heads/{branch}") is None:
        return True, None
    pushed = resolve_commit(checkout, f"refs/remotes/origin/{branch}")
    default = _git(checkout, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD") or ""
    base = default.removeprefix("origin/") or None
    if pushed is None or branch == base:
        return True, None
    pushed_commits = commits & _commits(checkout, start, pushed, stopped)
    if not pushed_commits:
        return True, None
    try:
        repo = hint_repository(remote)
        if repo is not None:
            for head in _heads(repo, branch, base):
                verified = (
                    pushed_commits if head == pushed else pushed_commits & _commits(checkout, start, head, stopped)
                )
                if verified:
                    return True, min(verified)
    except (CollabError, ValueError):
        # 托管方暂时不可读时，已有 Git 提交仍可结算。
        pass
    return True, None
