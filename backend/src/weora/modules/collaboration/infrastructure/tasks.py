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
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from weora.modules.collaboration.application.tasks import (
    TaskAssignment,
    TaskCollaboration,
    TaskInput,
    TaskRecord,
    TaskStatus,
)
from weora.modules.collaboration.infrastructure.inbox import archive_task_inbox, notify_task, touch_inbox
from weora.shared.infrastructure.database import Base


class TaskModel(Base):
    """原始目标和渠道受众独立于显式发布的控制台协作区。"""

    __tablename__ = "collaboration_tasks"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", "created_by", name="uq_tasks_private_owner"),
        UniqueConstraint("id", "workspace_id", name="uq_tasks_workspace"),
        CheckConstraint(
            "status IN ('backlog', 'todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled')",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "(assignee_type IS NULL AND assignee_id IS NULL) OR "
            "(assignee_type IS NOT NULL AND assignee_type IN ('member', 'agent') AND assignee_id IS NOT NULL)",
            name="ck_tasks_assignee",
        ),
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
    title: Mapped[str | None] = mapped_column(String(200))
    # 非空表示待生成或已领取。领取时把时间推迟，Worker 崩溃后可重新领取。
    title_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    origin: Mapped[str] = mapped_column(String(16), default="console")
    audience: Mapped[str] = mapped_column(String(16), default="private")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    origin_metadata: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)
    project_context: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)
    input_sequence: Mapped[int] = mapped_column(Integer(), default=0, server_default="0")
    collaboration_title: Mapped[str | None] = mapped_column(String(160))
    collaboration_summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(String(16), default="backlog", server_default="backlog")
    assignee_type: Mapped[str | None] = mapped_column(String(16))
    assignee_id: Mapped[UUID | None] = mapped_column()
    assignee_name: Mapped[str | None] = mapped_column(String(120))
    assignee_version: Mapped[str | None] = mapped_column(String(64))
    collaboration_manager_id: Mapped[UUID | None] = mapped_column()
    # 仅用于结清旧版已受理的交接请求。
    collaboration_pending: Mapped[dict[str, object] | None] = mapped_column(JSON(none_as_null=True))
    collaboration_revision: Mapped[int] = mapped_column(default=0, server_default="0")


class TaskParticipantModel(Base):
    """显式获准访问协作摘要的工作空间成员。"""

    __tablename__ = "task_participants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "workspace_id"], ["collaboration_tasks.id", "collaboration_tasks.workspace_id"],
        ),
        Index("ix_task_participants_member", "workspace_id", "user_id"),
    )

    task_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)


class TaskActivityModel(Base):
    """协作动作的审计记录与任务修订号保持一致。"""

    __tablename__ = "task_activities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "workspace_id"], ["collaboration_tasks.id", "collaboration_tasks.workspace_id"],
        ),
    )

    task_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    event: Mapped[dict[str, object]] = mapped_column(JSON)


def _assignment(data: dict[str, object] | None) -> TaskAssignment | None:
    if data is None:
        return None
    version = data.get("version")
    return TaskAssignment(
        str(data["kind"]), UUID(str(data["id"])), str(data["name"]), str(version) if version else None,
    )


def _assignment_data(item: TaskAssignment | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {"kind": item.kind, "id": str(item.id), "name": item.name, "version": item.version}


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

    async def collaboration(self, workspace_id: UUID, task_id: UUID, *, lock: bool = False) -> TaskCollaboration | None:
        scope = (TaskModel.workspace_id == workspace_id, TaskModel.id == task_id)
        if lock:
            # 与私有续接使用同一行写锁，兼容 PostgreSQL 和 SQLite。
            await self._session.execute(update(TaskModel).where(*scope).values(input_sequence=TaskModel.input_sequence))
        model = await self._session.scalar(select(TaskModel).where(*scope).execution_options(populate_existing=True))
        return await self._collaboration(model) if model else None

    async def _collaboration(self, model: TaskModel) -> TaskCollaboration:
        members = await self._session.scalars(select(TaskParticipantModel.user_id).where(
            TaskParticipantModel.task_id == model.id, TaskParticipantModel.workspace_id == model.workspace_id,
        ).order_by(TaskParticipantModel.user_id))
        pending = model.collaboration_pending
        assignment = (
            TaskAssignment(model.assignee_type, model.assignee_id, model.assignee_name or "", model.assignee_version)
            if model.assignee_type and model.assignee_id else None
        )
        return TaskCollaboration(
            model.id, model.workspace_id, model.created_by, model.collaboration_title, model.collaboration_summary,
            tuple(members), assignment, model.collaboration_manager_id,
            _assignment(pending),
            UUID(str(pending["actor_id"])) if pending else None, model.collaboration_revision,
            model.status,
        )

    async def list_collaborations(self, workspace_id: UUID, user_id: UUID) -> list[TaskCollaboration]:
        member_tasks = select(TaskParticipantModel.task_id).where(
            TaskParticipantModel.workspace_id == workspace_id, TaskParticipantModel.user_id == user_id,
        )
        rows = await self._session.scalars(select(TaskModel).where(
            TaskModel.workspace_id == workspace_id, TaskModel.collaboration_title.is_not(None),
            or_(TaskModel.created_by == user_id, TaskModel.id.in_(member_tasks)),
        ).order_by(TaskModel.created_at.desc(), TaskModel.id.desc()).limit(100))
        return [await self._collaboration(model) for model in rows]

    async def save_collaboration(self, state: TaskCollaboration) -> None:
        pending = _assignment_data(state.pending)
        if pending:
            pending["actor_id"] = str(state.pending_by)
        assignment = state.assignment
        await self._session.execute(update(TaskModel).where(
            TaskModel.workspace_id == state.workspace_id, TaskModel.id == state.task_id,
        ).values(
            collaboration_title=state.title, collaboration_summary=state.summary,
            assignee_type=assignment.kind if assignment else None,
            assignee_id=assignment.id if assignment else None,
            assignee_name=assignment.name if assignment else None,
            assignee_version=assignment.version if assignment else None,
            collaboration_manager_id=state.manager_id,
            collaboration_pending=pending, collaboration_revision=state.revision, status=state.status,
        ))
        scope = (TaskParticipantModel.task_id == state.task_id, TaskParticipantModel.workspace_id == state.workspace_id)
        existing = set(await self._session.scalars(select(TaskParticipantModel.user_id).where(*scope)))
        removed = existing.difference(state.participants)
        if removed:
            await self._session.execute(delete(TaskParticipantModel).where(
                *scope, TaskParticipantModel.user_id.in_(removed),
            ))
        self._session.add_all([
            TaskParticipantModel(task_id=state.task_id, workspace_id=state.workspace_id, user_id=user_id)
            for user_id in sorted(set(state.participants) - existing)
        ])

    async def add_activity(self, state: TaskCollaboration, event: dict[str, object]) -> None:
        self._session.add(TaskActivityModel(
            task_id=state.task_id, workspace_id=state.workspace_id, revision=state.revision,
            event=event | {"at": datetime.now(UTC).isoformat()},
        ))
        workspace_id, task_id = state.workspace_id, state.task_id
        action = event["action"]
        recipients: set[UUID] = set()
        if action == "review_submitted":
            recipients |= await archive_task_inbox(self._session, workspace_id, task_id, "review_returned")
            await notify_task(self._session, workspace_id, task_id, state.owner_id, "review")
            recipients.add(state.owner_id)
        elif action in ("review_accepted", "review_returned"):
            recipients |= await archive_task_inbox(self._session, workspace_id, task_id, "review")
            recipient = state.manager_id or state.owner_id
            if action == "review_returned" and str(recipient) != event["actor_id"]:
                await notify_task(self._session, workspace_id, task_id, recipient, "review_returned")
                recipients.add(recipient)
        elif event.get("assignment_changed"):
            recipients |= await archive_task_inbox(self._session, workspace_id, task_id, "assignment")
            target = state.assignment
            if target and target.kind == "member" and str(target.id) != event["actor_id"]:
                await notify_task(self._session, workspace_id, task_id, target.id, "assignment")
                recipients.add(target.id)
        await touch_inbox(self._session, workspace_id, recipients)

    async def activities(self, workspace_id: UUID, task_id: UUID) -> list[dict[str, object]]:
        rows = await self._session.scalars(select(TaskActivityModel.event).where(
            TaskActivityModel.workspace_id == workspace_id, TaskActivityModel.task_id == task_id,
        ).order_by(TaskActivityModel.revision.desc()).limit(30))
        return list(rows)

    async def pending_collaborations(self, limit: int) -> list[tuple[UUID, UUID]]:
        rows = await self._session.execute(select(TaskModel.workspace_id, TaskModel.id).where(
            TaskModel.collaboration_pending.is_not(None),
        ).order_by(
            func.coalesce(TaskModel.collaboration_pending["checked_at"].as_string(), ""),
            TaskModel.created_at, TaskModel.id,
        ).limit(limit))
        return [(workspace_id, task_id) for workspace_id, task_id in rows]

    async def defer_collaboration(self, workspace_id: UUID, task_id: UUID) -> None:
        """等待活跃 Run 的交接移到扫描队尾，避免长任务饿死已可交接的任务。"""
        scope = (TaskModel.workspace_id == workspace_id, TaskModel.id == task_id)
        pending = await self._session.scalar(select(TaskModel.collaboration_pending).where(*scope))
        if pending is not None:
            await self._session.execute(update(TaskModel).where(*scope).values(
                collaboration_pending=pending | {"checked_at": datetime.now(UTC).isoformat()},
            ))

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
                title=task.title,
                title_due_at=task.created_at if task.origin == "console" and task.title is None else None,
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
            .values(instruction="[正文已过期]", project_context=None, title=None, title_due_at=None)
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
        return self._record(model) if model is not None else None

    async def list_private(self, workspace_id: UUID, user_id: UUID, task_ids: set[UUID]) -> list[TaskRecord]:
        if not task_ids:
            return []
        models = await self._session.scalars(select(TaskModel).where(
            TaskModel.workspace_id == workspace_id, TaskModel.created_by == user_id, TaskModel.id.in_(task_ids),
        ))
        return [self._record(model) for model in models]

    async def claim_title(self, now: datetime, lease_until: datetime) -> TaskRecord | None:
        due = TaskModel.title_due_at <= now
        candidate = select(TaskModel.id).where(due).order_by(TaskModel.title_due_at, TaskModel.id).limit(1)
        # 条件更新同时支持 PostgreSQL 和 SQLite，并发 Worker 只能领取一次。
        model = await self._session.scalar(
            update(TaskModel).where(TaskModel.id == candidate.scalar_subquery(), due)
            .values(title_due_at=lease_until).returning(TaskModel)
        )
        return self._record(model) if model is not None else None

    async def finish_title(self, task: TaskRecord, lease_until: datetime, title: str | None) -> bool:
        # 过期、清理或重新领取后，旧的模型响应不能写回标题。
        changed = await self._session.scalar(update(TaskModel).where(
            TaskModel.workspace_id == task.workspace_id,
            TaskModel.created_by == task.created_by,
            TaskModel.id == task.id,
            TaskModel.title_due_at == lease_until,
            TaskModel.title_due_at > datetime.now(UTC),
            TaskModel.title.is_(None),
        ).values(title=title, title_due_at=None).returning(TaskModel.id))
        return changed is not None

    @staticmethod
    def _record(model: TaskModel) -> TaskRecord:
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
            title=model.title,
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
