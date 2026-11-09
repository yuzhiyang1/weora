"""收件箱持久化。审批和任务状态仍由各自的业务表决定。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    and_,
    false,
    func,
    literal,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, aliased, mapped_column
from sqlalchemy.sql import Select, Subquery
from sqlalchemy.sql.elements import ColumnElement

from weora.modules.collaboration.application.inbox import InboxBulkAction, InboxItem
from weora.shared.infrastructure.database import Base, SessionFactory


class InboxItemModel(Base):
    __tablename__ = "inbox_items"
    __table_args__ = (
        Index("ix_inbox_recipient", "workspace_id", "recipient_type", "recipient_id", "archived", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    recipient_type: Mapped[str] = mapped_column(String(16), default="member", server_default="member")
    recipient_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    type: Mapped[str] = mapped_column(String(32))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("collaboration_tasks.id"))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id"))
    plan_id: Mapped[UUID | None] = mapped_column(ForeignKey("delivery_plans.id"))
    read: Mapped[bool] = mapped_column(default=False, server_default=false())
    archived: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class InboxRevisionModel(Base):
    """跨进程可见的收件箱版本，只在对应通知事务提交后推进。"""

    __tablename__ = "inbox_revisions"
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    recipient_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger)


async def touch_inbox(session: AsyncSession, workspace_id: UUID, recipients: set[UUID]) -> None:
    """通知写完后按固定顺序锁定收件人，避免交叉指派时反序取锁。"""
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    for recipient in sorted(recipients):
        await session.execute(
            insert(InboxRevisionModel)
            .values(workspace_id=workspace_id, recipient_id=recipient, revision=1)
            .on_conflict_do_update(
                index_elements=[InboxRevisionModel.workspace_id, InboxRevisionModel.recipient_id],
                set_={"revision": InboxRevisionModel.revision + 1},
            )
        )


async def notify_task(
    session: AsyncSession,
    workspace_id: UUID,
    task_id: UUID,
    recipient_id: UUID,
    kind: str,
) -> None:
    """由调用方和任务状态、动态一起提交。通知只保存来源引用。"""
    session.add(
        InboxItemModel(
            workspace_id=workspace_id,
            recipient_id=recipient_id,
            task_id=task_id,
            type=kind,
        )
    )


async def archive_task_inbox(
    session: AsyncSession,
    workspace_id: UUID,
    task_id: UUID,
    kind: str,
) -> set[UUID]:
    recipients = await session.scalars(
        update(InboxItemModel)
        .where(
            InboxItemModel.workspace_id == workspace_id,
            InboxItemModel.task_id == task_id,
            InboxItemModel.type == kind,
            InboxItemModel.archived.is_(False),
        )
        .values(archived=True)
        .returning(InboxItemModel.recipient_id)
    )
    return set(recipients)


async def sync_plan_inbox(
    session: AsyncSession,
    workspace_id: UUID,
    recipient_id: UUID,
    run_id: UUID,
    plan_id: UUID,
    status: str,
) -> None:
    """计划保存后同步通知，旧版本和已决事项随同一事务归档。"""
    scope = (
        InboxItemModel.workspace_id == workspace_id,
        InboxItemModel.recipient_id == recipient_id,
        InboxItemModel.run_id == run_id,
        InboxItemModel.type == "approval",
        InboxItemModel.archived.is_(False),
    )
    if status == "PENDING":
        await session.execute(update(InboxItemModel).where(*scope).values(archived=True))
        session.add(
            InboxItemModel(
                workspace_id=workspace_id,
                recipient_id=recipient_id,
                run_id=run_id,
                plan_id=plan_id,
                type="approval",
            )
        )
    elif status == "REVISION_REQUIRED":
        await session.execute(update(InboxItemModel).where(*scope).values(archived=True))
    else:
        await session.execute(
            update(InboxItemModel)
            .where(
                *scope,
                InboxItemModel.plan_id == plan_id,
            )
            .values(archived=True)
        )
    await touch_inbox(session, workspace_id, {recipient_id})


class InboxStore:
    def __init__(self, sessions: SessionFactory, run_tasks: Subquery) -> None:
        self.sessions = sessions
        self.run_tasks = run_tasks

    @staticmethod
    def _scope(workspace_id: UUID, user_id: UUID) -> tuple[ColumnElement[bool], ...]:
        return (
            InboxItemModel.workspace_id == workspace_id,
            InboxItemModel.recipient_type == "member",
            InboxItemModel.recipient_id == user_id,
        )

    @staticmethod
    def _latest_review() -> ColumnElement[bool]:
        # 已归档的旧验收通知恢复后，只有最近一轮可以显示为待验收。
        newer = aliased(InboxItemModel)
        return (
            ~select(newer.id)
            .where(
                newer.workspace_id == InboxItemModel.workspace_id,
                newer.recipient_type == InboxItemModel.recipient_type,
                newer.recipient_id == InboxItemModel.recipient_id,
                newer.task_id == InboxItemModel.task_id,
                newer.type == "review",
                tuple_(newer.created_at, newer.id) > tuple_(InboxItemModel.created_at, InboxItemModel.id),
            )
            .exists()
        )

    @staticmethod
    def _item(row: InboxItemModel, latest_review: bool) -> InboxItem:
        return InboxItem(
            row.id,
            row.type,
            row.task_id,
            row.run_id,
            row.plan_id,
            row.read,
            row.archived,
            row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC),
            latest_review,
        )

    async def list_items(self, workspace_id: UUID, user_id: UUID, archived: bool) -> list[InboxItem]:
        query = (
            select(InboxItemModel, self._latest_review())
            .where(
                *self._scope(workspace_id, user_id),
                InboxItemModel.archived.is_(archived),
            )
            .order_by(InboxItemModel.created_at.desc(), InboxItemModel.id.desc())
        )
        if archived:
            query = query.limit(100)
        async with self.sessions() as session:
            items = await session.execute(query)
            return [self._item(row, latest_review) for row, latest_review in items]

    def _task_rows(self, workspace_id: UUID, user_id: UUID) -> Select[tuple[UUID, UUID, bool, bool, datetime]]:
        # 运行通知的 Task 来自 Run 的持久关联，也覆盖 task_id 为空的旧通知。
        task = func.coalesce(InboxItemModel.task_id, self.run_tasks.c.task_id)
        return (
            select(
                InboxItemModel.id,
                task.label("task_id"),
                InboxItemModel.read,
                InboxItemModel.archived,
                InboxItemModel.created_at,
            )
            .select_from(InboxItemModel)
            .outerjoin(
                self.run_tasks,
                and_(
                    self.run_tasks.c.id == InboxItemModel.run_id,
                    self.run_tasks.c.workspace_id == InboxItemModel.workspace_id,
                ),
            )
            .where(*self._scope(workspace_id, user_id))
        )

    async def archive_page(
        self,
        workspace_id: UUID,
        user_id: UUID,
        *,
        before: tuple[datetime, UUID] | None,
        limit: int,
        unread_only: bool,
    ) -> list[InboxItem]:
        rows = self._task_rows(workspace_id, user_id).cte("inbox_scope")
        active = rows.alias("active")
        # 先对整个归档选出每个 Task 的最新记录，再套筛选和游标。
        latest = (
            select(
                rows,
                func.row_number()
                .over(
                    partition_by=rows.c.task_id,
                    order_by=(rows.c.created_at.desc(), rows.c.id.desc()),
                )
                .label("rank"),
            )
            .where(
                rows.c.archived.is_(True),
                rows.c.task_id.is_not(None),
                ~select(active.c.id).where(active.c.task_id == rows.c.task_id, active.c.archived.is_(False)).exists(),
            )
            .subquery()
        )
        query = select(InboxItemModel, self._latest_review()).join(latest, latest.c.id == InboxItemModel.id)
        query = query.where(latest.c.rank == 1)
        if unread_only:
            query = query.where(latest.c.read.is_(False))
        if before:
            query = query.where(
                tuple_(latest.c.created_at, latest.c.id) < tuple_(literal(before[0]), literal(before[1]))
            )
        query = query.order_by(latest.c.created_at.desc(), latest.c.id.desc()).limit(limit)
        async with self.sessions() as session:
            items = await session.execute(query)
            return [self._item(row, latest_review) for row, latest_review in items]

    async def set_group_archive(self, workspace_id: UUID, user_id: UUID, task_id: UUID, archived: bool) -> bool:
        rows = self._task_rows(workspace_id, user_id).subquery()
        ids = select(rows.c.id).where(rows.c.task_id == task_id)
        async with self.sessions() as session, session.begin():
            existing = await session.scalar(ids.limit(1))
            if existing is None:
                return False
            changed = await session.scalar(
                update(InboxItemModel)
                .where(
                    *self._scope(workspace_id, user_id),
                    InboxItemModel.id.in_(ids),
                    InboxItemModel.archived.is_not(archived),
                )
                .values(archived=archived)
                .returning(InboxItemModel.id)
            )
            if changed is not None:
                await touch_inbox(session, workspace_id, {user_id})
            return True

    async def bulk(self, workspace_id: UUID, user_id: UUID, action: InboxBulkAction) -> None:
        active = update(InboxItemModel).where(*self._scope(workspace_id, user_id), InboxItemModel.archived.is_(False))
        if action == "read_all":
            query = active.where(InboxItemModel.read.is_(False)).values(read=True)
        else:
            query = active.values(archived=True)
            if action == "archive_read":
                rows = self._task_rows(workspace_id, user_id).where(InboxItemModel.archived.is_(False)).cte()
                latest = select(
                    rows.c.task_id,
                    rows.c.read,
                    func.row_number()
                    .over(
                        partition_by=rows.c.task_id,
                        order_by=(rows.c.created_at.desc(), rows.c.id.desc()),
                    )
                    .label("rank"),
                ).subquery()
                read_tasks = select(latest.c.task_id).where(latest.c.rank == 1, latest.c.read.is_(True))
                query = query.where(InboxItemModel.id.in_(select(rows.c.id).where(rows.c.task_id.in_(read_tasks))))
        async with self.sessions() as session, session.begin():
            changed = await session.scalar(query.returning(InboxItemModel.id))
            if changed is not None:
                await touch_inbox(session, workspace_id, {user_id})

    async def revision(self, workspace_id: UUID, user_id: UUID) -> int:
        async with self.sessions() as session:
            value = await session.scalar(
                select(InboxRevisionModel.revision).where(
                    InboxRevisionModel.workspace_id == workspace_id,
                    InboxRevisionModel.recipient_id == user_id,
                )
            )
            return value or 0

    async def update(
        self,
        workspace_id: UUID,
        user_id: UUID,
        item_ids: list[UUID],
        *,
        read: bool | None = None,
        archived: bool | None = None,
    ) -> bool:
        values = {key: value for key, value in {"read": read, "archived": archived}.items() if value is not None}
        async with self.sessions() as session, session.begin():
            changed = await session.scalars(
                update(InboxItemModel)
                .where(
                    *self._scope(workspace_id, user_id),
                    InboxItemModel.id.in_(item_ids),
                )
                .values(**values)
                .returning(InboxItemModel.id)
            )
            if set(changed) != set(item_ids):
                await session.rollback()
                return False
            await touch_inbox(session, workspace_id, {user_id})
            return True
