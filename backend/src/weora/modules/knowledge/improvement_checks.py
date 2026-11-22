"""把可信 PR 事件关联到同一提交下的完整基准和最新 CI 尝试。"""

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import select

from weora.modules.knowledge.benchmark import _lock_connection
from weora.modules.knowledge.benchmark_models import (
    KnowledgeImprovementCheck,
    KnowledgeImprovementDelivery,
    KnowledgeImprovementRevision,
    KnowledgeRegressionProposal,
    KnowledgeRegressionSuite,
)
from weora.modules.knowledge.benchmark_runs import BenchmarkPlanInput, _scoped_run, plan_run, report_run
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.improvement_delivery import (
    Target,
    _conflict,
    _context,
    _target,
    _verify_benchmark,
    confirm_delivery,
    latest_benchmark_check,
    matches_benchmark,
)
from weora.modules.knowledge.store import KnowledgeStore
from weora.modules.projects.service import ProjectService


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    proposal_id: UUID
    repository: str
    base_branch: str
    head_commit: str
    number: int


@dataclass(frozen=True, slots=True)
class CheckAttempt:
    operation: UUID
    workflow_run: str | None
    attempt: str | None


@dataclass(frozen=True, slots=True)
class CheckContext:
    suite: KnowledgeRegressionSuite
    proposal: KnowledgeRegressionProposal
    revision: KnowledgeImprovementRevision
    delivery: KnowledgeImprovementDelivery
    target: Target


async def _match(store: KnowledgeStore, projects: ProjectService, event: ReviewEvent) -> CheckContext:
    store.require_reviewer()
    suite, proposal = KnowledgeRegressionSuite, KnowledgeRegressionProposal
    path = await store.db.scalar(select(suite.path).join(proposal, proposal.suite_id == suite.id).where(
        proposal.id == event.proposal_id, suite.workspace_id == store.scope.workspace,
        suite.project_id == store.scope.project,
    ))
    if path is None:
        raise _conflict("当前项目找不到此 PR 对应的提案。")
    selected, current, revision = await _context(store, path, event.proposal_id, write=True)
    delivery = await store.db.get(KnowledgeImprovementDelivery, (current.id, revision.revision))
    if (
        delivery is None or delivery.head_commit != event.head_commit
        or delivery.pr_number not in (None, event.number)
    ):
        raise _conflict("PR 事件不再对应当前的交付提交。")
    target = await _target(store, projects, revision)
    if (event.repository, event.base_branch) != (target.repository, target.branch):
        raise _conflict("PR 事件的仓库或目标分支不匹配。")
    return CheckContext(selected, current, revision, delivery, target)


def _receipt(context: CheckContext, check: KnowledgeImprovementCheck, **state: object) -> dict[str, object]:
    return {
        "proposal_id": context.proposal.id, "revision": context.revision.revision, "suite": context.suite.path,
        "repository": context.target.repository, "head_commit": context.delivery.head_commit,
        "base_resources": context.revision.base_commit, "benchmark_id": check.benchmark_id, **state,
        "check_operation_id": check.operation_id if check.workflow_run is not None else None,
    }


async def plan_check(
    store: KnowledgeStore, projects: ProjectService, event: ReviewEvent, attempt: CheckAttempt,
) -> dict[str, object]:
    context = await _match(store, projects, event)
    suite, revision = context.suite, context.revision
    check = KnowledgeImprovementCheck
    operation = attempt.operation
    replay = await store.db.scalar(select(check).where(
        check.proposal_id == event.proposal_id, check.revision == revision.revision, check.operation_id == operation,
    ))
    active = await latest_benchmark_check(store, event.proposal_id, revision.revision)
    if replay is not None and active is not None and replay.generation != active.generation:
        return _receipt(context, replay, check_state="superseded")
    if replay is None and attempt.workflow_run is not None:
        family = await store.db.scalar(select(check).where(
            check.proposal_id == event.proposal_id, check.revision == revision.revision,
            check.workflow_run == attempt.workflow_run,
        ).order_by(check.generation.desc()).limit(1))
        if family is not None and (
            active is None or active.generation != family.generation
            or int(attempt.attempt or "0") <= int(family.attempt or "0")
        ):
            return _receipt(context, family, check_state="superseded")
    if context.proposal.status != "open":
        raise _conflict("只有开放 PR 可以登记基准。")

    # 保持与其他同时访问代码库和资源库的用例相同的加锁顺序。
    for repository in sorted({suite.repository_id, revision.resource_repository_id}):
        await _lock_connection(store, projects, repository)
    # GitHub 的事件可能早于 CLI 收据。以托管端回读完成原预留后，再创建基准。
    await confirm_delivery(store, projects, suite.path, event.proposal_id, revision.revision, event.number, None)
    if replay is not None:
        identity = (attempt.workflow_run, attempt.attempt)
        if replay.workflow_run is not None and (replay.workflow_run, replay.attempt) != identity:
            raise _conflict("同一检查标识已登记为另一项工作流尝试。")
        result = await report_run(store, replay.benchmark_id)
        return _receipt(context, replay, check_state="pending", benchmark_status=result["status"])
    run = None
    if active is not None:
        # 旧尝试的写入与换手按同一基准行串行处理。已登记的判定可以保留。
        await _scoped_run(store, active.benchmark_id, write=True)
        if attempt.workflow_run is not None and active.workflow_run == attempt.workflow_run:
            prior = await report_run(store, active.benchmark_id)
            outcomes = {row["outcome"] for row in cast(list[dict[str, object]], prior["trials"])}
            resumable = prior["status"] == "pending" and outcomes <= {"not_run", "passed", "failed"}
            reusable = prior["status"] == "complete" and prior["gate"] in ("held", "regressed")
            if (resumable or reusable) and await matches_benchmark(
                store, suite, context.proposal, revision, context.delivery, prior,
            ):
                run = prior
    if run is None:
        run = await plan_run(store, projects, suite.path, BenchmarkPlanInput(
            operation, event.proposal_id, revision.resource_repository_id,
            revision.base_commit, context.delivery.head_commit, (),
        ))
    row = check(
        proposal_id=event.proposal_id, revision=revision.revision, operation_id=operation,
        generation=(active.generation if active else 0) + 1, benchmark_id=UUID(str(run["id"])),
        workflow_run=attempt.workflow_run, attempt=attempt.attempt,
    )
    store.db.add(row)
    return _receipt(context, row, check_state="pending", benchmark_status=run["status"])


async def check_result(
    store: KnowledgeStore, projects: ProjectService, event: ReviewEvent, operation: UUID,
) -> dict[str, object]:
    context = await _match(store, projects, event)
    active = await latest_benchmark_check(store, event.proposal_id, context.revision.revision)
    if active is None:
        raise _conflict("PR 尚未登记基准检查。")
    if active.operation_id != operation or context.proposal.status != "open":
        return _receipt(context, active, check_state="superseded")
    # 再读 PR 的分支及文件，运行中变更不能成为当前提交的通过证据。
    try:
        await confirm_delivery(
            store, projects, context.suite.path, event.proposal_id, context.revision.revision, event.number, None,
        )
    except KnowledgeError as error:
        return _receipt(context, active, check_state="error", reason_code=error.code)
    result = await report_run(store, active.benchmark_id)
    state = "failure" if result["gate"] == "regressed" else "error"
    if result["gate"] == "held":
        try:
            await _verify_benchmark(
                store, context.suite, context.proposal, context.revision, context.delivery, active.benchmark_id,
            )
        except KnowledgeError as error:
            return _receipt(context, active, check_state="error", reason_code=error.code)
        state = "success"
    return _receipt(
        context, active, check_state=state, gate=result["gate"],
        benchmark_status=result["status"], comparisons=result["comparisons"],
    )


async def merge_check(store: KnowledgeStore, projects: ProjectService, event: ReviewEvent) -> dict[str, object]:
    context = await _match(store, projects, event)
    active = await latest_benchmark_check(store, event.proposal_id, context.revision.revision)
    if active is None:
        raise _conflict("当前提交没有 CI 基准结果。")
    return await confirm_delivery(
        store, projects, context.suite.path, event.proposal_id, context.revision.revision,
        event.number, active.benchmark_id,
    )
