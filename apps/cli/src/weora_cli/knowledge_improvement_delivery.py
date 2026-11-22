"""按平台预留提交创建或续写一个固定 GitHub PR。"""

import argparse
import base64
import hashlib
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from weora_cli.collab_client import Client, CollabError, as_object, items
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_benchmark_executor import _clone, _env
from weora_cli.knowledge_mr import ReviewRepository, _authorization, _request, hint_repository
from weora_cli.knowledge_repo_import import repository_plan
from weora_cli.knowledge_session_origin import checkout_remote, commits_in_checkout

_SHA = re.compile(r"[0-9a-f]{40}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*")
_ASSET = re.compile(
    r"(?:(?:rules|skills|agents)(?:/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})+|AGENTS\.md|CLAUDE\.md"
    r"|benchmarks/(?:[A-Za-z0-9_-]+/){1,2}tasks/[A-Za-z0-9_-]+\.yaml)"
)


def _ref(value: JsonValue) -> str:
    if not isinstance(value, str) or not _BRANCH.fullmatch(value) or ".." in value or value.endswith((".", ".lock")):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_BRANCH", "提案或目标分支格式无效。")
    return "refs/heads/" + value


def _sha(value: JsonValue) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_COMMIT", "交付需要完整的 GitHub 提交标识。")
    return value


def _git(root: Path, env: dict[str, str], *args: str, content: bytes | None = None) -> bytes:
    binary = shutil.which("git")
    if binary is None:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_GIT", "当前机器没有 Git。")
    try:
        result = subprocess.run(
            [binary, "-C", str(root), *args], env=env, input=content or b"", stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=True, timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_GIT", "Git 交付失败，请核对仓库、提交和远端状态。") from error
    if len(result.stdout) > 4_000_000:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_GIT", "Git 响应超过可处理范围。")
    return result.stdout


def _object_format(entry: bytes) -> tuple[str, str]:
    mode, kind, _ = entry.decode().split("\t", 1)[0].split()
    return mode, kind


def materialize(
    root: Path, spec: dict[str, JsonValue], parent: str, *, prefix: str | None = None,
) -> tuple[str, str]:
    """使用 Git 索引写入冻结文件，始终从固定基准重建完整树。"""
    base = _sha(spec.get("base_commit"))
    env = _env(git=True)
    entry = _git(root, env, "ls-tree", base, "--", ".teamai")
    if entry and _object_format(entry) != ("040000", "tree"):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_ASSET", "团队资源根目录必须是普通目录。")
    actual_prefix = ".teamai/" if entry else ""
    if prefix is not None and prefix != actual_prefix:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_ASSET", "提案续写使用的资源目录与第一次不同。")
    _git(root, env, "read-tree", base)
    edits = items({"items": spec.get("files")})
    if not edits:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_ASSET", "提案没有可交付文件。")
    for edit in sorted(edits, key=lambda item: item.get("content") is not None):
        path, before, content = edit.get("path"), edit.get("before_sha256"), edit.get("content")
        if not isinstance(path, str) or not _ASSET.fullmatch(path):
            raise CollabError("KNOWLEDGE_IMPROVEMENT_ASSET", "变更路径超出团队资源及回归任务。")
        path = actual_prefix + path
        original = _git(root, env, "ls-tree", base, "--", path)
        mode = "100644"
        if original:
            mode, kind = _object_format(original)
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise CollabError("KNOWLEDGE_IMPROVEMENT_ASSET", "交付文件不能覆盖链接、子模块或目录。")
            old = _git(root, env, "show", f"{base}:{path}")
            if hashlib.sha256(old).hexdigest() != before:
                raise CollabError("KNOWLEDGE_IMPROVEMENT_BASE", "资源原始内容与提案固定的摘要不同。")
        elif before is not None or content is None:
            raise CollabError("KNOWLEDGE_IMPROVEMENT_BASE", "资源在原始基准中的状态与提案不符。")
        if content is None:
            _git(root, env, "update-index", "--force-remove", "--", path)
        elif isinstance(content, str):
            blob = _sha(_git(root, env, "hash-object", "-w", "--stdin", content=content.encode()).decode().strip())
            _git(root, env, "update-index", "--add", "--cacheinfo", mode, blob, path)
        else:
            raise CollabError("KNOWLEDGE_IMPROVEMENT_ASSET", "提案的资源正文无效。")
    tree = _sha(_git(root, env, "write-tree").decode().strip())
    if tree == _git(root, env, "rev-parse", f"{parent}^{{tree}}").decode().strip():
        raise CollabError("KNOWLEDGE_IMPROVEMENT_ASSET", "此修订与原分支内容相同。")
    # 使用修订时间和固定作者，重试时由同一树及父提交得到同一 SHA。
    created = datetime.fromisoformat(str(spec["created_at"]))
    timestamp = f"{int(created.timestamp())} +0000"
    for identity in ("AUTHOR", "COMMITTER"):
        env.update({
            f"GIT_{identity}_NAME": "Weora Improvement", f"GIT_{identity}_EMAIL": "improvement@weora.invalid",
            f"GIT_{identity}_DATE": timestamp,
        })
    message = f"Improve {spec['scorer']}\n\n{spec['marker']}\nWeora revision: {spec['revision']}\n"
    return _sha(_git(root, env, "commit-tree", tree, "-p", parent, "-m", message).decode().strip()), actual_prefix


def _remote_env(headers: dict[str, str]) -> dict[str, str]:
    bearer = headers.get("Authorization", "")
    if not bearer.startswith("Bearer ") or not bearer[7:]:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_AUTH", "请先登录 GitHub 并取得资源仓库写权限。")
    basic = base64.b64encode(("x-access-token:" + bearer[7:]).encode()).decode()
    env = _env(git=True)
    env.update({
        "GIT_ALLOW_PROTOCOL": "https", "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic " + basic,
        "GIT_CONFIG_KEY_1": "http.followRedirects", "GIT_CONFIG_VALUE_1": "false",
    })
    return env


def _remote_ref(root: Path, env: dict[str, str], remote: str, ref: str) -> str | None:
    listing = _git(root, env, "ls-remote", "--refs", "--", remote, ref).decode().strip()
    if not listing:
        return None
    sha, separator, returned = listing.partition("\t")
    if not separator or returned != ref:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_GIT", "远端返回了无法核验的分支。")
    return _sha(sha)


def _review(
    repo: ReviewRepository, headers: dict[str, str], spec: dict[str, JsonValue], head: str,
    known: int | None,
) -> dict[str, JsonValue]:
    branch, base, marker = str(spec["branch"]), str(spec["base_branch"]), str(spec["marker"])
    path = f"/repos/{repo.repository}/pulls"
    owner = repo.repository.split("/", 1)[0]
    rows = _request(repo, headers, path,
                    params={"state": "open", "head": f"{owner}:{branch}", "base": base, "per_page": "2"})[0]
    found = items({"items": rows})
    if known is not None and (len(found) != 1 or found[0].get("number") != known):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PR", "已登记的 PR 不再匹配该分支。")
    if len(found) > 1:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PR", "远端出现多个匹配 PR。")
    if found:
        review = found[0]
    else:
        # 创建请求超时后，重试先按稳定分支查询，避免另开同一提案的 PR。
        review = as_object(_request(repo, headers, path, method="POST", payload={
            "title": f"Improve {spec['scorer']}",
            "body": f"{marker}\n\nWeora proposal {spec['proposal_id']}.",
            "head": branch, "base": base, "draft": False,
        })[0])
    remote_head, remote_base = as_object(review.get("head")), as_object(review.get("base"))
    number = review.get("number")
    if (
        type(number) is not int or remote_head.get("sha") != head or remote_head.get("ref") != branch
        or remote_base.get("ref") != base or marker not in str(review.get("body", ""))
        or str(review.get("html_url", "")).lower() != f"https://github.com/{repo.repository}/pull/{number}"
    ):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PR", "PR 回读与预留的分支或提交不一致。")
    return review


def _publish(
    client: Client, project: str, args: argparse.Namespace, service: str, spec: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    if spec.get("status") != "open":
        raise CollabError("KNOWLEDGE_IMPROVEMENT_DELIVERY", "只有开放提案可以发布 PR。")
    local = checkout_remote(args.resources)
    repo = hint_repository(local or "")
    if repo is None or repo.provider != "github" or repo.repository != spec.get("repository") or local is None:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_REPOSITORY", "本地资源仓库与登记的 GitHub 目标不一致。")
    mapped = repository_plan(client, project, [local])[0]
    if (
        mapped.get("status") != "ready" or mapped.get("repository_id") != spec.get("repository_id")
        or mapped.get("connection_version") != spec.get("connection_version")
    ):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_REPOSITORY", "资源仓库连接版本已变化。")
    base = _sha(spec.get("base_commit"))
    if commits_in_checkout(args.resources, {base}) != {base}:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_BASE", "本地资源仓库缺少冻结提交。")
    headers = _authorization("github")
    env = _remote_env(headers)
    branch, base_ref = _ref(spec.get("branch")), _ref(spec.get("base_branch"))
    if branch != f"refs/heads/weora/self-improvement/{UUID(args.proposal).hex}" or branch == base_ref:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_BRANCH", "只能写入提案专用分支。")
    remote = f"https://github.com/{repo.repository}.git"
    previous = as_object(spec["previous"]) if spec.get("previous") is not None else {}
    current = as_object(spec["current"]) if spec.get("current") is not None else {}
    number = current.get("pr_number") or previous.get("pr_number")
    known = number if type(number) is int else None
    if known is not None:
        review = as_object(_request(repo, headers, f"/repos/{repo.repository}/pulls/{known}")[0])
        if (
            review.get("state") != "open" or as_object(review.get("head")).get("ref") != spec["branch"]
            or as_object(review.get("base")).get("ref") != spec["base_branch"]
            or str(spec["marker"]) not in str(review.get("body", ""))
        ):
            raise CollabError("KNOWLEDGE_IMPROVEMENT_PR", "已登记的 PR 已关闭或分支不同。")
    parent = _sha(previous.get("head_commit")) if previous else base
    with TemporaryDirectory(prefix="weora-improvement-deliver-") as temporary:
        root = Path(temporary) / "resources"
        _clone(args.resources, base, root)
        if parent != base:
            _git(root, env, "fetch", "-q", "--depth=1", "--no-tags", "--no-recurse-submodules",
                 "--", remote, parent)
        head, prefix = materialize(root, spec, parent, prefix=str(previous["prefix"]) if previous else None)
        default = _remote_ref(root, env, remote, base_ref)
        existing = _remote_ref(root, env, remote, branch)
        prior_head = parent if previous else None
        if default != base or existing not in (prior_head, head):
            raise CollabError("KNOWLEDGE_IMPROVEMENT_BASE", "默认分支或提案分支已前进，请先核对远端。")
        call(client, project, service, method="POST", suite=args.suite,
             payload={"revision": spec["revision"], "head_commit": head, "prefix": prefix})
        if existing != head:
            _git(root, env, "push", "--porcelain", f"--force-with-lease={branch}:{prior_head or ''}",
                 "--", remote, f"{head}:{branch}")
        if _remote_ref(root, env, remote, branch) != head:
            raise CollabError("KNOWLEDGE_IMPROVEMENT_GIT", "远端分支回读与预留提交不同。")

    review = _review(repo, headers, spec, head, known)
    result = call(client, project, service, method="PUT", suite=args.suite,
                  payload={"revision": spec["revision"], "pr_number": review["number"]})
    return result | {"pull_request": review["html_url"], "branch": spec["branch"]}


def publish_revision(
    client: Client, args: argparse.Namespace, receipt: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    project = args.project
    if project is None:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PROJECT_REQUIRED", "交付提案需要指定 --project。")
    try:
        identifier = str(UUID(str(receipt.get("proposal_id"))))
    except ValueError as error:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_DELIVERY", "草稿收据缺少有效提案标识。") from error
    path = f"/self-improvement/proposals/{identifier}/delivery"
    spec = call(client, project, path, suite=args.suite)
    # 自动交付只续做当时的修订，避免随后生成的新草稿被意外交付。
    if spec.get("revision") != receipt.get("revision"):
        return receipt | {"status": "superseded"}
    if spec.get("status") != "open":
        return receipt | {"status": spec.get("status")}
    current = spec.get("current")
    if isinstance(current, dict) and type(current.get("pr_number")) is int:
        return receipt | {"status": "delivered", "pr_number": current["pr_number"],
                          "head_commit": current.get("head_commit")}
    scoped = argparse.Namespace(**(vars(args) | {"proposal": identifier}))
    delivery = _publish(client, project, scoped, path, spec)
    return receipt | {"status": "delivered", "pr_number": delivery.get("pr_number"),
                      "head_commit": delivery.get("head_commit"), "delivery": delivery}


def execute(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    project = args.project
    if project is None:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PROJECT_REQUIRED", "交付提案需要指定 --project。")
    path = f"/self-improvement/proposals/{args.proposal}/delivery"
    spec = call(client, project, path, suite=args.suite)
    if args.improvement_action == "publish":
        return _publish(client, project, args, path, spec)
    current = as_object(spec.get("current"))
    number = current.get("pr_number")
    if type(number) is not int:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PR", "须先确认远端 PR 编号。")
    return call(client, project, path, method="PUT", suite=args.suite, payload={
        "revision": spec["revision"], "pr_number": number, "benchmark_id": args.benchmark,
    })
