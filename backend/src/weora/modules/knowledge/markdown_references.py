"""按不可变版本保存 Markdown 路径，并在召回所用范围内解析双向引用。"""

from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha256
from uuid import UUID

from sqlalchemy import Select, String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import CTE

from weora.modules.knowledge.markdown_paths_v1 import indexed_paths
from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeEvent, KnowledgeMarkdownPath, KnowledgeRevision
from weora.modules.knowledge.path_declarations import Declaration
from weora.modules.skills.infrastructure.knowledge_catalog import PublishedSkillReference, published_skill_paths


def index_markdown(
    db: AsyncSession, entry: KnowledgeEntry, revision: KnowledgeRevision, origin: str | None,
    declared: Sequence[Declaration],
) -> None:
    def posting(path: str, key: str, kind: str, position: int) -> KnowledgeMarkdownPath:
        return KnowledgeMarkdownPath(
            entry_id=entry.id, revision=revision.revision, key=key, path=path, kind=kind, position=position,
        )

    targets = {link.path for link in declared}
    db.add_all(
        posting(path, key, "REFERENCES", position)
        for position, (path, key) in enumerate(indexed_paths(entry.kind, origin, revision.body))
        if position == 0 or path not in targets
    )
    db.add_all(
        posting(link.path, sha256(link.path.encode()).hexdigest(), link.kind, position - len(declared))
        for position, link in enumerate(declared)
    )


def index_code_paths(db: AsyncSession, revision: KnowledgeRevision, locations: Sequence[str]) -> None:
    """代码页可以有多个托管地址别名，全部指向同一个发布版本。"""
    db.add_all(
        KnowledgeMarkdownPath(
            entry_id=revision.entry_id, revision=revision.revision, key=sha256(path.encode()).hexdigest(),
            kind="REFERENCES", position=0, path=path,
        ) for path in locations
    )


def _stored_declarations(revision: KnowledgeRevision) -> Select[tuple[str, str, str]]:
    paths = KnowledgeMarkdownPath
    return select(paths.kind, paths.path, paths.key).where(
        paths.entry_id == revision.entry_id, paths.revision == revision.revision, paths.position < 0,
    ).order_by(paths.position)


async def saved_declarations(db: AsyncSession, revision: KnowledgeRevision) -> list[Declaration]:
    return [Declaration(kind, path) for kind, path, _ in await db.execute(_stored_declarations(revision))]


def _path_targets(
    scoped: Select[tuple[KnowledgeEntry, KnowledgeRevision]], workspace: UUID, project: UUID | None,
    skill_directories: tuple[str, ...] | None,
) -> CTE:
    paths = KnowledgeMarkdownPath
    registry = published_skill_paths(workspace, project, skill_directories).subquery()
    native = scoped.join(
        paths, and_(paths.entry_id == KnowledgeEntry.id, paths.revision == KnowledgeRevision.revision),
    ).where(paths.position == 0).with_only_columns(
        KnowledgeEntry.id, KnowledgeEntry.project_id,
        cast(KnowledgeRevision.revision, String).label("version_key"),
        KnowledgeRevision.title, paths.path,
    ).cte("knowledge_path_assets")
    # 晋升生成的同路径 Registry 包由对应知识条目代表，历史异名包仍能通过独立路径引用。
    mirrored = select(KnowledgeEvent.id).join(native, native.c.id == KnowledgeEvent.entry_id).where(
        KnowledgeEvent.action == "skill_published",
        func.replace(KnowledgeEvent.reason, "-", "") == func.replace(cast(registry.c.id, String), "-", ""),
        native.c.project_id.is_not_distinct_from(registry.c.project_id), native.c.path == registry.c.path,
    ).exists()
    return select(native).union_all(select(
        registry.c.id, registry.c.project_id, func.replace(cast(registry.c.version_id, String), "-", ""),
        registry.c.title, registry.c.path,
    ).where(~mirrored)).cte("published_path_assets")


async def declaration_findings(
    db: AsyncSession, scoped: Select[tuple[KnowledgeEntry, KnowledgeRevision]],
    entry: KnowledgeEntry, revision: KnowledgeRevision, *, workspace: UUID, project: UUID | None,
    skill_directories: tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    """预览只报本范围内找不到唯一目标，不暴露其他范围或未发布副本。"""
    findings = list(revision.relation_findings or [])
    declared = (await db.execute(_stored_declarations(revision))).all()
    if not declared:
        return findings

    targets = _path_targets(scoped, workspace, project, skill_directories)
    available = select(targets.c.path).where(
        targets.c.path.in_({path for _, path, _ in declared}), targets.c.project_id == entry.project_id,
    ).group_by(targets.c.path).having(func.count() == 1)
    resolved = set(await db.scalars(available))
    return findings + [
        {"level": "warn", "code": "target_unavailable", "message": f"{kind} 的团队路径 {path} 暂无唯一可见目标。"}
        for kind, path, _ in declared if path not in resolved
    ]


async def markdown_relations(
    db: AsyncSession,
    scoped: Select[tuple[KnowledgeEntry, KnowledgeRevision]],
    items: Sequence[tuple[KnowledgeEntry, KnowledgeRevision]],
    *,
    workspace: UUID,
    project: UUID | None,
    skills: Sequence[PublishedSkillReference] = (),
    skill_directories: tuple[str, ...] | None = None,
) -> dict[UUID, list[dict[str, str]]]:
    versions = {
        entry.id: str(revision.revision) for entry, revision in items
        if entry.kind in {"doc", "rule", "learning", "skill", "code"}
        and entry.status == "active" and entry.published_revision == revision.revision
    }
    versions.update((skill.id, skill.version_id.hex) for skill in skills)
    if not versions:
        return {}

    paths = KnowledgeMarkdownPath
    active = scoped.with_only_columns(
        KnowledgeEntry.id, KnowledgeEntry.project_id, KnowledgeRevision.revision, KnowledgeRevision.title,
    ).cte("active_path_entries")
    origin = active.alias("markdown_origin")
    assets = _path_targets(scoped, workspace, project, skill_directories)
    target = select(
        assets, func.count().over(partition_by=(assets.c.project_id, assets.c.path)).label("copies"),
    ).cte("active_markdown_targets")
    reference = aliased(KnowledgeMarkdownPath)
    origin_path = select(func.min(paths.path)).where(
        paths.entry_id == origin.c.id, paths.revision == origin.c.revision, paths.position == 0,
    ).correlate(origin).scalar_subquery()

    # 先确认同范围目标唯一，再筛选本次命中；查询没有返回的副本同样会造成歧义。
    statement = select(
        origin.c.id, origin.c.revision, origin.c.title, reference.kind, reference.position,
        target.c.id, target.c.version_key, target.c.title,
    ).select_from(origin).join(reference, and_(
        reference.entry_id == origin.c.id, reference.revision == origin.c.revision, reference.position != 0,
    )).join(target, and_(
        target.c.path == reference.path, target.c.project_id.is_not_distinct_from(origin.c.project_id),
        target.c.copies == 1, origin.c.id != target.c.id,
    )).where(or_(origin.c.id.in_(versions), target.c.id.in_(versions))).order_by(
        origin_path, origin.c.id, reference.position,
    )

    outgoing: dict[UUID, list[dict[str, str]]] = defaultdict(list)
    incoming: dict[UUID, list[dict[str, str]]] = defaultdict(list)
    for source, source_revision, source_title, kind, position, dest, dest_revision, dest_title in await db.execute(
        statement,
    ):
        if versions.get(source) == str(source_revision):
            outgoing[source].append(_view(kind, dest, dest_title, "out", declared=position < 0))
        if versions.get(dest) == dest_revision:
            incoming[dest].append(_view(kind, source, source_title, "in", declared=position < 0))
    return {entry: outgoing[entry] + incoming[entry] for entry in versions if entry in outgoing or entry in incoming}


def _view(kind: str, target: UUID, title: str, direction: str, *, declared: bool) -> dict[str, str]:
    relation = {"kind": kind, "target": str(target), "title": title, "direction": direction}
    if declared:
        relation["via"] = "declared_path"
    return relation
