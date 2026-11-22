"""结算可信知识工作流的终态，每次尝试留存，周报只读取最新终态。"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import KnowledgeWorkflowAttempt
from weora.modules.knowledge.review_source import registered_repo
from weora.modules.knowledge.store import KnowledgeStore, Scope
from weora.modules.projects.domain import ProjectActor
from weora.modules.projects.service import ProjectService

WorkflowKind = Literal["knowledge-review", "improvement-benchmark", "self-improvement"]
WorkflowOutcome = Literal["succeeded", "failed", "cancelled", "skipped"]


async def record_workflow(
    store: KnowledgeStore, projects: ProjectService, *,
    repository_id: UUID, connection_version: int, repository: str, run_id: str,
    workflow: WorkflowKind, attempt: int, outcome: WorkflowOutcome, duration_ms: int,
) -> dict[str, object]:
    store.require_reviewer()
    scope, db = store.scope, store.db
    if scope.project is None or dict(scope.code_connections).get(repository_id) != connection_version:
        raise KnowledgeError("KNOWLEDGE_WORKFLOW_REPOSITORY", "工作流必须绑定当前项目的活动仓库连接。", 409)
    await store.require_usage_reporting()
    actor = ProjectActor(scope.user, scope.workspace)
    await projects.lock_active_repository(actor, scope.project, repository_id, connection_version, db)
    addresses = await projects.active_repository_addresses(actor, scope.project, db)
    expected = registered_repo(f"https://github.com/{repository}.git")
    if expected is None or registered_repo(addresses.get(repository_id, "")) != expected:
        raise KnowledgeError("KNOWLEDGE_WORKFLOW_REPOSITORY", "GitHub 仓库与项目登记地址不匹配。", 409)

    model = KnowledgeWorkflowAttempt
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    statement = insert(model).values(
        repository_id=repository_id, run_id=run_id, attempt=attempt, workspace_id=scope.workspace,
        project_id=scope.project, connection_version=connection_version, reported_by=scope.user,
        reported_at=datetime.now(UTC), workflow=workflow, outcome=outcome, duration_ms=duration_ms,
    ).on_conflict_do_nothing(index_elements=[model.repository_id, model.run_id, model.attempt])
    if await db.scalar(statement.returning(model.attempt)) is not None:
        return {"status": "recorded"}
    original = await db.get(model, (repository_id, run_id, attempt))
    if original is None or (
        original.workflow, original.outcome, original.duration_ms
    ) != (workflow, outcome, duration_ms):
        raise KnowledgeError("KNOWLEDGE_WORKFLOW_CONFLICT", "该次工作流尝试已登记不同终态。", 409)
    return {"status": "already_recorded"}


async def workflow_digest(db: AsyncSession, scope: Scope) -> dict[str, int] | None:
    if scope.project is None:
        return None
    row = KnowledgeWorkflowAttempt
    newest = select(
        row.repository_id, row.run_id, func.max(row.attempt).label("attempt"),
    ).where(
        row.workspace_id == scope.workspace, row.project_id == scope.project,
    ).group_by(row.repository_id, row.run_id).subquery()
    statement = select(row.outcome, func.count(), func.sum(row.duration_ms)).join(newest, and_(
        row.repository_id == newest.c.repository_id, row.run_id == newest.c.run_id, row.attempt == newest.c.attempt,
    )).where(row.outcome != "skipped").group_by(row.outcome)
    groups = (await db.execute(statement)).tuples().all()
    if not groups:
        return None
    digest = {"runs": 0, "succeeded": 0, "failed": 0, "total_duration_ms": 0}
    for outcome, count, duration in groups:
        digest["runs"] += count
        digest["total_duration_ms"] += duration
        if outcome in ("succeeded", "failed"):
            digest[outcome] = count
    return digest
