"""为独立 Skill 提供质量候选和审核后的 Registry 发布。"""

from uuid import UUID

from sqlalchemy import Select, String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import CTE

from weora.modules.knowledge.domain import FeedbackTotals, KnowledgeError
from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeRevision, KnowledgeSkillRecall
from weora.modules.knowledge.recall_metrics import feedback_totals, recall_aggregates
from weora.modules.knowledge.registry_skills import next_skill_patch, replace_skill_body, standalone_skill_catalog
from weora.modules.skills.application.service import SkillActor
from weora.modules.skills.domain.errors import SkillRegistryError
from weora.modules.skills.infrastructure.binding import scoped_skill_registry
from weora.modules.skills.infrastructure.knowledge_catalog import published_skill_reference
from weora.shared.object_storage import ObjectStoragePort

REGISTRY_QUALITY_SOURCE = "registry_quality"


def pending_skill_proposals(workspace: UUID, project: UUID | None, *, include_shared: bool = False) -> CTE:
    """提案进入知识审核队列，待处理期间同一资产不能再次提交。"""
    owners = KnowledgeEntry.project_id == project
    if include_shared and project is not None:
        owners = or_(owners, KnowledgeEntry.project_id.is_(None))
    return select(KnowledgeRevision.source["skill_id"].as_string().label("skill_id")).join(
        KnowledgeEntry, KnowledgeEntry.id == KnowledgeRevision.entry_id,
    ).where(
        KnowledgeEntry.workspace_id == workspace, owners,
        KnowledgeEntry.status == "pending", KnowledgeRevision.status == "pending",
        KnowledgeRevision.source["type"].as_string() == REGISTRY_QUALITY_SOURCE,
    ).cte("pending_skill_quality")


def registry_quality_catalog(
    workspace: UUID, project: UUID | None, *, include_shared: bool = False, directories: tuple[str, ...] | None = None,
) -> CTE:
    catalog = standalone_skill_catalog(workspace, project, delivered=False, directories=directories)
    pending = pending_skill_proposals(workspace, project, include_shared=include_shared)
    # JSON 中保存标准 UUID，忽略数据库文本表示中的分隔符差异。
    matches = func.replace(pending.c.skill_id, "-", "") == func.replace(cast(catalog.c.id, String), "-", "")
    rows = select(catalog).where(~select(pending.c.skill_id).where(matches).exists())
    if include_shared and project is not None:
        local = catalog.alias("project_skill")
        shadows_shared = select(local.c.id).where(
            local.c.project_id == project, local.c.path == catalog.c.path,
        ).correlate(catalog).exists()
        rows = rows.where(~(catalog.c.project_id.is_(None) & shadows_shared))
    else:
        rows = rows.where(catalog.c.project_id == project)
    return rows.cte("registry_quality_catalog")


def registry_quality_feedback(catalog: CTE) -> Select[tuple[object, ...]]:
    """合并已授权资产和其反馈，缺少反馈的资产也参与游标分页。"""
    history = KnowledgeSkillRecall
    measures = (
        select(history.entry_id, *recall_aggregates(history))
        .join(catalog, catalog.c.id == history.entry_id)
        .group_by(history.entry_id)
        .subquery()
    )
    return select(
        catalog.c.id, catalog.c.title, catalog.c.version_id, catalog.c.project_id,
        *(column for column in measures.c if column.key != "entry_id"),
    ).outerjoin(measures, measures.c.entry_id == catalog.c.id)


async def registry_skill_totals(db: AsyncSession, skill: UUID) -> FeedbackTotals:
    measures = select(*recall_aggregates(KnowledgeSkillRecall)).where(KnowledgeSkillRecall.entry_id == skill)
    return feedback_totals((await db.execute(measures)).mappings().one())


async def publish_registry_quality(
    db: AsyncSession,
    actor: SkillActor,
    project: UUID | None,
    skill_id: UUID,
    base: UUID,
    body: str,
    request_id: str,
    storage: ObjectStoragePort | None,
) -> UUID:
    """保留原 Skill 元数据和附属文件，在审核事务内发布到原资产。"""
    workspace = actor.workspace_id
    reference = await published_skill_reference(db, workspace, project, identity=skill_id, lock=True)
    if reference is None or reference.project_id != project or reference.version_id != base:
        raise KnowledgeError("KNOWLEDGE_CONFLICT", "Skill 基准版本已变化，请拒绝此提案后重新生成。", 409)
    registry = await scoped_skill_registry(db, project, storage)
    try:
        async with db.begin_nested():
            version = await next_skill_patch(registry, actor, skill_id, reference.version)
            sha = await replace_skill_body(registry, actor, skill_id, reference.version, body, request_id)
            released = await registry.publish(
                actor=actor, workspace_id=workspace, skill_id=skill_id, version=version,
                changelog="应用已审核的知识质量修订", request_id=request_id, draft_sha256=sha,
            )
    except SkillRegistryError as error:
        raise KnowledgeError("KNOWLEDGE_SKILL_PUBLISH_FAILED", f"{error.message}。知识版本仍待审核。", 409) from error
    return released.id
