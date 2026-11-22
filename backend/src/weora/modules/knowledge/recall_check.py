"""从已发布索引投影首位召回证据，预检不读取正文或仓库。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import CTE

from weora.modules.knowledge.markdown_paths_v1 import entry_path
from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeRevision
from weora.modules.knowledge.recall_code import Published
from weora.modules.knowledge.resource_paths import normalize_resource_path
from weora.modules.knowledge.search import RELEVANCE_FLOOR, Candidate, SearchHit

_SOURCE_LIMIT = 10


def empty_check() -> dict[str, object]:
    return {"relevant": False, "score": 0.0, "threshold": RELEVANCE_FLOOR}


async def describe_check(
    db: AsyncSession, published: Published, registry: CTE, hit: SearchHit | None,
) -> dict[str, object]:
    """按正式召回的排序解释首位。仅相关命中返回标题和可见出处。"""
    if hit is None:
        return empty_check()
    result: dict[str, object] = {
        "relevant": hit.relevant, "score": round(hit.score, 4), "threshold": round(hit.threshold, 4),
    }
    if not hit.relevant:
        return result
    candidate = hit.candidate
    title: str
    tags: list[str]
    if candidate.skill_version_id is not None:
        skill = (await db.execute(select(
            registry.c.title, registry.c.tags, registry.c.path,
        ).where(registry.c.id == candidate.entry, registry.c.version_id == candidate.skill_version_id))).one_or_none()
        if skill is None:
            return empty_check()
        title, tags, path = skill
        sources: list[dict[str, str]] = []
    else:
        metadata = (await db.execute(published.with_only_columns(
            KnowledgeRevision.title, KnowledgeRevision.tags, KnowledgeRevision.source, KnowledgeRevision.relations,
        ).where(KnowledgeEntry.id == candidate.entry, KnowledgeRevision.revision == candidate.revision)
        )).one_or_none()
        if metadata is None:
            # 另一事务可能已切换或撤下命中版本，避免显示旧标题和出处。
            return empty_check()
        title, tags, origin, relations = metadata
        path = entry_path(candidate.entry, candidate.kind, origin)
        sources = await _code_sources(db, published, candidate, origin, relations)
    return result | hit.evidence(title, tags) | {
        "title": title, "entry_id": str(candidate.entry), "resource_path": path, "sources": sources,
    }


async def _code_sources(
    db: AsyncSession, published: Published, candidate: Candidate,
    origin: dict[str, str], relations: list[dict[str, str]],
) -> list[dict[str, str]]:
    if candidate.codebase is None:
        return []
    if source := normalize_resource_path(origin.get("path", "")):
        return [{"path": source}]
    # 概览只导航到仓库。模块页和图谱页只列出用发布关系明确引用的源码。
    targets: list[UUID] = []
    for link in relations:
        if link.get("kind") != "REFERENCES":
            continue
        try:
            target = UUID(link.get("target", ""))
        except ValueError:
            continue
        if target not in targets:
            targets.append(target)
        if len(targets) >= _SOURCE_LIMIT + 1:
            break
    if not targets:
        return []
    path = KnowledgeRevision.source["path"].as_string()
    # 如果并发索引已更换概览版本，不能将旧页面的边和新一版文件混用。
    parent = aliased(KnowledgeEntry)
    same_version = select(parent.id).where(
        parent.id == candidate.entry, parent.status == "active", parent.published_revision == candidate.revision,
    ).exists()
    rows = (await db.execute(published.with_only_columns(KnowledgeEntry.id, path).where(
        KnowledgeEntry.id.in_(targets), KnowledgeEntry.codebase_id == candidate.codebase, path != "", same_version,
    ))).tuples()
    current = {identity: relative for identity, value in rows if (relative := normalize_resource_path(value))}
    return [{"path": current[target]} for target in targets if target in current][:_SOURCE_LIMIT]
