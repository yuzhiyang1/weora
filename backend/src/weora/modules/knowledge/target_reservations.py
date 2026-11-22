"""串行化知识晋升与 Skill Registry 首次占用逻辑路径。"""

from hashlib import sha256
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeMarkdownPath
from weora.modules.knowledge.skill_releases import linked_to_skill, skill_promotion
from weora.modules.skills.application.ports import SkillRecord
from weora.modules.skills.domain.errors import SkillPromotionPathConflictError, SkillPromotionSourceConflictError
from weora.modules.skills.infrastructure.knowledge_catalog import published_skill_reference


async def lock_knowledge_targets(db: AsyncSession, workspace: UUID, project: UUID | None) -> None:
    """同一范围使用事务锁，批量贡献和首次发布在提交前共同保护候选目标。"""
    if db.get_bind().dialect.name == "postgresql":
        key = sha256(f"knowledge-targets:{workspace}:{project}".encode()).digest()
        await db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(key[:8], signed=True))))


async def guard_registry_publication(db: AsyncSession, project: UUID | None, skill: SkillRecord) -> None:
    """在 Registry 的发布事务中核对待审和已发布的晋升路径。"""
    path = "/".join(part for part in ("skills", skill.namespace, skill.slug) if part)
    indexed = KnowledgeMarkdownPath
    linked = (await db.execute(skill_promotion(skill.workspace_id, project, skill.id).join(
        indexed, indexed.entry_id == KnowledgeEntry.id,
    ).where(indexed.revision == 1, indexed.position == 0).with_only_columns(
        KnowledgeEntry.id, KnowledgeEntry.promoted_from, indexed.path,
    ))).one_or_none()
    keys = [sha256(path.encode()).hexdigest()]
    if linked is not None:
        entry, source, alias = linked
        # 同一来源一次只有一份待审或已发布晋升，归档后可能已另选文档或规则。
        replaced = select(KnowledgeEntry.id).where(
            KnowledgeEntry.workspace_id == skill.workspace_id, KnowledgeEntry.project_id == project,
            KnowledgeEntry.promoted_from == source, KnowledgeEntry.id != entry,
            KnowledgeEntry.status.in_(("pending", "active")),
        )
        if await db.scalar(replaced.limit(1)) is not None:
            raise SkillPromotionSourceConflictError
        if alias != path:
            # 历史安装名可能与知识路径不同，恢复会同时公开这两个入口。
            keys.append(sha256(alias.encode()).hexdigest())
            other = await published_skill_reference(db, skill.workspace_id, project, path=alias)
            if other is not None and other.id != skill.id and other.project_id == project:
                raise SkillPromotionPathConflictError
    reserved = select(KnowledgeEntry.id).join(indexed, indexed.entry_id == KnowledgeEntry.id).where(
        KnowledgeEntry.workspace_id == skill.workspace_id, KnowledgeEntry.project_id == project,
        KnowledgeEntry.kind == "skill", KnowledgeEntry.promoted_from.is_not(None),
        KnowledgeEntry.status.in_(("pending", "active")),
        indexed.revision == 1, indexed.position == 0, indexed.key.in_(keys), ~linked_to_skill(skill.id),
    )
    if await db.scalar(reserved.limit(1)) is not None:
        raise SkillPromotionPathConflictError
