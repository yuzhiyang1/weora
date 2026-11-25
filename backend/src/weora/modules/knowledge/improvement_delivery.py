"""固定 GitHub 交付约定，并在托管事实和基准门禁都通过后确认发布。"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import product
from typing import Literal, cast
from urllib.parse import quote
from uuid import UUID

import aiohttp
from sqlalchemy import select

from weora.modules.knowledge.benchmark import (
    _active_tasks,
    _lock_connection,
    _project,
    _proposal,
    _suite,
    _timestamp,
    settle_proposal,
)
from weora.modules.knowledge.benchmark_models import (
    AssetEdit,
    KnowledgeImprovementCheck,
    KnowledgeImprovementDelivery,
    KnowledgeImprovementRevision,
    KnowledgeRegressionProposal,
    KnowledgeRegressionSuite,
    KnowledgeRegressionTask,
)
from weora.modules.knowledge.benchmark_runs import ARMS, report_run
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.improvement_proposals import _latest
from weora.modules.knowledge.review_source import registered_repo
from weora.modules.knowledge.store import KnowledgeStore
from weora.modules.projects.domain import ProjectActor
from weora.modules.projects.service import ProjectService

type Prefix = Literal["", ".teamai/"]


@dataclass(frozen=True, slots=True)
class Target:
    repository: str
    branch: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Reservation:
    revision: int
    head_commit: str
    prefix: Prefix


def _branch(identifier: UUID) -> str:
    return f"weora/self-improvement/{identifier.hex}"


def _marker(identifier: UUID) -> str:
    return f"<!-- weora-improvement:{identifier} -->"


def _conflict(message: str) -> KnowledgeError:
    return KnowledgeError("KNOWLEDGE_IMPROVEMENT_DELIVERY", message, 409)


def _view(row: KnowledgeImprovementDelivery) -> dict[str, object]:
    return {
        "revision": row.revision, "head_commit": row.head_commit, "prefix": row.prefix, "pr_number": row.pr_number,
        "verified_at": _timestamp(row.verified_at), "merge_commit": row.merge_commit, "benchmark_id": row.benchmark_id,
    }


async def _context(
    store: KnowledgeStore, path: str, identifier: UUID, *, write: bool,
) -> tuple[KnowledgeRegressionSuite, KnowledgeRegressionProposal, KnowledgeImprovementRevision]:
    store.require_reviewer()
    suite = await _suite(store, path, lock="write" if write else "read")
    assert suite is not None
    proposal = await _proposal(store, suite, identifier)
    revision = await _latest(store, proposal.id)
    if revision is None:
        raise _conflict("当前提案没有生成的资源差异。")
    return suite, proposal, revision


async def _target(
    store: KnowledgeStore, projects: ProjectService, revision: KnowledgeImprovementRevision,
) -> Target:
    if dict(store.scope.code_connections).get(revision.resource_repository_id) != revision.connection_version:
        raise _conflict("原资源仓库连接已变化。")
    source = await projects._source(
        ProjectActor(store.scope.user, store.scope.workspace), _project(store),
        revision.resource_repository_id, revision.connection_version,
    )
    key = registered_repo(source.git_url)
    if key is None or key[:2] != ("github.com", None) or key[2].count("/") != 1:
        raise _conflict("自动交付需要登记在项目中的 GitHub 资源仓库。")
    return Target(key[2], source.branch, source.token)


async def _manifest(
    store: KnowledgeStore, suite: KnowledgeRegressionSuite, revision: KnowledgeImprovementRevision,
) -> list[AssetEdit]:
    task = KnowledgeRegressionTask
    tasks = await store.db.scalars(select(task).where(
        task.suite_id == suite.id, task.proposal_id == revision.proposal_id,
    ).order_by(task.name))
    changes = list(revision.changes)
    for row in tasks:
        # JSON 是有效的 YAML 子集，远端校验可按同一确定性序列计算 Git blob。
        content = json.dumps({
            "schemaVersion": "v1alpha1", "title": row.title, "prompt": row.prompt,
            "startingCommit": row.starting_commit, "successCriteria": row.success_criteria,
        }, ensure_ascii=False, indent=2) + "\n"
        changes.append({"path": f"{suite.path}/tasks/{row.name}.yaml", "before_sha256": None, "content": content})
    if len(changes) > 250:
        raise _conflict("单次 PR 文件数超过交付上限。")
    return changes


def _digest(entries: list[AssetEdit]) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def delivery_spec(
    store: KnowledgeStore, projects: ProjectService, path: str, identifier: UUID,
) -> dict[str, object]:
    suite, proposal, newest = await _context(store, path, identifier, write=False)
    target = await _target(store, projects, newest)
    model = KnowledgeImprovementDelivery
    rows = await store.db.scalars(select(model).where(
        model.proposal_id == identifier, model.revision <= newest.revision,
    ).order_by(model.revision.desc()).limit(2))
    current = None
    previous = None
    for row in rows:
        if row.revision == newest.revision:
            current = _view(row)
        elif previous is None:
            previous = _view(row)
    return {
        "proposal_id": identifier, "status": proposal.status, "revision": newest.revision,
        "repository_id": newest.resource_repository_id, "connection_version": newest.connection_version,
        "repository": target.repository, "base_branch": target.branch, "base_commit": newest.base_commit,
        "branch": _branch(identifier), "marker": _marker(identifier), "created_at": _timestamp(newest.created_at),
        "scorer": proposal.scorer, "files": await _manifest(store, suite, newest),
        "current": current, "previous": previous,
    }


async def reserve_delivery(
    store: KnowledgeStore, projects: ProjectService, path: str, identifier: UUID, reservation: Reservation,
) -> dict[str, object]:
    suite, proposal, newest = await _context(store, path, identifier, write=True)
    if newest.revision != reservation.revision or proposal.status != "open":
        raise _conflict("提案状态或版本已变化。")
    if reservation.head_commit == newest.base_commit:
        raise _conflict("空提交不能创建提案交付。")
    await _lock_connection(store, projects, newest.resource_repository_id)
    model = KnowledgeImprovementDelivery
    before = await store.db.scalar(select(model).where(
        model.proposal_id == identifier, model.revision < newest.revision,
    ).order_by(model.revision.desc()).limit(1))
    if before and before.pr_number is None:
        raise _conflict("须先恢复上一次尚未确认的远端交付。")
    if before and before.prefix != reservation.prefix:
        raise _conflict("同一提案的资源目录已变化。")
    manifest = _digest(await _manifest(store, suite, newest))
    delivery = await store.db.get(model, (identifier, newest.revision))
    if delivery is not None:
        if (delivery.head_commit, delivery.prefix, delivery.manifest_digest) != (
            reservation.head_commit, reservation.prefix, manifest,
        ):
            raise _conflict("此修订已经预留了其他提交或文件。")
        return _view(delivery)
    delivery = model(
        proposal_id=identifier, revision=newest.revision, head_commit=reservation.head_commit,
        manifest_digest=manifest, prefix=reservation.prefix, reserved_by=store.scope.user,
        reserved_at=datetime.now(UTC),
    )
    store.db.add(delivery)
    return _view(delivery)


async def _get_review(target: Target, suffix: str) -> object:
    url = f"https://api.github.com/repos/{target.repository}/{suffix}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Weora"}
    if target.token:
        headers["Authorization"] = f"Bearer {target.token}"
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15), trust_env=False, headers=headers,
            ) as session,
            session.get(url, allow_redirects=False) as response,
        ):
            if response.status != 200:
                raise _conflict("无法读取登记仓库的 PR，请核对项目连接权限。")
            raw = bytearray()
            async for part in response.content.iter_chunked(65536):
                raw.extend(part)
                if len(raw) > 2_000_000:
                    raise _conflict("PR 响应超过可核验范围。")
            return cast(object, json.loads(raw))
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_REMOTE", "无法验证远端 PR。", 502) from error


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _conflict("托管平台返回了无法核验的记录。")
    return cast(dict[str, object], value)


def _blob(content: str) -> str:
    data = content.encode()
    # GitHub 的文件 SHA 是包含 Git 对象头的 SHA-1，而不是内容摘要。
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()


async def _verify_review(
    target: Target, delivery: KnowledgeImprovementDelivery, number: int, files: list[AssetEdit],
    *, merged: bool, base_commit: str,
) -> dict[str, object]:
    identifier = delivery.proposal_id
    pr = _object(await _get_review(target, f"pulls/{number}"))
    head, base = _object(pr.get("head")), _object(pr.get("base"))
    if (
        pr.get("number") != number or
        str(pr.get("html_url", "")).lower() != f"https://github.com/{target.repository}/pull/{number}"
        or head.get("ref") != _branch(identifier) or head.get("sha") != delivery.head_commit
        or base.get("ref") != target.branch
        or str(_object(head.get("repo")).get("full_name", "")).lower() != target.repository
        or str(_object(base.get("repo")).get("full_name", "")).lower() != target.repository
        or _marker(identifier) not in str(pr.get("body", ""))
        or (pr.get("merged") is not True if merged else pr.get("state") != "open")
    ):
        raise _conflict("远端 PR 的仓库、分支、提交或状态与提案不一致。")
    if not merged:
        reference = _object(await _get_review(target, f"git/ref/heads/{quote(target.branch, safe='/')}"))
        if _object(reference.get("object")).get("sha") != base_commit:
            raise _conflict("远端目标分支已前进，须重新生成或核验资源基准。")
    expected = {delivery.prefix + change["path"]: change["content"] for change in files}
    for page in range(1, 5):
        value = await _get_review(target, f"pulls/{number}/files?per_page=100&page={page}")
        if not isinstance(value, list):
            raise _conflict("无法核对 PR 文件清单。")
        for raw in value:
            item = _object(raw)
            path = item.get("filename")
            if not isinstance(path, str) or path not in expected:
                raise _conflict("PR 含未批准的文件。")
            content = expected.pop(path)
            if (item.get("status") != "removed" if content is None else
                item.get("status") not in {"added", "modified"} or item.get("sha") != _blob(content)):
                raise _conflict("PR 文件内容与冻结的提案不一致。")
        if len(value) < 100:
            break
    if expected:
        raise _conflict("PR 缺少提案中的文件。")
    return pr


async def latest_benchmark_check(
    store: KnowledgeStore, proposal_id: UUID, revision: int,
) -> KnowledgeImprovementCheck | None:
    check = KnowledgeImprovementCheck
    row: KnowledgeImprovementCheck | None = await store.db.scalar(select(check).where(
        check.proposal_id == proposal_id, check.revision == revision,
    ).order_by(check.generation.desc()).limit(1))
    return row


async def confirm_delivery(
    store: KnowledgeStore, projects: ProjectService, path: str, identifier: UUID, revision: int,
    number: int, benchmark: UUID | None,
) -> dict[str, object]:
    suite, proposal, newest = await _context(store, path, identifier, write=True)
    delivery = await store.db.get(KnowledgeImprovementDelivery, (identifier, revision))
    if delivery is None or newest.revision != revision:
        raise _conflict("提案未预留此修订，或其版本已变化。")
    if delivery.merge_commit:
        if number == delivery.pr_number and benchmark == delivery.benchmark_id:
            return _view(delivery)
        raise _conflict("提案已由另一结果确认。")
    if proposal.status != "open":
        raise _conflict("只有开放提案可以确认交付。")
    previous = await store.db.scalar(select(KnowledgeImprovementDelivery.pr_number).where(
        KnowledgeImprovementDelivery.proposal_id == identifier, KnowledgeImprovementDelivery.revision < revision,
    ).order_by(KnowledgeImprovementDelivery.revision.desc()).limit(1))
    if any(value is not None and value != number for value in (previous, delivery.pr_number)):
        raise _conflict("同一提案须继续使用已登记的 PR。")
    files = await _manifest(store, suite, newest)
    if delivery.manifest_digest != _digest(files):
        raise _conflict("预留后任务或资源有变化。")
    await _lock_connection(store, projects, newest.resource_repository_id)
    target = await _target(store, projects, newest)
    if benchmark is not None:
        active = await latest_benchmark_check(store, identifier, revision)
        if active is not None and active.benchmark_id != benchmark:
            raise _conflict("合并须使用当前提交最新的 CI 基准。")
        await _verify_benchmark(store, suite, proposal, newest, delivery, benchmark)
    pr = await _verify_review(
        target, delivery, number, files, merged=benchmark is not None, base_commit=newest.base_commit,
    )
    if delivery.pr_number is None:
        delivery.pr_number, delivery.verified_at = number, datetime.now(UTC)
    if benchmark is not None:
        merge = pr.get("merge_commit_sha")
        if not isinstance(merge, str) or len(merge) != 40 or any(c not in "0123456789abcdef" for c in merge):
            raise _conflict("缺少可核验的合并提交。")
        await settle_proposal(store, path, identifier, "merged", verified_delivery=True)
        delivery.merge_commit, delivery.benchmark_id = merge, benchmark
    return _view(delivery)


async def _verify_benchmark(
    store: KnowledgeStore, suite: KnowledgeRegressionSuite, proposal: KnowledgeRegressionProposal,
    newest: KnowledgeImprovementRevision, delivery: KnowledgeImprovementDelivery, benchmark: UUID,
) -> None:
    result = await report_run(store, benchmark)
    if (
        result["gate"] != "held" or result["status"] != "complete"
        or not await matches_benchmark(store, suite, proposal, newest, delivery, result)
    ):
        raise _conflict("须提供通过门禁且覆盖本提案提交和完整套件矩阵的运行。")


async def matches_benchmark(
    store: KnowledgeStore, suite: KnowledgeRegressionSuite, proposal: KnowledgeRegressionProposal,
    newest: KnowledgeImprovementRevision, delivery: KnowledgeImprovementDelivery, result: dict[str, object],
) -> bool:
    """发布和续跑共用同一份提交、仓库连接和完整矩阵校验。"""
    if (
        result["proposal_id"] != proposal.id
        or result["suite"] != suite.path or result["resource_repository_id"] != newest.resource_repository_id
        or result["resource_connection_version"] != newest.connection_version
        or result["base_resources"] != newest.base_commit or result["proposal_resources"] != delivery.head_commit
        or result["connection_version"] != dict(store.scope.code_connections).get(suite.repository_id)
    ):
        return False
    active = (await store.db.execute(_active_tasks(suite, proposal))).tuples()
    task_ids = {task.id for task, _ in active}
    trials = cast(list[dict[str, object]], result["trials"])
    expected = set(product(task_ids, suite.tools, ARMS, range(1, suite.repetitions + 1)))
    return {(row["task_id"], row["tool"], row["arm"], row["repetition"]) for row in trials} == expected
