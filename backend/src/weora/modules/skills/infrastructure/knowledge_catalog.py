"""向项目知识库提供只读的当前发布 Skill 目录。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import CTE, Subquery

from weora.modules.skills.infrastructure.models import (
    SkillModel,
    SkillSearchTerm,
    SkillSearchVersion,
    SkillVersionModel,
)
from weora.modules.skills.infrastructure.skill_selection import (
    ranked_skill_sources,
    skill_tree_path,
    visible_skill_directories,
    visible_skill_scope,
)


@dataclass(frozen=True, slots=True)
class PublishedSkillReference:
    """关系绑定稳定资产 ID，打开正文时使用当次可见的发布版本。"""

    id: UUID
    project_id: UUID | None
    version_id: UUID
    title: str
    path: str
    version: str
    author: UUID
    published_at: datetime
    content_sha256: str


@dataclass(frozen=True, slots=True)
class SkillSearchView:
    reference: PublishedSkillReference
    tags: list[str]
    excerpt: str


def published_skill_paths(
    workspace: UUID, project: UUID | None, directories: tuple[str, ...] | None = None,
    *, include_shared: bool = True,
) -> Select[tuple[
    UUID, UUID | None, UUID, str, str, str, UUID, datetime, str,
]]:
    """只返回当前项目与共享范围可用的版本，不暴露包地址和草稿。"""
    skill, version = SkillModel, SkillVersionModel
    return select(
        skill.id, skill.project_id, version.id.label("version_id"), skill.display_name.label("title"),
        skill_tree_path().label("path"), version.version,
        version.published_by.label("author"), version.published_at, version.content_sha256,
    ).select_from(skill).join(version, (
        (version.id == skill.latest_published_version_id)
        & (version.skill_id == skill.id) & (version.workspace_id == workspace)
    )).where(
        visible_skill_scope(workspace, project, include_shared=include_shared),
        visible_skill_directories(directories), skill.status == "active",
    )


def searchable_skill_versions(
    workspace: UUID, project: UUID | None, *, delivered: bool = True, directories: tuple[str, ...] | None = None,
    include_shared: bool = True,
) -> CTE:
    paths = published_skill_paths(workspace, project, directories, include_shared=include_shared)
    if delivered:
        sources = ranked_skill_sources(workspace, project, directories, include_shared=include_shared)
        paths = paths.join(sources, SkillModel.id == sources.c.id).where(
            sources.c.source_rank == 1, sources.c.namespace_copies <= 1,
        )
    available = paths.subquery()
    return select(
        available.c.id, available.c.project_id, available.c.version_id, SkillSearchVersion.title,
        available.c.path, available.c.version, available.c.author, available.c.published_at,
        available.c.content_sha256, SkillSearchVersion.tags, SkillSearchVersion.excerpt,
        SkillSearchVersion.domain, SkillSearchVersion.title_length, SkillSearchVersion.length,
    ).join(SkillSearchVersion, SkillSearchVersion.version_id == available.c.version_id).cte("searchable_skill_versions")


async def skill_search_views(
    db: AsyncSession, catalog: CTE, limit: int, versions: Sequence[UUID] | None,
) -> list[SkillSearchView]:
    if versions is not None and not versions:
        return []
    statement = select(catalog).order_by(catalog.c.published_at.desc(), catalog.c.id).limit(limit)
    if versions is not None:
        statement = statement.where(catalog.c.version_id.in_(versions))
    return [
        SkillSearchView(PublishedSkillReference(*row[:9]), row.tags, row.excerpt)
        for row in await db.execute(statement)
    ]


def matching_skill_terms(
    workspace: UUID, terms: Sequence[str] = (), *, versions: Sequence[UUID] = (),
) -> Subquery:
    """按查询词取命中，或按已经授权的发布版本取完整词表。"""
    table = SkillSearchTerm
    criterion = table.version_id.in_(versions) if versions else table.term.in_(terms)
    return select(table.version_id, table.term, table.fields, table.frequency).where(
        table.workspace_id == workspace, criterion,
    ).subquery("matching_skill_terms")


def skill_release_times() -> Select[tuple[UUID, UUID, datetime]]:
    """只暴露版本身份和发生时间，权限由已绑定的可见资产负责。"""
    return select(SkillVersionModel.id, SkillVersionModel.skill_id, SkillVersionModel.published_at)


async def published_skill_reference(
    db: AsyncSession, workspace: UUID, project: UUID | None, *,
    identity: UUID | None = None, path: str | None = None, lock: bool = False,
    directories: tuple[str, ...] | None = None, include_shared: bool = True,
) -> PublishedSkillReference | None:
    statement = published_skill_paths(workspace, project, directories, include_shared=include_shared)
    if identity is not None:
        statement = statement.where(SkillModel.id == identity)
    elif path is not None and path.startswith("skills/"):
        directory, _, slug = path.removeprefix("skills/").rpartition("/")
        statement = statement.where(SkillModel.namespace == directory, SkillModel.slug == slug).order_by(
            SkillModel.project_id.is_(None),
        ).limit(1)
    else:
        return None
    if lock:
        statement = statement.with_for_update(of=SkillModel)
    row = (await db.execute(statement)).one_or_none()
    return PublishedSkillReference(*row) if row is not None else None
