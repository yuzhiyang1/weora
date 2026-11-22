"""从 GitHub 回读确切的工作流尝试，上报完成结论和耗时。"""

import argparse
import json
import os
import re
from pathlib import Path

from weora_cli.collab_client import Client, CollabError, as_object, items
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_mr import _authorization, _request, hint_repository, merged_date
from weora_cli.knowledge_repo_import import repository_plan

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_RUN = re.compile(r"[1-9][0-9]{0,19}")
_WORKFLOWS = {
    ".github/workflows/github-knowledge-review.yml": ("knowledge-review", {"pull_request_target"}),
    ".github/workflows/github-self-improvement-benchmark.yml": ("improvement-benchmark", {"pull_request_target"}),
    ".github/workflows/github-self-improvement-schedule.yml": ("self-improvement", {"schedule", "workflow_dispatch"}),
}
_CONCLUSIONS = {
    "success": "succeeded", "failure": "failed", "timed_out": "failed", "startup_failure": "failed",
    "action_required": "failed", "skipped": "skipped", "neutral": "skipped", "cancelled": "cancelled",
    "stale": "cancelled",
}


def _event_identity(path: Path) -> tuple[JsonValue, JsonValue, JsonValue]:
    try:
        with path.open(encoding="utf-8-sig") as handle:
            raw = handle.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("event too large")
        event = as_object(json.loads(raw))
        run = as_object(event.get("workflow_run"))
        if event.get("action") != "completed":
            raise ValueError("not a completion event")
        return as_object(event.get("repository")).get("full_name"), run.get("id"), run.get("run_attempt")
    except (OSError, ValueError, RecursionError, CollabError) as exc:
        raise CollabError("KNOWLEDGE_WORKFLOW_EVENT", "无法读取 GitHub 工作流完成事件。") from exc


def _duration(jobs: list[dict[str, JsonValue]]) -> int:
    """按第一个作业开始到最后一个作业结束计算耗时，并行作业只占一段墙钟时间。"""
    intervals = [
        (start, end) for job in jobs if job.get("conclusion") != "skipped"
        and (start := merged_date(job.get("started_at"))) is not None
        and (end := merged_date(job.get("completed_at"))) is not None
        and end >= start
    ]
    if not intervals:
        return 0
    elapsed = max(end for _, end in intervals) - min(start for start, _ in intervals)
    return (elapsed.days * 86400 + elapsed.seconds) * 1000 + elapsed.microseconds // 1000


def execute(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    project: str | None = args.project
    if project is None:
        raise CollabError("KNOWLEDGE_WORKFLOW_PROJECT", "工作流统计需要指定 --project。")
    if args.event is not None:
        if args.attempt is not None:
            raise CollabError("KNOWLEDGE_WORKFLOW_EVENT", "--event 已包含尝试序号。")
        repository, run, attempt = _event_identity(args.event)
    else:
        repository, run, attempt = args.repository, args.run, args.attempt
    if (
        not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository)
        or not _RUN.fullmatch(str(run)) or type(attempt) is not int or not 1 <= attempt <= 2147483647
    ):
        raise CollabError("KNOWLEDGE_WORKFLOW_EVENT", "请提供有效的仓库、运行编号和尝试序号。")
    target = hint_repository(f"https://github.com/{repository}.git")
    configured = (args.repository, os.environ.get("GITHUB_REPOSITORY"))
    if target is None or any(name and name.lower() != target.repository for name in configured):
        raise CollabError("KNOWLEDGE_WORKFLOW_EVENT", "运行事件的仓库与当前仓库不符。")

    if call(client, None, "/settings/usage-report").get("enabled") is not True:
        return {"status": "disabled"}
    authorization = _authorization("github")
    path = f"/repos/{target.repository}/actions/runs/{run}/attempts/{attempt}"
    detail = as_object(_request(target, authorization, path)[0])
    if (
        str(detail.get("id")) != str(run) or detail.get("run_attempt") != attempt
        or str(as_object(detail.get("repository")).get("full_name", "")).lower() != target.repository
        or detail.get("status") != "completed"
    ):
        raise CollabError("KNOWLEDGE_WORKFLOW_EVENT", "GitHub 回读的工作流终态与请求不符。")
    workflow_path = str(detail.get("path", "")).split("@", 1)[0]
    tracked = _WORKFLOWS.get(workflow_path)
    trigger = detail.get("event")
    if tracked is None or not isinstance(trigger, str) or trigger not in tracked[1]:
        return {"status": "ignored"}
    workflow, _ = tracked
    outcome = _CONCLUSIONS.get(str(detail.get("conclusion")))
    if outcome is None:
        raise CollabError("KNOWLEDGE_WORKFLOW_EVENT", "GitHub 回读缺少可识别的工作流结论。")
    duration = 0
    if outcome != "skipped":
        result = as_object(_request(target, authorization, path + "/jobs", params={"per_page": "100"})[0])
        jobs = items({"items": result.get("jobs")})
        if (
            type(result.get("total_count")) is not int or result["total_count"] != len(jobs)
            or any(str(job.get("run_id")) != str(run) or job.get("status") != "completed" for job in jobs)
        ):
            raise CollabError("KNOWLEDGE_WORKFLOW_EVENT", "GitHub 回读的工作流作业不完整。")
        if outcome == "succeeded" and all(job.get("conclusion") == "skipped" for job in jobs):
            # GitHub 会把条件为假的整组作业标为 skipped，但外层运行仍可能为 success。
            outcome = "skipped"
        else:
            duration = _duration(jobs)

    source = f"https://github.com/{target.repository}.git"
    binding = repository_plan(client, project, [source])[0]
    identifier, version = binding.get("repository_id"), binding.get("connection_version")
    if binding.get("status") != "ready" or not isinstance(identifier, str) or type(version) is not int:
        raise CollabError("KNOWLEDGE_WORKFLOW_REPOSITORY", "当前项目未登记此 GitHub 仓库。")
    try:
        return call(client, project, "/activity/workflows", method="PUT", payload={
            "repository_id": identifier, "connection_version": version, "repository": target.repository,
            "run_id": str(run), "attempt": attempt, "workflow": workflow, "outcome": outcome, "duration_ms": duration,
        })
    except CollabError as exc:
        if exc.code == "KNOWLEDGE_USAGE_DISABLED":
            return {"status": "disabled"}
        raise
