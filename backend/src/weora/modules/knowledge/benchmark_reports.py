"""基准看板只读取已结算或到期的历史运行与持久化试验。"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from math import sqrt
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Subquery

from weora.modules.knowledge.benchmark import _project, _suite_view, _timestamp
from weora.modules.knowledge.benchmark_models import (
    KnowledgeRegressionRun,
    KnowledgeRegressionSuite,
    KnowledgeRegressionTask,
    KnowledgeRegressionTrial,
)
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.store import KnowledgeStore


def _rate(total: int, passed: int) -> dict[str, object]:
    """通过率按计划次数计算，缺失或错误的试验不能提高通过率。"""
    proportion = passed / total if total else None
    interval = None
    if proportion is not None:
        z = 1.959963984540054
        weight = 1 + z * z / total
        center = (proportion + z * z / (2 * total)) / weight
        radius = z * sqrt(proportion * (1 - proportion) / total + z * z / (4 * total**2)) / weight
        interval = [max(0, center - radius), min(1, center + radius)]
    return {"planned": total, "passed": passed, "pass_rate": proportion, "interval95": interval}


async def _paired(store: KnowledgeStore, runs: Subquery) -> list[dict[str, object]]:
    baseline, proposed = aliased(KnowledgeRegressionTrial), aliased(KnowledgeRegressionTrial)
    task = KnowledgeRegressionTask
    judged = baseline.outcome.in_(("passed", "failed")) & proposed.outcome.in_(("passed", "failed"))
    same_judge = (
        (baseline.judge_tool == proposed.judge_tool)
        & (baseline.judge_model == proposed.judge_model)
        & baseline.judge_effort.is_not_distinct_from(proposed.judge_effort)
    )
    comparable = judged & same_judge
    rows = await store.db.execute(select(
        task.name, task.starting_commit, baseline.tool, func.count(),
        func.sum(case((comparable, 1), else_=0)),
        func.sum(case((judged & ~same_judge, 1), else_=0)),
        func.sum(case((comparable & (baseline.outcome == "passed"), 1), else_=0)),
        func.sum(case((comparable & (proposed.outcome == "passed"), 1), else_=0)),
    ).select_from(baseline).join(
        proposed, (baseline.run_id == proposed.run_id) & (baseline.task_id == proposed.task_id)
        & (baseline.tool == proposed.tool) & (baseline.repetition == proposed.repetition),
    ).join(runs, runs.c.id == baseline.run_id).join(task, task.id == baseline.task_id).where(
        baseline.arm == "main", proposed.arm == "pulled",
    ).group_by(task.name, task.starting_commit, baseline.tool).order_by(task.name, task.starting_commit, baseline.tool))
    return [
        {"task": name, "starting_commit": start, "tool": tool, "planned": planned, "compared": compared,
         "excluded_judge_changed": changed, "unjudged": planned - compared - changed,
         "main": _rate(compared, main), "pulled": _rate(compared, pulled)}
        for name, start, tool, planned, compared, changed, main, pulled in rows.tuples()
    ]


async def benchmark_dashboard(
    store: KnowledgeStore, suite_id: UUID | None, days: int, limit: int, offset: int,
) -> dict[str, object]:
    """聚合所选套件的完整时间窗，分页只限制历史明细。"""
    suite, run, trial, task = (
        KnowledgeRegressionSuite, KnowledgeRegressionRun, KnowledgeRegressionTrial, KnowledgeRegressionTask,
    )
    suites = (await store.db.scalars(select(suite).where(
        suite.workspace_id == store.scope.workspace, suite.project_id == _project(store),
    ).order_by(suite.path, suite.id))).all()
    selected = next((row for row in suites if row.id == suite_id), None) if suite_id else next(iter(suites), None)
    if suite_id and selected is None:
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_SUITE_NOT_FOUND", "当前项目找不到回归套件。", 404)

    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    common = {"days": days, "since": since.isoformat(), "until": now.isoformat(),
              "suites": [_suite_view(row) for row in suites], "suite_id": selected.id if selected else None}
    if selected is None:
        return common | {
            "total": 0, "next_offset": None, "runs": [], "arms": [], "tasks": [], "comparisons": [], "revisions": [],
        }

    ended = func.coalesce(run.completed_at, run.deadline)
    window = (run.suite_id == selected.id, ended >= since, ended <= now)
    history = select(run.id).where(*window).subquery()
    total = int(await store.db.scalar(select(func.count()).select_from(history)) or 0)
    page = (await store.db.scalars(select(run).where(*window).order_by(
        ended.desc(), run.id.desc(),
    ).offset(offset).limit(limit))).all()

    rows = await store.db.execute(select(
        trial.run_id, trial.tool, trial.arm, task.name, run.base_resources, run.proposal_resources, func.count(),
        func.sum(case((trial.outcome == "passed", 1), else_=0)),
        func.sum(case((trial.outcome.in_(("passed", "failed")), 1), else_=0)),
    ).join(history, history.c.id == trial.run_id).join(run, run.id == trial.run_id)
    .join(task, task.id == trial.task_id).group_by(
        trial.run_id, trial.tool, trial.arm, task.name, run.base_resources, run.proposal_resources,
    ))
    arm_totals: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    task_totals: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    revision_totals: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    page_ids = {row.id for row in page}
    run_totals: dict[tuple[UUID, str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for run_id, tool, arm, name, base, proposed, planned, passed, judged in rows.tuples():
        revision = base if arm == "main" else proposed
        aggregates = [arm_totals[tool, arm], task_totals[name, tool, arm], revision_totals[revision, tool, arm]]
        if run_id in page_ids:
            aggregates.append(run_totals[run_id, tool, arm])
        for counts in aggregates:
            counts[0] += planned
            counts[1] += passed
            counts[2] += judged
    history_arms: dict[UUID, list[dict[str, object]]] = defaultdict(list)
    for (run_id, tool, arm), (planned, passed, judged) in sorted(run_totals.items()):
        history_arms[run_id].append({"tool": tool, "arm": arm, "judged": judged, **_rate(planned, passed)})
    return common | {
        "total": total, "next_offset": offset + limit if offset + limit < total else None,
        "runs": [
            {"id": row.id, "proposal_id": row.proposal_id, "base_resources": row.base_resources,
             "proposal_resources": row.proposal_resources, "ended_at": _timestamp(row.completed_at or row.deadline),
             "status": "complete" if row.completed_at else "expired", "arms": history_arms[row.id]}
            for row in page
        ],
        "arms": [
            {"tool": tool, "arm": arm, "judged": judged, **_rate(planned, passed)}
            for (tool, arm), (planned, passed, judged) in sorted(arm_totals.items())
        ],
        "tasks": [
            {"task": name, "tool": tool, "arm": arm, "judged": judged, **_rate(planned, passed)}
            for (name, tool, arm), (planned, passed, judged) in sorted(task_totals.items())
        ],
        "comparisons": await _paired(store, history),
        "revisions": [
            {"revision": revision, "tool": tool, "arm": arm, "judged": judged, **_rate(planned, passed)}
            for (revision, tool, arm), (planned, passed, judged) in sorted(revision_totals.items())
        ],
    }
