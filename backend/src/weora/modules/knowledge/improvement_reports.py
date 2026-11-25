"""从项目回归、提案和交付账本重建自我改进进度。"""

from typing import cast
from uuid import UUID

from sqlalchemy import and_, func, select

from weora.modules.knowledge.benchmark import _project, _timestamp
from weora.modules.knowledge.benchmark_models import (
    KnowledgeImprovementCheck,
    KnowledgeImprovementDelivery,
    KnowledgeImprovementRevision,
    KnowledgeRegressionProposal,
    KnowledgeRegressionSuite,
    KnowledgeRegressionTask,
)
from weora.modules.knowledge.benchmark_runs import report_run
from weora.modules.knowledge.improvement_delivery import matches_benchmark
from weora.modules.knowledge.models import KnowledgeSessionLabel, KnowledgeSessionScorer
from weora.modules.knowledge.scoring_report import MergedResources, merged_resource_scores, scoring_failures
from weora.modules.knowledge.store import KnowledgeStore


async def _proof(
    store: KnowledgeStore, suite: KnowledgeRegressionSuite, proposal: KnowledgeRegressionProposal,
    revision: KnowledgeImprovementRevision, delivery: KnowledgeImprovementDelivery, benchmark: UUID,
) -> dict[str, object]:
    report = await report_run(store, benchmark)
    # 合并时已经验证不可变的一次运行。未合并的检查仍要核对当前提案与矩阵。
    verified = bool(delivery.benchmark_id == benchmark and delivery.merge_commit)
    if not verified and report["status"] == "complete" and report["gate"] == "held":
        verified = await matches_benchmark(store, suite, proposal, revision, delivery, report)
    return {
        "run_id": benchmark, "status": report["status"], "gate": report["gate"],
        "verified": verified, "comparisons": report["comparisons"],
    }


async def self_improvement_dashboard(
    store: KnowledgeStore, scorer: str | None, limit: int, offset: int,
) -> dict[str, object]:
    """列表只展示资源路径、版本和交付事实，正文由原有受控接口读取。"""
    db = store.db
    suite, proposal, task = KnowledgeRegressionSuite, KnowledgeRegressionProposal, KnowledgeRegressionTask
    revision, delivery, check = KnowledgeImprovementRevision, KnowledgeImprovementDelivery, KnowledgeImprovementCheck
    scope = (suite.workspace_id == store.scope.workspace, suite.project_id == _project(store))
    proposals = (*scope, proposal.scorer == scorer) if scorer else scope
    status_rows = await db.execute(select(
        proposal.status, func.count(),
    ).join(suite, suite.id == proposal.suite_id).where(*proposals).group_by(proposal.status))
    status_counts = dict(status_rows.tuples().all())
    total = sum(status_counts.values())
    scorers = (await db.scalars(select(proposal.scorer).join(suite, suite.id == proposal.suite_id)
                               .where(*scope).distinct().order_by(proposal.scorer))).all()
    tasks = select(func.count()).select_from(task).join(suite, suite.id == task.suite_id).where(*scope)
    if scorer:
        # 已合入的默认任务仍按它创建时绑定的评分标签归因。
        label, rubric = KnowledgeSessionLabel, KnowledgeSessionScorer
        tasks = tasks.join(label, label.id == task.label_id).join(
            rubric, and_(rubric.id == label.scorer_id, rubric.revision == label.scorer_revision),
        ).where(rubric.name == scorer)
    task_count = int(await db.scalar(tasks) or 0)
    failure_report = await scoring_failures(store, 1, scorer=scorer)

    newest = select(func.max(revision.revision)).where(
        revision.proposal_id == proposal.id,
    ).correlate(proposal).scalar_subquery()
    newest_check = select(func.max(check.generation)).where(
        check.proposal_id == revision.proposal_id, check.revision == revision.revision,
    ).correlate(revision).scalar_subquery()
    rows = await db.execute(select(suite, proposal, revision, delivery, check).select_from(proposal)
        .join(suite, suite.id == proposal.suite_id)
        .outerjoin(revision, and_(revision.proposal_id == proposal.id, revision.revision == newest))
        .outerjoin(delivery, and_(delivery.proposal_id == proposal.id, delivery.revision == revision.revision))
        .outerjoin(check, and_(check.proposal_id == proposal.id, check.revision == revision.revision,
                               check.generation == newest_check))
        .where(*proposals).order_by(proposal.created_at.desc(), proposal.id.desc()).offset(offset).limit(limit))
    items: list[dict[str, object]] = []
    for suite_row, proposal_row, newest_revision, delivered, checked in rows.tuples():
        proof = None
        effect = None
        if newest_revision and delivered:
            benchmark = delivered.benchmark_id or (checked.benchmark_id if checked else None)
            if benchmark:
                proof = await _proof(store, suite_row, proposal_row, newest_revision, delivered, benchmark)
            if delivered.merge_commit and proposal_row.settled_at:
                effect = await merged_resource_scores(store, MergedResources(
                    proposal_row.scorer_id, newest_revision.resource_repository_id, newest_revision.connection_version,
                    newest_revision.base_commit, delivered.merge_commit, proposal_row.settled_at,
                ))
        items.append({
            "id": proposal_row.id, "suite": suite_row.path, "suite_id": suite_row.id, "scorer": proposal_row.scorer,
            "status": proposal_row.status, "created_at": _timestamp(proposal_row.created_at),
            "settled_at": _timestamp(proposal_row.settled_at),
            "revision": newest_revision.revision if newest_revision else None,
            "reason": newest_revision.reason if newest_revision else None,
            "resources": [asset["path"] for asset in newest_revision.changes] if newest_revision else [],
            "tasks": len(newest_revision.task_ids) if newest_revision else None,
            "pr_number": delivered.pr_number if delivered else None,
            "merge_commit": delivered.merge_commit if delivered else None,
            "proof": proof,
            "effect": effect,
            "ci": {"run": checked.workflow_run, "attempt": checked.attempt}
            if checked and checked.workflow_run else None,
        })
    return {
        "scorers": sorted(set(scorers) | {
            str(cohort["scorer"]) for cohort in cast(list[dict[str, object]], failure_report["cohorts"])
        }),
        "total": total, "next_offset": offset + limit if offset + limit < total else None, "items": items,
        "summary": {"failed_labels": failure_report["total_failed"], "tasks": task_count,
                    "open": status_counts.get("open", 0), "merged": status_counts.get("merged", 0),
                    "closed": status_counts.get("closed", 0)},
    }
