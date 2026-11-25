"""按工作空间清单解析职能与项目目录，授权继续由身份和项目服务执行。"""

from datetime import UTC, datetime
from re import fullmatch
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.identity.infrastructure.unit_of_work import SqlAlchemyIdentityStore
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import KnowledgeCodebase, KnowledgeNamespaceManifest

Directory = Annotated[str, StringConstraints(pattern=r"^[\w](?:[\w.-]{0,62}[\w-])?$")]
WikiDirectory = Annotated[str, StringConstraints(max_length=256, pattern=r"^\w(?:[\w .-]*[\w-])?$")]


class ResourceFolders(BaseModel):
    """同一个目录名可在不同资源根下使用。"""

    model_config = ConfigDict(extra="forbid")
    learnings: list[Directory] = Field(default_factory=list, max_length=256)
    knowledge: list[Directory] = Field(default_factory=list, max_length=256)
    docs: list[Directory] = Field(default_factory=list, max_length=256)
    skills: list[Directory] = Field(default_factory=list, max_length=256)
    wiki: list[WikiDirectory] = Field(default_factory=list, max_length=256)
    scorers: list[Directory] = Field(default_factory=list, max_length=256)


class NamespaceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: dict[str, ResourceFolders] = Field(default_factory=dict)
    projects: dict[UUID, ResourceFolders] = Field(default_factory=dict)
    members: dict[UUID, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_assignments(self) -> Self:
        if any(not fullmatch(r"[\w][\w.-]{0,63}", role) for role in self.roles):
            raise ValueError("职能名称须为有效标识。")
        if set(self.members.values()) - self.roles.keys():
            raise ValueError("成员引用了未声明的职能。")
        declarations = [*self.roles.values(), *self.projects.values()]
        for family in ("learnings", "knowledge", "docs", "skills", "wiki", "scorers"):
            if len({item for data in declarations for item in getattr(data, family)}) > 256:
                raise ValueError("每类资源最多声明 256 个目录。")
        return self


class NamespaceUpdate(NamespaceManifest):
    base_revision: int = Field(ge=0, strict=True)


class NamespaceSelection(BaseModel):
    """本次调用的目录集合，None 表示沿用历史行为。"""

    model_config = ConfigDict(frozen=True)
    revision: int
    role: str | None
    learnings: tuple[str, ...]
    knowledge: tuple[str, ...]
    skills: tuple[str, ...]
    docs: tuple[str, ...]
    inactive_docs: tuple[str, ...]
    wiki: tuple[str, ...]
    inactive_wiki: tuple[str, ...]
    scorers: tuple[str, ...]
    inactive_scorers: tuple[str, ...]

    def view(self) -> dict[str, object]:
        """成员能看到生效目录，内部排除项仅用于查询。"""
        return self.model_dump(exclude={"inactive_docs", "inactive_wiki", "inactive_scorers"})


async def current_manifest(db: AsyncSession, workspace: UUID) -> tuple[int, NamespaceManifest]:
    row = await db.scalar(select(KnowledgeNamespaceManifest).where(
        KnowledgeNamespaceManifest.workspace_id == workspace,
    ).order_by(KnowledgeNamespaceManifest.revision.desc()).limit(1))
    if row is None:
        return 0, NamespaceManifest()
    return row.revision, NamespaceManifest.model_validate(row.manifest)


async def resolve_namespaces(
    db: AsyncSession, workspace: UUID, user: UUID, project: UUID | None,
) -> NamespaceSelection | None:
    revision, manifest = await current_manifest(db, workspace)
    return select_namespaces(revision, manifest, user, project)


def select_namespaces(
    revision: int, manifest: NamespaceManifest, user: UUID, project: UUID | None,
) -> NamespaceSelection | None:
    role = manifest.members.get(user)
    if role is None and not manifest.projects:
        return None
    role_folders = manifest.roles[role] if role is not None else ResourceFolders()
    project_folders = manifest.projects.get(project, ResourceFolders()) if project is not None else ResourceFolders()
    docs = set(role_folders.docs) | set(project_folders.docs)
    wiki = set(role_folders.wiki) | set(project_folders.wiki)
    scorers = set(role_folders.scorers) | set(project_folders.scorers)
    declarations = (*manifest.roles.values(), *manifest.projects.values())
    declared_docs = {item for folder in declarations for item in folder.docs}
    declared_wiki = {item.casefold() for folder in declarations for item in folder.wiki}
    declared_scorers = {item for folder in declarations for item in folder.scorers}
    return NamespaceSelection(
        revision=revision, role=role, docs=tuple(sorted(docs)), inactive_docs=tuple(sorted(declared_docs - docs)),
        wiki=tuple(sorted(wiki)), inactive_wiki=tuple(sorted(declared_wiki - {item.casefold() for item in wiki})),
        scorers=tuple(sorted(scorers)), inactive_scorers=tuple(sorted(declared_scorers - scorers)),
        learnings=tuple(sorted(set(role_folders.learnings + project_folders.learnings))),
        knowledge=tuple(sorted(set(role_folders.knowledge + project_folders.knowledge))),
        skills=tuple(sorted(set(role_folders.skills + project_folders.skills))),
    )


async def hidden_wiki_ids(
    db: AsyncSession, workspace: UUID, project: UUID | None, selection: NamespaceSelection | None,
) -> tuple[UUID, ...]:
    """仓库别名指向一个快照，切换大小写或路径别名均不能绕过目录筛选。"""
    if project is None or selection is None or not selection.inactive_wiki:
        return ()
    inactive = set(selection.inactive_wiki)
    index = KnowledgeCodebase
    rows = await db.execute(select(index.id, index.wiki_namespaces).where(
        index.workspace_id == workspace, index.project_id == project,
    ))
    return tuple(key for key, aliases in rows if any(alias.casefold() in inactive for alias in aliases))


async def write_manifest(
    db: AsyncSession, workspace: UUID, user: UUID, value: NamespaceUpdate,
) -> dict[str, object]:
    identity = SqlAlchemyIdentityStore(db)
    if not await identity.lock_workspace(workspace):
        raise KnowledgeError("KNOWLEDGE_NOT_FOUND", "工作空间不可用。", 404)
    # 工作空间行锁同时保护初次创建及同一基准上的并发提交。
    revision, previous = await current_manifest(db, workspace)
    contents = value.model_dump(mode="json", exclude={"base_revision"})
    if previous.model_dump(mode="json") == contents:
        return {"revision": revision, **contents}
    if value.base_revision != revision:
        raise KnowledgeError("KNOWLEDGE_CONFLICT", "目录清单已更新，请刷新后再提交。", 409)
    members = {item.id for item in await identity.list_members(workspace)}
    if value.members.keys() - members:
        raise KnowledgeError("KNOWLEDGE_NAMESPACE_MEMBER", "清单包含当前工作空间之外的成员。", 422)
    row = KnowledgeNamespaceManifest(
        workspace_id=workspace, revision=revision + 1, manifest=contents,
        created_by=user, created_at=datetime.now(UTC),
    )
    db.add(row)
    return {"revision": row.revision, **contents}
