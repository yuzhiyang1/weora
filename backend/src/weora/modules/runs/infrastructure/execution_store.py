"""独立进程可用的领取、租约和终态存储，不在模型调用期间持有数据库锁。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from weora.modules.runs.application.execution import RunLease
from weora.modules.runs.application.ports import RunActor, RunRecord
from weora.modules.runs.infrastructure.models import RunDispatchModel, RunEventModel, RunModel, RunStepModel
from weora.modules.runs.infrastructure.unit_of_work import _record
from weora.shared.infrastructure.database import SessionFactory


class SqlAlchemyExecutionStore:
    """每次状态变化与事实事件原子提交；PostgreSQL 使用数据库时钟。"""

    def __init__(self, sessions: SessionFactory, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._sessions = sessions
        self._clock = clock

    def _now(self, session: AsyncSession) -> datetime | ColumnElement[datetime]:
        # 生产租约不信任不同 Worker 的本机时钟；SQLite 时钟可注入以测试到期边界。
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            return func.clock_timestamp()
        return self._clock()

    async def claim(self, *, workspace_id: UUID, run_id: UUID, owner: str, lease_seconds: int) -> RunLease | None:
        """QUEUED 到 RUNNING 的 CAS 只允许一个领取者，过期 RUNNING 不自动重跑。"""
        if not owner or len(owner) > 128 or not 3 <= lease_seconds <= 300:
            raise ValueError("Worker 标识或租约配置无效")
        async with self._sessions() as session, session.begin():
            now = self._now(session)
            model = await session.scalar(
                update(RunModel)
                .where(
                    RunModel.id == run_id,
                    RunModel.workspace_id == workspace_id,
                    RunModel.status == "QUEUED",
                    RunModel.cancellation_requested_at.is_(None),
                )
                .values(
                    status="RUNNING",
                    lease_owner=owner,
                    fencing_token=RunModel.fencing_token + 1,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    updated_at=now,
                    last_sequence=RunModel.last_sequence + 1,
                )
                .returning(RunModel)
            )
            if model is None:
                return None
            await session.execute(update(RunStepModel).where(RunStepModel.run_id == run_id).values(status="RUNNING"))
            await session.execute(
                update(RunDispatchModel)
                .where(RunDispatchModel.run_id == run_id)
                .values(
                    status="DISPATCHED",
                    updated_at=now,
                )
            )
            await self._event(session, model.id, model.last_sequence, "run.started")
            return RunLease(_record(model), owner, model.fencing_token)

    def _lease_conditions(self, session: AsyncSession, lease: RunLease) -> tuple[ColumnElement[bool], ...]:
        return (
            RunModel.id == lease.run.id,
            RunModel.workspace_id == lease.run.workspace_id,
            RunModel.status == "RUNNING",
            RunModel.lease_owner == lease.owner,
            RunModel.fencing_token == lease.fencing_token,
            RunModel.lease_expires_at > self._now(session),
        )

    async def renew(self, lease: RunLease, *, lease_seconds: int) -> bool:
        """取消请求阻止续租；过期后不能通过心跳复活。"""
        async with self._sessions() as session, session.begin():
            now = self._now(session)
            result = await session.scalar(
                update(RunModel)
                .where(
                    *self._lease_conditions(session, lease),
                    RunModel.cancellation_requested_at.is_(None),
                )
                .values(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)
                .returning(RunModel.id)
            )
            return result is not None

    async def authorize_token(
        self, actor: RunActor, run_id: UUID, fencing_token: int, owner: str | None = None, *, write: bool = False
    ) -> bool:
        """每个工具边界校验实际租约；HTTP 不接受 token，不靠客户端宣称执行权。"""
        async with self._sessions() as session:
            query = select(RunModel.id).where(
                RunModel.id == run_id,
                RunModel.workspace_id == actor.workspace_id,
                RunModel.created_by == actor.user_id,
                RunModel.status == "RUNNING",
                RunModel.fencing_token == fencing_token,
                RunModel.lease_expires_at > self._now(session),
                RunModel.cancellation_requested_at.is_(None),
            )
            if owner is not None:
                query = query.where(RunModel.lease_owner == owner)
            if write:
                query = query.where(RunModel.recovery_read_only.is_(False))
            return await session.scalar(query) is not None

    async def transition(
        self,
        actor: RunActor,
        run_id: UUID,
        target: str,
        *,
        expected_lease: RunLease | None = None,
    ) -> bool:
        """审批/缺失上下文状态切换使旧租约立即失效，恢复只重新投递非终态。"""
        allowed = {
            "WAITING_APPROVAL": {"QUEUED", "RUNNING", "WAITING_APPROVAL", "WAITING_CONTEXT", "RECOVERY_REQUIRED"},
            "WAITING_CONTEXT": {"QUEUED", "RUNNING", "WAITING_CONTEXT"},
            "QUEUED": {"WAITING_APPROVAL", "WAITING_CONTEXT"},
            "FAILED": {"WAITING_APPROVAL", "WAITING_CONTEXT"},
            "RECOVERY_REQUIRED": {"RUNNING", "WAITING_APPROVAL"},
        }
        if target not in allowed:
            return False
        async with self._sessions() as session, session.begin():
            ownership = self._lease_conditions(session, expected_lease) if expected_lease else ()
            model = await session.scalar(
                update(RunModel)
                .where(
                    RunModel.id == run_id,
                    RunModel.workspace_id == actor.workspace_id,
                    RunModel.created_by == actor.user_id,
                    RunModel.status.in_(allowed[target]),
                    RunModel.status != target,
                    RunModel.cancellation_requested_at.is_(None),
                    *ownership,
                )
                .values(
                    status=target,
                    fencing_token=RunModel.fencing_token + 1,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=self._now(session),
                    last_sequence=RunModel.last_sequence + 1,
                )
                .returning(RunModel)
            )
            if model is None:
                existing = await session.scalar(
                    select(RunModel.status).where(
                        RunModel.id == run_id,
                        RunModel.workspace_id == actor.workspace_id,
                        RunModel.created_by == actor.user_id,
                    )
                )
                return existing == target
            if target == "QUEUED":
                await session.execute(
                    update(RunDispatchModel)
                    .where(RunDispatchModel.run_id == run_id)
                    .values(status="PENDING", updated_at=self._now(session))
                )
                await session.execute(
                    update(RunStepModel).where(RunStepModel.run_id == run_id).values(status="PENDING")
                )
            await self._event(session, run_id, model.last_sequence, f"run.{target.lower()}")
            return True

    async def purge_content(self, workspace_id: UUID, run_ids: set[UUID]) -> int:
        """正文过期不删除审计事件；先停活跃尝试，避免 Worker 再写回过期正文。"""
        if not run_ids:
            return 0
        async with self._sessions() as session, session.begin():
            rows = await session.scalars(
                update(RunModel)
                .where(
                    RunModel.workspace_id == workspace_id,
                    RunModel.id.in_(run_ids),
                    (RunModel.failure_code.is_(None) | (RunModel.failure_code != "CONTEXT_EXPIRED")),
                )
                .values(
                    output_text=None,
                    fencing_token=RunModel.fencing_token + 1,
                    lease_owner=None,
                    lease_expires_at=None,
                    status=case(
                        (RunModel.status.in_(["SUCCEEDED", "FAILED", "CANCELLED"]), RunModel.status), else_="CANCELLED"
                    ),
                    failure_code="CONTEXT_EXPIRED",
                )
                .returning(RunModel.id)
            )
            return len(rows.all())

    async def task_ids(self, workspace_id: UUID, run_ids: set[UUID]) -> set[UUID]:
        async with self._sessions() as session:
            return set(
                await session.scalars(
                    select(RunModel.task_id).where(RunModel.workspace_id == workspace_id, RunModel.id.in_(run_ids))
                )
            )

    async def recover_read_only(self, actor: RunActor, run_id: UUID) -> bool:
        """人工恢复只能进入只读尝试，未知写结果不能因重启再次执行。"""
        async with self._sessions() as session, session.begin():
            model = await session.scalar(
                update(RunModel)
                .where(
                    RunModel.id == run_id,
                    RunModel.workspace_id == actor.workspace_id,
                    RunModel.created_by == actor.user_id,
                    RunModel.status.in_(["RECOVERY_REQUIRED", "WAITING_CONTEXT"]),
                    RunModel.cancellation_requested_at.is_(None),
                )
                .values(
                    status="QUEUED",
                    recovery_read_only=True,
                    lease_owner=None,
                    lease_expires_at=None,
                    fencing_token=RunModel.fencing_token + 1,
                    failure_code=None,
                    last_sequence=RunModel.last_sequence + 1,
                    updated_at=self._now(session),
                )
                .returning(RunModel)
            )
            if model is None:
                current = await session.scalar(
                    select(RunModel.id).where(
                        RunModel.id == run_id,
                        RunModel.workspace_id == actor.workspace_id,
                        RunModel.created_by == actor.user_id,
                        RunModel.status == "QUEUED",
                        RunModel.recovery_read_only.is_(True),
                    )
                )
                return current is not None
            await session.execute(
                update(RunDispatchModel)
                .where(RunDispatchModel.run_id == run_id)
                .values(status="PENDING", updated_at=self._now(session))
            )
            await session.execute(update(RunStepModel).where(RunStepModel.run_id == run_id).values(status="PENDING"))
            await self._event(session, run_id, model.last_sequence, "run.recovered_read_only")
            return True

    async def finish(
        self,
        lease: RunLease,
        *,
        status: Literal["SUCCEEDED", "FAILED", "CANCELLED"],
        output: str | None = None,
        failure_code: str | None = None,
    ) -> bool:
        """取消与成功竞争时取消优先，输出、步骤、事件和终态必须一起写入。"""
        if status == "SUCCEEDED" and (not output or len(output) > 65536):
            raise ValueError("成功运行必须有受限大小的真实输出")
        async with self._sessions() as session, session.begin():
            cancelled = RunModel.cancellation_requested_at.is_not(None)
            conditions = self._lease_conditions(session, lease)
            if status == "CANCELLED":
                conditions += (cancelled,)
            model = await session.scalar(
                update(RunModel)
                .where(*conditions)
                .values(
                    status=case((cancelled, "CANCELLED"), else_=status),
                    output_text=case((cancelled, None), else_=output if status == "SUCCEEDED" else None),
                    failure_code=case((cancelled, "REMOTE_COMPLETION_UNCONFIRMED"), else_=failure_code),
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=self._now(session),
                    last_sequence=RunModel.last_sequence + 1,
                )
                .returning(RunModel)
            )
            if model is None:
                return False
            await session.execute(
                update(RunStepModel).where(RunStepModel.run_id == model.id).values(status=model.status)
            )
            await self._event(session, model.id, model.last_sequence, f"run.{model.status.lower()}")
            return True

    async def recover_expired(self) -> int:
        """运行已开始但租约过期时转人工恢复，不自动重复请求模型。"""
        async with self._sessions() as session, session.begin():
            rows = (
                await session.execute(
                    update(RunModel)
                    .where(
                        RunModel.status == "RUNNING",
                        RunModel.lease_expires_at <= self._now(session),
                    )
                    .values(
                        status="RECOVERY_REQUIRED",
                        failure_code="LEASE_EXPIRED",
                        lease_owner=None,
                        lease_expires_at=None,
                        fencing_token=RunModel.fencing_token + 1,
                        last_sequence=RunModel.last_sequence + 1,
                        updated_at=self._now(session),
                    )
                    .returning(RunModel.id, RunModel.last_sequence)
                )
            ).all()
            for run_id, sequence in rows:
                await session.execute(update(RunStepModel).where(RunStepModel.run_id == run_id).values(status="FAILED"))
                await self._event(session, run_id, sequence, "run.recovery_required")
            return len(rows)

    async def maintenance_runs(self) -> list[RunRecord]:
        """后台只枚举需跨模块对账的非终态；不把这份跨成员视图暴露给 HTTP。"""
        async with self._sessions() as session:
            rows = await session.scalars(
                select(RunModel).where(RunModel.status.in_(["WAITING_APPROVAL", "RECOVERY_REQUIRED"])).limit(100)
            )
            return [_record(row) for row in rows]

    async def is_live(self, run_id: UUID) -> bool:
        """清理器失败关闭：不属于本数据库的容器不自动删除。"""
        async with self._sessions() as session:
            record = await session.get(RunModel, run_id)
            if record is None:
                return True
            active = await session.scalar(
                select(RunModel.id).where(
                    RunModel.id == run_id, RunModel.status == "RUNNING", RunModel.lease_expires_at > self._now(session)
                )
            )
            return active is not None

    async def actor_for_maintenance(self, run_id: UUID) -> RunActor | None:
        async with self._sessions() as session:
            record = await session.get(RunModel, run_id)
            return RunActor(record.created_by, record.workspace_id) if record else None

    async def pending_dispatches(self, *, limit: int = 50) -> list[tuple[UUID, UUID]]:
        """数据库是待投递真相；长时间未领取的已投递任务也可重复发送。"""
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(RunModel.workspace_id, RunModel.id)
                    .join(
                        RunDispatchModel,
                        RunDispatchModel.run_id == RunModel.id,
                    )
                    .where(
                        RunModel.status == "QUEUED",
                        RunDispatchModel.status != "CANCELLED",
                        (RunDispatchModel.status == "PENDING")
                        | (RunDispatchModel.updated_at <= self._now(session) - timedelta(seconds=30)),
                    )
                    .order_by(RunModel.created_at, RunModel.id)
                    .limit(limit)
                )
            ).all()
            return [(workspace_id, run_id) for workspace_id, run_id in rows]

    async def mark_dispatched(self, *, workspace_id: UUID, run_id: UUID) -> None:
        """只在队列确认接收后调用；未知发送结果保持原样，允许重复投递。"""
        async with self._sessions() as session, session.begin():
            queued = select(RunModel.id).where(RunModel.workspace_id == workspace_id, RunModel.status == "QUEUED")
            await session.execute(
                update(RunDispatchModel)
                .where(
                    RunDispatchModel.run_id == run_id,
                    RunDispatchModel.run_id.in_(queued),
                    RunDispatchModel.status != "CANCELLED",
                )
                .values(status="DISPATCHED", updated_at=self._now(session))
            )

    async def _event(self, session: AsyncSession, run_id: UUID, sequence: int, event_type: str) -> None:
        request_id = await session.scalar(
            select(RunEventModel.request_id).where(
                RunEventModel.run_id == run_id,
                RunEventModel.sequence == 1,
            )
        )
        session.add(
            RunEventModel(
                run_id=run_id,
                sequence=sequence,
                event_type=event_type,
                request_id=request_id or "worker",
                occurred_at=self._now(session),
            )
        )
