"""由 GitHub 定期或手动启动一次可恢复的知识改进。"""

import argparse
import json
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid5

from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue
from weora_cli.knowledge_benchmark_executor import _env
from weora_cli.knowledge_improve_run import finish, recover
from weora_cli.knowledge_improvement_delivery import _git, _ref, _remote_env, _remote_ref, _sha
from weora_cli.knowledge_mr import _authorization, _request, hint_repository
from weora_cli.knowledge_repo_import import repository_plan

_WORKFLOW_RUN = re.compile(r"[1-9][0-9]{0,29}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+")


def execute(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    project = args.project
    if project is None:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PROJECT_REQUIRED", "定期改进需要指定 --project。")
    run = args.workflow_run or os.environ.get("GITHUB_RUN_ID", "")
    if not _WORKFLOW_RUN.fullmatch(run):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_SCHEDULE", "缺少有效的 GitHub Actions 运行编号。")
    if not _REPOSITORY.fullmatch(args.repository):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_REPOSITORY", "需要 GitHub 仓库标识 owner/repo。")
    repo = hint_repository(f"https://github.com/{args.repository}.git")
    if repo is None:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_REPOSITORY", "GitHub 仓库标识无效。")
    ref = _ref(args.base_branch)
    # GitHub 重跑会增加 attempt，run ID 保持不变，指向 PostgreSQL 中的同一份修订。
    identity = json.dumps(["github-improvement", repo.repository, args.suite, args.scorer, run])
    args.key = str(uuid5(UUID(project), identity))
    receipt = recover(client, args)
    if receipt.get("status") not in {"missing", "pending_delivery"}:
        return receipt

    remote = f"https://github.com/{repo.repository}.git"
    binding = repository_plan(client, project, [remote])[0]
    repository_id = binding.get("repository_id")
    if (
        binding.get("status") != "ready" or not isinstance(repository_id, str)
        or (receipt.get("status") == "pending_delivery"
            and repository_id != receipt.get("resource_repository_id"))
    ):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_REPOSITORY", "改进需要已登记且可用的项目资源仓库。")

    headers = _authorization("github")
    env = _remote_env(headers)
    metadata = as_object(_request(repo, headers, f"/repos/{repo.repository}")[0])
    if (
        str(metadata.get("full_name", "")).lower() != repo.repository
        or metadata.get("default_branch") != args.base_branch
    ):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_BRANCH", "调度目标必须是资源仓库的当前默认分支。")
    with TemporaryDirectory(prefix="weora-improvement-schedule-") as temporary:
        root = Path(temporary)
        _git(root, _env(git=True), "init", "-q", "--template=")
        base = _sha(_remote_ref(root, env, remote, ref))
        if receipt.get("status") == "pending_delivery" and receipt.get("base_commit") != base:
            raise CollabError("KNOWLEDGE_IMPROVEMENT_BASE", "默认分支已前进，请核对原草稿的固定基准。")
        # 只抓取冻结对象，生成器和发布器可复用它且不依赖调度机器的工作目录。
        _git(root, env, "fetch", "-q", "--depth=1", "--no-tags", "--no-recurse-submodules", "--", remote, base)
        _git(root, _env(git=True), "update-ref", "refs/weora/improvement/base", base)
        _git(root, _env(git=True), "remote", "add", "origin", remote)
        args.base_ref = base
        args.resources = root
        return finish(client, args, receipt)
