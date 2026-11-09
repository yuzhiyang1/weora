"""任务表和事务内存储；仅由协作模块的应用接口对外提供写入。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from weora.modules.collaboration.application.tasks import TaskInput, TaskRecord
from weora.shared.infrastructure.database import Base


class TaskModel(Base):
    """用户目标与固定私有受众，暂不提供转为群聊或共享任务的入口。"""

    __tablename__ = "collaboration_tasks"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", "created_by", name="uq_tasks_private_owner"),
        CheckConstraint(
            "(origin = 'console' AND audience = 'private') OR "
            "(origin IN ('feishu', 'code_hosting') AND audience = 'group')",
            name="ck_tasks_origin_audience",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    instruction: Mapped[str] = mapped_column(Text())
    origin: Mapped[str] = mapped_column(String(16), default="console")
    audience: Mapped[str] = mapped_column(String(16), default="private")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    origin_metadata: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)
    project_context: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)
    input_sequence: Mapped[int] = mapped_column(Integer(), default=0, server_default="0")


class TaskInputModel(Base):
    """消息先入库再派工；后续 Run 的 ID 是跨模块关联，不授予执行权限。"""

    __tablename__ = "task_inputs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "workspace_id", "created_by"],
            ["collaboration_tasks.id", "collaboration_tasks.workspace_id", "collaboration_tasks.created_by"],
        ),
        UniqueConstraint("workspace_id", "created_by", "idempotency_key", name="uq_task_input_key"),
        UniqueConstraint("task_id", "sequence", name="uq_task_input_sequence"),
        UniqueConstraint("run_id", name="uq_task_input_run"),
        CheckConstraint("status IN ('PENDING','DISPATCHED','REJECTED')", name="ck_task_input_status"),
        Index("ix_task_inputs_pending", "status", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    task_id: Mapped[UUID] = mapped_column()
    workspace_id: Mapped[UUID] = mapped_column()
    created_by: Mapped[UUID] = mapped_column()
    anchor_run_id: Mapped[UUID] = mapped_column()
    instruction: Mapped[str] = mapped_column(Text())
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sequence: Mapped[int] = mapped_column(Integer())
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    origin_metadata: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)


class TaskThreadModel(Base):
    """来源线程只关联同一发起人与固定路由版本，不把整个群当成一个任务。"""

    __tablename__ = "task_threads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "workspace_id", "created_by"],
            ["collaboration_tasks.id", "collaboration_tasks.workspace_id", "collaboration_tasks.created_by"],
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(primary_key=True)
    created_by: Mapped[UUID] = mapped_column(primary_key=True)
    thread_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[UUID] = mapped_column()
    anchor_run_id: Mapped[UUID] = mapped_column()


def _input(row: TaskInputModel) -> TaskInput:
    return TaskInput(
        id=row.id, task_id=row.task_id, workspace_id=row.workspace_id, created_by=row.created_by,
        anchor_run_id=row.anchor_run_id, instruction=row.instruction, idempotency_key=row.idempotency_key,
        request_sha256=row.request_sha256, request_id=row.request_id, created_at=row.created_at,
        sequence=row.sequence, run_id=row.run_id, status=row.status, origin_metadata=row.origin_metadata,
    )


class SqlAlchemyTaskStore:
    """使用外层提供的同一 Session，保证 Task 与 Run 不会部分提交。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, task: TaskRecord) -> None:
        """先 flush 父记录，但不提交；后续 Run 写入失败仍整体回滚。"""
        self._session.add(
            TaskModel(
                id=task.id,
                workspace_id=task.workspace_id,
                created_by=task.created_by,
                instruction=task.instruction,
                created_at=task.created_at,
                origin=task.origin,
                audience=task.audience,
                origin_metadata=task.origin_metadata,
                project_context=task.project_context,
            )
        )
        await self._session.flush()

    async def purge_content(self, workspace_id: UUID, task_ids: set[UUID]) -> None:
        await self._session.execute(
            update(TaskInputModel)
            .where(TaskInputModel.workspace_id == workspace_id, TaskInputModel.task_id.in_(task_ids))
            .values(instruction="[正文已过期]", origin_metadata=None, status="REJECTED")
        )
        await self._session.execute(
            update(TaskModel)
            .where(TaskModel.workspace_id == workspace_id, TaskModel.id.in_(task_ids))
            .values(instruction="[正文已过期]", project_context=None)
        )

    async def get_private(self, *, workspace_id: UUID, task_id: UUID, user_id: UUID) -> TaskRecord | None:
        """查询始终同时限定任务、工作空间和发起人。"""
        model = await self._session.scalar(
            select(TaskModel).where(
                TaskModel.id == task_id,
                TaskModel.workspace_id == workspace_id,
                TaskModel.created_by == user_id,
            )
        )
        if model is None:
            return None
        return TaskRecord(
            id=model.id,
            workspace_id=model.workspace_id,
            created_by=model.created_by,
            instruction=model.instruction,
            created_at=model.created_at.replace(tzinfo=UTC) if model.created_at.tzinfo is None else model.created_at,
            origin=model.origin,
            audience=model.audience,
            origin_metadata=model.origin_metadata,
            project_context=model.project_context,
        )

    async def lock_private(self, workspace_id: UUID, task_id: UUID, user_id: UUID) -> TaskRecord | None:
        # 短写锁同时适用于 PostgreSQL 与 SQLite，避免两个跟进争抢最后一个 Run 槽位。
        changed = await self._session.scalar(
            update(TaskModel)
            .where(TaskModel.id == task_id, TaskModel.workspace_id == workspace_id, TaskModel.created_by == user_id)
            .values(input_sequence=TaskModel.input_sequence).returning(TaskModel.id)
        )
        return None if changed is None else await self.get_private(
            workspace_id=workspace_id, task_id=task_id, user_id=user_id,
        )

    async def add_input(self, item: TaskInput) -> TaskInput:
        sequence = await self._session.scalar(
            update(TaskModel).where(
                TaskModel.id == item.task_id, TaskModel.workspace_id == item.workspace_id,
                TaskModel.created_by == item.created_by,
            ).values(input_sequence=TaskModel.input_sequence + 1).returning(TaskModel.input_sequence)
        )
        if sequence is None:
            raise ValueError("任务不存在")
        row = TaskInputModel(
            id=item.id, task_id=item.task_id, workspace_id=item.workspace_id, created_by=item.created_by,
            anchor_run_id=item.anchor_run_id, instruction=item.instruction, idempotency_key=item.idempotency_key,
            request_sha256=item.request_sha256, request_id=item.request_id, created_at=item.created_at,
            sequence=sequence, checked_at=item.created_at, status="PENDING", origin_metadata=item.origin_metadata,
        )
        self._session.add(row)
        await self._session.flush()
        return _input(row)

    async def input_by_key(self, workspace_id: UUID, user_id: UUID, key: str) -> TaskInput | None:
        row = await self._session.scalar(select(TaskInputModel).where(
            TaskInputModel.workspace_id == workspace_id, TaskInputModel.created_by == user_id,
            TaskInputModel.idempotency_key == key,
        ))
        return _input(row) if row else None

    async def input_by_id(self, workspace_id: UUID, user_id: UUID, input_id: UUID) -> TaskInput | None:
        row = await self._session.scalar(select(TaskInputModel).where(
            TaskInputModel.id == input_id, TaskInputModel.workspace_id == workspace_id,
            TaskInputModel.created_by == user_id,
        ))
        return _input(row) if row else None

    async def input_for_run(self, workspace_id: UUID, run_id: UUID) -> TaskInput | None:
        row = await self._session.scalar(select(TaskInputModel).where(
            TaskInputModel.workspace_id == workspace_id, TaskInputModel.run_id == run_id,
        ))
        return _input(row) if row else None

    async def inputs(self, workspace_id: UUID, task_id: UUID, user_id: UUID) -> list[TaskInput]:
        rows = await self._session.scalars(select(TaskInputModel).where(
            TaskInputModel.workspace_id == workspace_id, TaskInputModel.task_id == task_id,
            TaskInputModel.created_by == user_id,
        ).order_by((TaskInputModel.status == "PENDING").desc(), TaskInputModel.sequence).limit(100))
        # 待处理项优先进入有界窗口，长期任务不会因已有 100 条历史而饿死新输入。
        return sorted((_input(row) for row in rows), key=lambda item: item.sequence)

    async def assign_input(self, item: TaskInput, run_id: UUID) -> None:
        await self._session.execute(update(TaskInputModel).where(
            TaskInputModel.id == item.id, TaskInputModel.status == "PENDING",
        ).values(status="DISPATCHED", run_id=run_id))

    async def reject_input(self, item: TaskInput) -> None:
        await self._session.execute(update(TaskInputModel).where(
            TaskInputModel.id == item.id, TaskInputModel.status == "PENDING",
        ).values(status="REJECTED"))

    async def defer_input(self, item: TaskInput) -> None:
        # 检查过的繁忙目标移到下一批后面，不能永远占住维护扫描的前 50 项。
        await self._session.execute(update(TaskInputModel).where(
            TaskInputModel.id == item.id, TaskInputModel.status == "PENDING",
        ).values(checked_at=datetime.now(UTC)))

    async def pending_inputs(self, limit: int) -> list[TaskInput]:
        first = select(TaskInputModel.id, func.row_number().over(
            partition_by=TaskInputModel.task_id, order_by=TaskInputModel.sequence,
        ).label("position")).where(TaskInputModel.status == "PENDING").subquery()
        rows = await self._session.scalars(select(TaskInputModel).join(
            first, TaskInputModel.id == first.c.id,
        ).where(first.c.position == 1).order_by(TaskInputModel.checked_at, TaskInputModel.id).limit(limit))
        return [_input(row) for row in rows]

    async def thread_run(self, workspace_id: UUID, user_id: UUID, key: str) -> UUID | None:
        result: UUID | None = await self._session.scalar(select(TaskThreadModel.anchor_run_id).where(
            TaskThreadModel.workspace_id == workspace_id, TaskThreadModel.created_by == user_id,
            TaskThreadModel.thread_key == key,
        ))
        return result

    async def bind_thread(self, task: TaskRecord, key: str, run_id: UUID) -> None:
        self._session.add(TaskThreadModel(
            workspace_id=task.workspace_id, created_by=task.created_by, thread_key=key,
            task_id=task.id, anchor_run_id=run_id,
        ))
