"""冻结基准运行的 A/B 矩阵，并按同一任务和工具配对记录。"""

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from itertools import product
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import Select, select

from weora.modules.knowledge.benchmark import _active_tasks, _lock_connection, _proposal, _suite, _timestamp
from weora.modules.knowledge.benchmark_models import (
    BenchmarkArm,
    BenchmarkExecutionBinding,
    BenchmarkExecutionProfile,
    BenchmarkTool,
    KnowledgeImprovementCheck,
    KnowledgeRegressionRun,
    KnowledgeRegressionSuite,
    KnowledgeRegressionTask,
    KnowledgeRegressionTrial,
    TrialOutcome,
)
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.store import KnowledgeStore
from weora.modules.projects.service import ProjectService

MAX_TRIALS = 2000
ARMS: tuple[BenchmarkArm, BenchmarkArm] = ("main", "pulled")


@dataclass(frozen=True, slots=True)
class BenchmarkPlanInput:
    operation_id: UUID
    proposal_id: UUID | None
    resource_repository_id: UUID
    base_resources: str
    proposal_resources: str
    task_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrialResult:
    trial_id: UUID
    outcome: TrialOutcome
    judge_tool: BenchmarkTool | None
    judge_model: str | None
    judge_effort: str | None
    execution_id: UUID | None = None


async def plan_run(
    store: KnowledgeStore, projects: ProjectService, path: str, plan: BenchmarkPlanInput,
) -> dict[str, object]:
    store.require_reviewer()
    suite = await _suite(store, path, lock="write")
    assert suite is not None
    request_hash = hashlib.sha256(json.dumps(asdict(plan), sort_keys=True, default=str).encode()).hexdigest()
    model = KnowledgeRegressionRun
    prior = await store.db.scalar(select(model).where(
        model.suite_id == suite.id, model.operation_id == plan.operation_id,
    ))
    if prior is not None:
        if prior.created_by != store.scope.user or request_hash != prior.request_digest:
            raise KnowledgeError("KNOWLEDGE_BENCHMARK_OPERATION_CONFLICT", "该操作标识已用于另一份运行。", 409)
        return await _report(store, suite, prior)

    proposal = await _proposal(store, suite, plan.proposal_id) if plan.proposal_id else None
    if proposal is not None and proposal.status == "closed":
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_PROPOSAL_SETTLED", "已放弃的提案不能计划新运行。", 409)
    task = KnowledgeRegressionTask
    query = _active_tasks(suite, proposal).with_only_columns(task.id, task.name, maintain_column_froms=True)
    if plan.task_names:
        query = query.where(task.name.in_(plan.task_names))
    trial_factor = len(suite.tools) * len(ARMS) * suite.repetitions
    max_tasks = MAX_TRIALS // trial_factor
    rows = (await store.db.execute(query.order_by(task.name).limit(max_tasks + 1))).tuples().all()
    names = {name for _, name in rows}
    if len(rows) > max_tasks:
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_MATRIX_LIMIT", "矩阵过大，请缩小本次任务范围。", 422)
    if not rows or len(names) != len(rows) or set(plan.task_names) - names:
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_TASK_SELECTION", "任务不存在或默认和提案任务有重名。", 422)

    # 单次运行可能绑定两个仓库，统一排序后锁定连接版本。
    versions = {
        repository: await _lock_connection(store, projects, repository)
        for repository in sorted({suite.repository_id, plan.resource_repository_id})
    }
    now = datetime.now(UTC)
    run = model(
        id=uuid4(), suite_id=suite.id, proposal_id=plan.proposal_id, operation_id=plan.operation_id,
        request_digest=request_hash, repository_version=versions[suite.repository_id],
        resource_repository_id=plan.resource_repository_id,
        resource_repository_version=versions[plan.resource_repository_id],
        base_resources=plan.base_resources, proposal_resources=plan.proposal_resources,
        created_by=store.scope.user, created_at=now, deadline=now + timedelta(seconds=suite.timeout_seconds),
    )
    store.db.add(run)
    await store.db.flush()
    store.db.add_all([
        KnowledgeRegressionTrial(id=uuid4(), run_id=run.id, task_id=task_id, tool=tool, arm=arm, repetition=number)
        for (task_id, _), tool, arm, number in product(rows, suite.tools, ARMS, range(1, suite.repetitions + 1))
    ])
    await store.db.flush()
    return await _report(store, suite, run)


async def _scoped_run(
    store: KnowledgeStore, identifier: UUID, *, write: bool = False,
) -> tuple[KnowledgeRegressionRun, KnowledgeRegressionSuite]:
    run, suite = KnowledgeRegressionRun, KnowledgeRegressionSuite
    query = select(run, suite).join(suite, suite.id == run.suite_id).where(
        run.id == identifier, suite.workspace_id == store.scope.workspace, suite.project_id == store.scope.project,
    ).with_for_update(of=run, read=not write)
    row = (await store.db.execute(query)).tuples().one_or_none()
    if row is None:
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_RUN_NOT_FOUND", "当前项目找不到基准运行。", 404)
    return row


def _deadline(run: KnowledgeRegressionRun) -> datetime:
    value = run.deadline
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _result_query(run: KnowledgeRegressionRun) -> Select[tuple[KnowledgeRegressionTrial, str, str]]:
    trial, task = KnowledgeRegressionTrial, KnowledgeRegressionTask
    return select(trial, task.name, task.starting_commit).join(task, task.id == trial.task_id).where(
        trial.run_id == run.id,
    ).order_by(task.name, trial.tool, trial.repetition, trial.arm)


def _judge(trial: KnowledgeRegressionTrial) -> tuple[BenchmarkTool | None, str | None, str | None]:
    return trial.judge_tool, trial.judge_model, trial.judge_effort


@dataclass(slots=True)
class Comparison:
    planned: int = 0
    compared: int = 0
    excluded_judge_changed: int = 0
    pending: int = 0
    main_passed: int = 0
    pulled_passed: int = 0

    def observe(self, main: KnowledgeRegressionTrial, pulled: KnowledgeRegressionTrial) -> None:
        self.planned += 1
        if main.outcome is None or pulled.outcome is None:
            self.pending += 1
            return
        if main.judge_tool and pulled.judge_tool and _judge(main) != _judge(pulled):
            self.excluded_judge_changed += 1
            return
        self.compared += 1
        self.main_passed += int(main.outcome == "passed")
        self.pulled_passed += int(pulled.outcome == "passed")


async def _report(
    store: KnowledgeStore, suite: KnowledgeRegressionSuite, run: KnowledgeRegressionRun,
) -> dict[str, object]:
    pairs: dict[tuple[str, BenchmarkTool, int], dict[BenchmarkArm, KnowledgeRegressionTrial]] = defaultdict(dict)
    comparisons: dict[tuple[str, BenchmarkTool], Comparison] = defaultdict(Comparison)
    slots: list[dict[str, object]] = []
    incomplete = 0
    rows = await store.db.execute(_result_query(run))
    for trial, name, start in rows.tuples():
        pairs[name, trial.tool, trial.repetition][trial.arm] = trial
        incomplete += int(trial.outcome not in ("passed", "failed"))
        slots.append({
            "id": trial.id, "task_id": trial.task_id, "task": name, "starting_commit": start,
            "tool": trial.tool, "arm": trial.arm, "repetition": trial.repetition, "outcome": trial.outcome or "not_run",
            "judge_tool": trial.judge_tool, "judge_model": trial.judge_model, "judge_effort": trial.judge_effort,
            "recorded_by": trial.recorded_by, "recorded_at": _timestamp(trial.recorded_at),
        })
    for (name, tool, _), sides in pairs.items():
        comparisons[name, tool].observe(sides["main"], sides["pulled"])
    terminal: Literal["complete", "expired", "pending"]
    terminal = "complete" if run.completed_at else "expired" if datetime.now(UTC) >= _deadline(run) else "pending"
    gate: Literal["incomplete", "regressed", "held"]
    if incomplete or not comparisons or any(item.excluded_judge_changed for item in comparisons.values()):
        gate = "incomplete"
    else:
        gate = "regressed" if any(item.pulled_passed < item.main_passed for item in comparisons.values()) else "held"
    return {
        "id": run.id, "suite": suite.path, "proposal_id": run.proposal_id, "repository_id": suite.repository_id,
        "connection_version": run.repository_version, "resource_repository_id": run.resource_repository_id,
        "resource_connection_version": run.resource_repository_version, "base_resources": run.base_resources,
        "proposal_resources": run.proposal_resources, "created_by": run.created_by,
        "created_at": _timestamp(run.created_at), "deadline": _timestamp(run.deadline),
        "completed_at": _timestamp(run.completed_at), "status": terminal, "gate": gate, "trials": slots,
        "comparisons": [{"task": name, "tool": tool, **asdict(item)} for (name, tool), item in comparisons.items()],
    }


async def report_run(store: KnowledgeStore, identifier: UUID) -> dict[str, object]:
    run, suite = await _scoped_run(store, identifier)
    return await _report(store, suite, run)


async def execution_inputs(store: KnowledgeStore, identifier: UUID) -> dict[str, object]:
    """受授权执行器显式取任务输入，公开进度报告只读固定 ID 和提交。"""
    store.require_reviewer()
    run, suite = await _scoped_run(store, identifier)
    task, trial = KnowledgeRegressionTask, KnowledgeRegressionTrial
    rows = await store.db.execute(select(task.id, task.prompt, task.success_criteria).join(
        trial, trial.task_id == task.id,
    ).where(trial.run_id == run.id).distinct().order_by(task.id))
    return {
        "run": await _report(store, suite, run),
        "tasks": [{"id": ident, "prompt": prompt, "success_criteria": criteria} for ident, prompt, criteria in rows],
        "execution": run.execution,
    }


async def _require_current_attempt(store: KnowledgeStore, run: UUID, operation: UUID | None) -> None:
    """在基准行锁内核对 CI 写入者。直接计划的基准和旧 CI 继续使用原契约。"""
    check = KnowledgeImprovementCheck
    attached = await store.db.scalar(select(check).where(check.benchmark_id == run).order_by(
        check.generation.desc(),
    ).limit(1))
    if attached is None or attached.workflow_run is None:
        if operation is None:
            return
    elif attached.operation_id == operation:
        latest = await store.db.scalar(select(check.generation).where(
            check.proposal_id == attached.proposal_id, check.revision == attached.revision,
        ).order_by(check.generation.desc()).limit(1))
        if latest == attached.generation:
            return
    raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_CI_ATTEMPT", "只有当前 CI 尝试可以登记基准结果。", 409)


async def bind_execution(
    store: KnowledgeStore, identifier: UUID, profile: BenchmarkExecutionProfile, operation: UUID | None = None,
) -> BenchmarkExecutionBinding:
    """在首个结算之前固定执行条件，重复登记同一条件时返回原记录。"""
    store.require_reviewer()
    run, _ = await _scoped_run(store, identifier, write=True)
    await _require_current_attempt(store, identifier, operation)
    if run.execution is not None:
        if run.execution["profile"] != profile:
            raise KnowledgeError("KNOWLEDGE_BENCHMARK_CONFIG", "续跑必须使用本次运行登记的执行条件。", 409)
        return run.execution
    now = datetime.now(UTC)
    if now >= _deadline(run):
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_RUN_EXPIRED", "基准运行已超时。", 409)
    trial = KnowledgeRegressionTrial
    settled = await store.db.scalar(select(trial.id).where(trial.run_id == run.id, trial.outcome.is_not(None)).limit(1))
    if settled is not None:
        raise KnowledgeError(
            "KNOWLEDGE_BENCHMARK_CONFIG", "该运行已有按旧契约登记的结果，须沿用原结算方式。", 409,
        )
    run.execution = {
        "id": str(uuid4()), "profile": profile,
        "registered_by": str(store.scope.user), "registered_at": now.isoformat(),
    }
    await store.db.flush()
    return run.execution


async def record_results(
    store: KnowledgeStore, identifier: UUID, results: list[TrialResult], operation: UUID | None = None,
) -> dict[str, object]:
    store.require_reviewer()
    run, suite = await _scoped_run(store, identifier, write=True)
    await _require_current_attempt(store, identifier, operation)
    trial = KnowledgeRegressionTrial
    rows = await store.db.scalars(select(trial).where(trial.run_id == run.id))
    slots = {row.id: row for row in rows}
    now = datetime.now(UTC)
    execution = run.execution
    expected_id = UUID(execution["id"]) if execution else None
    for result in results:
        slot = slots.get(result.trial_id)
        if slot is None:
            raise KnowledgeError("KNOWLEDGE_BENCHMARK_TRIAL_SCOPE", "试验不属于这次运行。", 404)
        supplied = result.judge_tool, result.judge_model, result.judge_effort
        if result.execution_id != expected_id:
            raise KnowledgeError("KNOWLEDGE_BENCHMARK_CONFIG", "试验收据与本次登记的执行条件不一致。", 409)
        if execution and result.outcome in ("passed", "failed"):
            profile = execution["profile"]
            if supplied != (profile["judge_tool"], profile["judge_model"], None):
                raise KnowledgeError("KNOWLEDGE_BENCHMARK_CONFIG", "试验的评判身份不同于运行登记的配置。", 409)
        if slot.outcome is not None:
            if slot.outcome != result.outcome or _judge(slot) != supplied:
                raise KnowledgeError("KNOWLEDGE_BENCHMARK_TRIAL_SETTLED", "试验已有其他判定。", 409)
            continue
        if now >= _deadline(run):
            raise KnowledgeError("KNOWLEDGE_BENCHMARK_RUN_EXPIRED", "基准运行已超时。", 409)
        slot.outcome = result.outcome
        slot.judge_tool, slot.judge_model, slot.judge_effort = supplied
        slot.recorded_at, slot.recorded_by = now, store.scope.user
    if run.completed_at is None and all(slot.outcome is not None for slot in slots.values()):
        run.completed_at = now
    await store.db.flush()
    return await _report(store, suite, run)
