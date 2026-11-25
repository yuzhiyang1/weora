"""在同一个 Registry 事务中维护晋升知识的版本和归档状态。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from weora.modules.knowledge.markdown_references import index_markdown, saved_declarations
from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeEvent, KnowledgeMarkdownPath, KnowledgeRevision
from weora.modules.knowledge.search import add_indexed_revisions
from weora.modules.skills.application.ports import SkillRecord, SkillVersionRecord
from weora.modules.skills.domain.errors import SkillDraftChangedError, SkillKnowledgeReviewPendingError

REGISTRY_VERSION = "registry_version"
REGISTRY_VERSION_ID = "registry_version_id"


def linked_to_skill(skill_id: UUID) -> ColumnElement[bool]:
    """以已提交的 Skill 发布事件确认知识条目关联的原资产。"""
    return select(KnowledgeEvent.id).where(
        KnowledgeEvent.entry_id == KnowledgeEntry.id,
        KnowledgeEvent.action == "skill_published",
        KnowledgeEvent.reason == str(skill_id),
    ).correlate(KnowledgeEntry).exists()


def skill_promotion(workspace: UUID, project: UUID | None, skill_id: UUID) -> Select[tuple[KnowledgeEntry]]:
    return select(KnowledgeEntry).where(
        KnowledgeEntry.workspace_id == workspace,
        KnowledgeEntry.project_id == project,
        KnowledgeEntry.kind == "skill",
        KnowledgeEntry.promoted_from.is_not(None),
        linked_to_skill(skill_id),
    )


async def record_registry_release(
    db: AsyncSession, project: UUID | None, release: SkillVersionRecord, markdown: str,
) -> None:
    """复用已授权的发布事务，保证搜索、引用和下载看到同一版。"""
    entry = await db.scalar(skill_promotion(release.workspace_id, project, release.skill_id).with_for_update())
    if entry is None:
        return
    current = await db.get(KnowledgeRevision, (entry.id, entry.published_revision))
    latest = await db.scalar(select(KnowledgeRevision).where(
        KnowledgeRevision.entry_id == entry.id,
    ).order_by(KnowledgeRevision.revision.desc()).limit(1))
    if current is None or latest is None:
        raise SkillDraftChangedError
    if latest.status == "pending":
        # 两种编辑入口会修改同一发布指针，保留基于当前版本提交的审核草稿。
        raise SkillKnowledgeReviewPendingError
    origin = await db.scalar(select(KnowledgeMarkdownPath.path).where(
        KnowledgeMarkdownPath.entry_id == entry.id,
        KnowledgeMarkdownPath.revision == 1,
        KnowledgeMarkdownPath.position == 0,
    ))
    revision = KnowledgeRevision(
        entry_id=entry.id, revision=latest.revision + 1, workspace_id=release.workspace_id,
        author_id=release.published_by, operation_id=release.id, request_digest=release.content_sha256,
        status="published", title=current.title, body=markdown, tags=current.tags, relations=current.relations,
        source={**current.source, REGISTRY_VERSION: release.version, REGISTRY_VERSION_ID: str(release.id)},
        relation_findings=current.relation_findings, rule_paths=current.rule_paths,
        created_at=release.published_at, authored_at=current.authored_at,
    )
    declared = await saved_declarations(db, current)
    await add_indexed_revisions(db, [revision], kind="skill", path=origin)
    index_markdown(db, entry, revision, origin, declared)
    entry.published_revision = revision.revision
    db.add(KnowledgeEvent(
        id=uuid4(), entry_id=entry.id, actor_id=release.published_by, action="skill_published",
        revision=revision.revision, reason=str(release.skill_id), occurred_at=release.published_at,
    ))


async def record_registry_status(
    db: AsyncSession, project: UUID | None, skill: SkillRecord, actor: UUID, occurred_at: datetime,
) -> None:
    """归档或恢复原知识入口，同时保留发布版本、待审稿和来源。"""
    entry = await db.scalar(skill_promotion(skill.workspace_id, project, skill.id).with_for_update())
    if entry is None or entry.status == skill.status:
        return
    if entry.status == "pending" and entry.published_revision is not None:
        # 重复归档不能释放重新晋升的待审位置。只有知识审核可以恢复这一资产。
        if skill.status != "archived":
            raise SkillKnowledgeReviewPendingError
        return
    entry.status = skill.status
    db.add(KnowledgeEvent(
        id=uuid4(), entry_id=entry.id, actor_id=actor,
        action="archived" if skill.status == "archived" else "restored",
        revision=entry.published_revision, reason="同步 Skill 归档状态", occurred_at=occurred_at,
    ))
