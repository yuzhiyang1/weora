"""用 PostgreSQL 修订与引用替代团队 Git 源。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from weora.modules.team_resources.domain import FileTree, ResourceError, TreeFile, validate_tree
from weora.shared.infrastructure.database import Base


class Revision(Base):
    __tablename__ = "team_resource_revisions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "parent_id"], ["team_resource_revisions.workspace_id", "team_resource_revisions.id"]
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    parent_id: Mapped[UUID | None]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    files: Mapped[FileTree] = mapped_column(JSON)


class Head(Base):
    __tablename__ = "team_resource_heads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "revision_id"], ["team_resource_revisions.workspace_id", "team_resource_revisions.id"]
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    revision_id: Mapped[UUID | None]


class Change(Base):
    __tablename__ = "team_resource_changes"
    __table_args__ = (
        UniqueConstraint("workspace_id", "branch"),
        ForeignKeyConstraint(
            ["workspace_id", "base_id"], ["team_resource_revisions.workspace_id", "team_resource_revisions.id"]
        ),
        ForeignKeyConstraint(
            ["workspace_id", "revision_id"], ["team_resource_revisions.workspace_id", "team_resource_revisions.id"]
        ),
        ForeignKeyConstraint(
            ["workspace_id", "merged_id"], ["team_resource_revisions.workspace_id", "team_resource_revisions.id"]
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    branch: Mapped[str] = mapped_column(String(120))
    author_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    base_id: Mapped[UUID | None]
    revision_id: Mapped[UUID]
    merged_id: Mapped[UUID | None]


class Tag(Base):
    __tablename__ = "team_resource_tags"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "revision_id"], ["team_resource_revisions.workspace_id", "team_resource_revisions.id"]
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    revision_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))


async def lock_head(db: AsyncSession, workspace: UUID) -> Head:
    query = select(Head).where(Head.workspace_id == workspace).with_for_update()
    row = await db.scalar(query)
    if row is None:
        # 空工作空间也需要序列化首次写入；并发插入由主键协商。
        try:
            async with db.begin_nested():
                db.add(Head(workspace_id=workspace))
                await db.flush()
        except IntegrityError:
            pass
        row = (await db.scalars(query)).one()
    return row


async def revision(db: AsyncSession, workspace: UUID, revision_id: UUID | None) -> Revision | None:
    if revision_id is None:
        return None
    row = await db.scalar(select(Revision).where(Revision.workspace_id == workspace, Revision.id == revision_id))
    if row is None:
        raise ResourceError("RESOURCE_REVISION_NOT_FOUND", "找不到团队源修订。", 404)
    return row


async def resolve(db: AsyncSession, workspace: UUID, ref: str) -> Revision | None:
    if ref == "main":
        head = await db.get(Head, workspace)
        return await revision(db, workspace, head.revision_id if head else None)
    change = await db.scalar(select(Change).where(Change.workspace_id == workspace, Change.branch == ref))
    if change:
        return await revision(db, workspace, change.revision_id)
    tag = await db.get(Tag, (workspace, ref))
    if tag:
        return await revision(db, workspace, tag.revision_id)
    try:
        identifier = UUID(ref)
    except ValueError as exc:
        raise ResourceError("RESOURCE_REVISION_NOT_FOUND", "找不到团队源引用。", 404) from exc
    return await revision(db, workspace, identifier)


async def create_tag(db: AsyncSession, workspace: UUID, actor: UUID, name: str, target: UUID) -> Tag:
    # 标签指向不可变修订，重试同一请求可复用，修改已有标签会影响固定版本的成员。
    await lock_head(db, workspace)
    tag = await db.get(Tag, (workspace, name))
    if tag is not None:
        if tag.revision_id != target:
            raise ResourceError("RESOURCE_TAG_EXISTS", "标签已指向其他修订。", 409)
        return tag
    await revision(db, workspace, target)
    tag = Tag(workspace_id=workspace, name=name, revision_id=target, actor_id=actor)
    db.add(tag)
    await db.flush()
    return tag


def replace_files(base: FileTree, updates: dict[str, TreeFile | None]) -> FileTree:
    files = base.copy()
    for path, content in updates.items():
        if content is None:
            files.pop(path, None)
        else:
            files[path] = content
    validate_tree(files)
    return files


async def save_revision(
    db: AsyncSession,
    workspace: UUID,
    actor: UUID,
    parent: UUID | None,
    files: FileTree,
) -> Revision:
    row = Revision(workspace_id=workspace, actor_id=actor, parent_id=parent, files=files)
    db.add(row)
    await db.flush()
    return row


async def write(
    db: AsyncSession,
    workspace: UUID,
    actor: UUID,
    expected: UUID | None,
    updates: dict[str, TreeFile | None],
) -> Revision | None:
    head = await lock_head(db, workspace)
    if head.revision_id != expected:
        raise ResourceError("RESOURCE_HEAD_CHANGED", "团队源已更新，请同步后重试。", 409)
    current = await revision(db, workspace, expected)
    before = current.files if current else {}
    files = replace_files(before, updates)
    if before == files:
        return current
    row = await save_revision(db, workspace, actor, expected, files)
    head.revision_id = row.id
    return row


async def propose(
    db: AsyncSession,
    workspace: UUID,
    actor: UUID,
    branch: str,
    base_id: UUID | None,
    expected: UUID | None,
    updates: dict[str, TreeFile | None],
) -> Change:
    head = await lock_head(db, workspace)
    change = await db.scalar(select(Change).where(Change.workspace_id == workspace, Change.branch == branch))
    if change is None and head.revision_id != base_id:
        raise ResourceError("RESOURCE_HEAD_CHANGED", "团队源已更新，请同步后重试。", 409)
    if change is not None and (change.author_id != actor or change.merged_id is not None):
        raise ResourceError("RESOURCE_BRANCH_UNAVAILABLE", "该分支不可更新。", 409)
    base = await revision(db, workspace, change.base_id if change else base_id)
    previous = await revision(db, workspace, change.revision_id if change else None)
    prior_files = previous.files if previous else base.files if base else {}
    files = replace_files(prior_files, updates)
    if change is not None and files == prior_files:
        return change
    if change is not None and expected != change.revision_id:
        raise ResourceError("RESOURCE_BRANCH_CHANGED", "分支已更新，请读取候选修订后重试。", 409)
    if files == prior_files:
        raise ResourceError("RESOURCE_NO_CHANGES", "没有可提交的团队规则变更。", 409)
    row = await save_revision(db, workspace, actor, change.revision_id if change else base_id, files)
    if change is None:
        change = Change(workspace_id=workspace, branch=branch, author_id=actor, base_id=base_id, revision_id=row.id)
        db.add(change)
    else:
        change.revision_id = row.id
    await db.flush()
    return change


async def merge(db: AsyncSession, workspace: UUID, actor: UUID, change_id: UUID, expected: UUID) -> Change:
    head = await lock_head(db, workspace)
    change = await db.scalar(select(Change).where(Change.workspace_id == workspace, Change.id == change_id))
    if change is None:
        raise ResourceError("RESOURCE_CHANGE_NOT_FOUND", "找不到合并请求。", 404)
    if change.revision_id != expected:
        raise ResourceError("RESOURCE_BRANCH_CHANGED", "候选规则已变化，请重新查看差异。", 409)
    if change.merged_id is not None:
        return change
    base = await revision(db, workspace, change.base_id)
    current = await revision(db, workspace, head.revision_id)
    candidate = await revision(db, workspace, change.revision_id)
    assert candidate is not None
    before, live = base.files if base else {}, current.files if current else {}
    updates = {
        path: candidate.files.get(path)
        for path in before.keys() | candidate.files.keys()
        if before.get(path) != candidate.files.get(path)
    }
    conflicts = [path for path, content in updates.items() if live.get(path) not in (before.get(path), content)]
    if conflicts:
        raise ResourceError("RESOURCE_MERGE_CONFLICT", f"下列文件已被修改：{', '.join(sorted(conflicts))}。", 409)
    row = await save_revision(db, workspace, actor, head.revision_id, replace_files(live, updates))
    head.revision_id = change.merged_id = row.id
    return change
