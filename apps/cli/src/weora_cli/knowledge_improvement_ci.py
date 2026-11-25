"""从 GitHub 活动提案事件启动完整基准，并把最新结论写到固定提交。"""

import argparse
import json
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid5

from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_benchmark_executor import Sandbox, _env
from weora_cli.knowledge_benchmark_executor import execute as execute_run
from weora_cli.knowledge_improvement_delivery import _git, _remote_env, _sha
from weora_cli.knowledge_mr import ReviewRepository, _authorization, _request

_BRANCH = re.compile(r"weora/self-improvement/([a-f0-9]{32})")
_ATTEMPT = re.compile(r"[1-9][0-9]{0,29}")
_REPO = re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+")
_CONTEXT = "weora/knowledge-regression"
_DESCRIPTIONS = {
    "pending": "Weora benchmark in progress",
    "success": "Complete Weora matrix passed",
    "failure": "Weora benchmark detected a regression",
    "error": "Weora benchmark incomplete or unverified",
}


def _event(path: Path) -> tuple[str, dict[str, JsonValue]]:
    try:
        with path.open(encoding="utf-8-sig") as file:
            raw = file.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("event too large")
        document = as_object(json.loads(raw))
        pr = as_object(document.get("pull_request"))
        head, base = as_object(pr.get("head")), as_object(pr.get("base"))
        branch = _BRANCH.fullmatch(str(head.get("ref", "")))
        action = document.get("action")
        if not branch or action not in ("opened", "reopened", "synchronize", "closed"):
            return "skip", {}
        if action == "closed" and pr.get("merged") is not True:
            return "skip", {}
        registered = str(as_object(document.get("repository")).get("full_name", "")).lower()
        if not _REPO.fullmatch(registered) or any(
            str(as_object(side.get("repo")).get("full_name", "")).lower() != registered for side in (head, base)
        ):
            return "skip", {}
        number, base_branch = pr.get("number"), base.get("ref")
        if type(number) is not int or number < 1 or not isinstance(base_branch, str) or not base_branch:
            raise ValueError("invalid PR identity")
        return str(action), {
            "proposal_id": str(UUID(hex=branch[1])), "repository": registered, "pr_number": number,
            "head_commit": _sha(head.get("sha")), "base_branch": base_branch,
        }
    except (OSError, ValueError, RecursionError, CollabError) as error:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_CI_EVENT", "GitHub PR 事件无法核验。") from error


def _attempt(args: argparse.Namespace, event: dict[str, JsonValue]) -> tuple[str, str, str]:
    workflow = str(args.workflow_run or os.environ.get("GITHUB_RUN_ID", ""))
    attempt = str(args.attempt or os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    if not _ATTEMPT.fullmatch(workflow) or not _ATTEMPT.fullmatch(attempt):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_CI_ATTEMPT", "CI 需要 GitHub 运行号和尝试序号。")
    key = f"github:{event['pr_number']}:{event['head_commit']}:{workflow}:{attempt}"
    return str(uuid5(UUID(str(event["proposal_id"])), key)), workflow, attempt


def _status(repo: ReviewRepository, headers: dict[str, str], head: str, state: str, run_url: str) -> None:
    _request(repo, headers, f"/repos/{repo.repository}/statuses/{head}", method="POST", payload={
        "context": _CONTEXT, "state": state, "description": _DESCRIPTIONS[state], "target_url": run_url,
    })


def _fetch_resources(
    root: Path, repo: ReviewRepository, headers: dict[str, str], base: str, head: str,
) -> None:
    """只取两条固定提交到临时 Git 库，PR 内容从不进入 CI Runner 的工作目录。"""
    remote = f"https://github.com/{repo.repository}.git"
    isolated = _env(git=True)
    _git(root, isolated, "init", "-q", "--template=")
    authenticated = _remote_env(headers)
    for arm, commit in (("main", base), ("pulled", head)):
        _git(root, authenticated, "fetch", "-q", "--no-tags", "--depth=1", "--no-recurse-submodules",
             "--", remote, commit)
        _git(root, isolated, "update-ref", f"refs/weora/ci/{arm}", commit)
    _git(root, isolated, "remote", "add", "origin", remote)


def execute(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    project = args.project
    if project is None:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PROJECT_REQUIRED", "CI 基准需要指定 --project。")
    action, event = _event(args.event)
    if action == "skip":
        return {"check_state": "skipped"}
    if action == "closed":
        result = call(client, project, "/self-improvement/ci/merge", method="POST", payload=event)
        return result | {"check_state": "merged"}

    operation, workflow, attempt = _attempt(args, event)
    request = event | {"operation_id": operation, "workflow_run": workflow, "attempt": attempt}
    planned = call(client, project, "/self-improvement/ci/plan", method="POST", payload=request)
    if planned.get("check_state") == "superseded":
        return planned
    repository, commit = str(event["repository"]), _sha(event["head_commit"])
    if planned.get("repository") != repository or planned.get("head_commit") != commit:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_CI_EVENT", "平台返回的运行与 PR 事件不符。")
    args.run = str(UUID(str(planned["benchmark_id"])))
    write_attempt = planned.get("check_operation_id")
    if write_attempt is not None and write_attempt != operation:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_CI_GATE", "平台返回的结算身份与当前尝试不一致。")
    args.check_operation_id = write_attempt
    repo = ReviewRepository("github", "https://api.github.com", repository)
    headers = _authorization("github")
    run_url = f"https://github.com/{repository}/actions/runs/{workflow}"
    failure: CollabError | None = None
    if planned.get("benchmark_status") not in ("complete", "expired"):
        if args.checkout is None or args.image is None or args.judge_model is None:
            raise CollabError("KNOWLEDGE_IMPROVEMENT_CI_INPUTS", "基准执行需要项目检出、镜像和评判模型。")
        _status(repo, headers, commit, "pending", run_url)
        try:
            sandbox = Sandbox.prepare(args.image, args.network, args.gateway)
            with TemporaryDirectory(prefix="weora-ci-resources-") as temporary:
                args.resources = Path(temporary)
                _fetch_resources(args.resources, repo, headers, _sha(planned.get("base_resources")), commit)
                execute_run(client, project, args, sandbox=sandbox)
        except CollabError as error:
            failure = error
        except OSError:
            failure = CollabError("KNOWLEDGE_IMPROVEMENT_CI_LOCAL", "无法准备 CI 临时资源检出。")
    result = call(client, project, "/self-improvement/ci/result", method="POST", payload=request)
    state = result.get("check_state")
    if state == "superseded":
        return result
    if not isinstance(state, str) or state not in ("success", "failure", "error"):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_CI_GATE", "平台未返回可用合并检查。")
    if result.get("head_commit") != commit or result.get("benchmark_id") != args.run:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_CI_GATE", "合并检查与已启动的基准不一致。")
    # 状态只写入已核验的 SHA。旧尝试退出时不写覆盖状态。
    _status(repo, headers, commit, state, run_url)
    if failure is not None and state == "error":
        return result | {"execution_error": failure.code}
    return result
