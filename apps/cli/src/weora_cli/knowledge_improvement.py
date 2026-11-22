"""以当前检出的 Git 对象确认评分失败是否能复现。"""

from pathlib import Path
from typing import Literal

from weora_cli.collab_client import Client, CollabError, as_object, items
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_benchmark import DEFAULT_SUITE
from weora_cli.knowledge_repo_import import repository_plan
from weora_cli.knowledge_session_origin import checkout_remote, commits_in_checkout


def _why(latest: dict[str, JsonValue]) -> Literal["tracked_changes", "not_recorded", "not_in_checkout"]:
    status = as_object(latest.get("git_start")).get("status")
    if status == "tracked_changes":
        return "tracked_changes"
    if status == "not_recorded":
        return "not_recorded"
    return "not_in_checkout"


def plan(
    client: Client, project: str | None, scorer: str, cwd: Path, *, suite: str | None = None,
) -> dict[str, JsonValue]:
    if project is None:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PROJECT_REQUIRED", "回归计划需要指定 --project。")
    endpoint = "/self-improvement/candidates"
    evidence = (
        call(client, project, endpoint, scorer=scorer, suite=suite)
        if suite is not None else call(client, project, endpoint, scorer=scorer)
    )
    repository = evidence.get("repository_id")
    scope = None
    remote = checkout_remote(cwd)
    if remote:
        matched = repository_plan(client, project, [remote])[0]
        if matched.get("status") == "ready" and (repository is None or matched.get("repository_id") == repository):
            scope = matched.get("repository_id"), matched.get("connection_version")

    subjects = items({"items": evidence.get("subjects")})
    possible: dict[str, tuple[str, dict[str, JsonValue]]] = {}
    for subject in subjects:
        identifier = subject.get("subject_id")
        if not isinstance(identifier, str):
            raise CollabError("KNOWLEDGE_IMPROVEMENT_EVIDENCE", "回归候选缺少会话标识。")
        recorded = subject.get("recorded")
        if subject.get("task") or scope is None or not isinstance(recorded, dict):
            continue
        start = as_object(recorded.get("git_start"))
        commit = start.get("commit")
        if (
            isinstance(commit, str) and start.get("status") == "recorded"
            and (start.get("repository_id"), recorded.get("connection_version")) == scope
        ):
            possible[identifier] = (commit, recorded)

    confirmed = commits_in_checkout(cwd, {commit for commit, _ in possible.values()})
    failures: list[JsonValue] = []
    refused: list[JsonValue] = []
    covered: dict[str, None] = {}
    for subject in subjects:
        task = subject.get("task")
        if isinstance(task, str):
            covered.setdefault(task, None)
            continue
        identifier = str(subject["subject_id"])
        candidate = possible.get(identifier)
        if candidate and candidate[0] in confirmed:
            failures.append(candidate[1] | {"subject_id": identifier, "learnings": subject.get("learnings")})
        else:
            refused.append({"subject_id": identifier, "why": _why(as_object(subject.get("latest")))})
    has_proposal = isinstance(evidence.get("proposal_id"), str)
    return {
        "scorer": evidence.get("scorer"), "scorer_id": evidence.get("scorer_id"), "suite": suite or DEFAULT_SUITE,
        "proposal_id": evidence.get("proposal_id"), "hasProposal": has_proposal,
        "failures": failures, "covered": list(covered), "refused": refused,
        "ready": repository is not None and len(failures) >= (1 if has_proposal else 2),
    }
