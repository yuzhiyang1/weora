"""运行事务与存储实现，跨模块写入始终调用 TaskService。"""

from dataclasses import asdict
from datetime import UTC, datetime
from types import TracebackType
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.collaboration.application.tasks import TaskService
from weora.modules.collaboration.infrastructure.tasks import SqlAlchemyTaskStore
from weora.modules.runs.application.ports import (
    ExecutionProfile,
    RunActor,
    RunEventRecord,
    RunRecord,
    RunStore,
    RunSubmission,
)
from weora.modules.runs.domain.errors import RunSubmissionRaceError
from weora.modules.runs.infrastructure.models import RunDispatchModel, RunEventModel, RunModel, RunStepModel
from weora.shared.infrastructure.database import SessionFactory


def _utc(value: datetime) -> datetime:
    """SQLite 测试返回朴素时间，恢复为与 PostgreSQL 相同的 UTC 契约。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _record(model: RunModel) -> RunRecord:
    profile = model.execution_profile
    return RunRecord(
        id=model.id,
        task_id=model.task_id,
        workspace_id=model.workspace_id,
        created_by=model.created_by,
        agent_version_id=model.agent_version_id,
        agent_name=model.agent_name,
        agent_version=model.agent_version,
        status=model.status,
        last_sequence=model.last_sequence,
        created_at=_utc(model.created_at),
        updated_at=_utc(model.updated_at),
        execution_profile=None
        if profile is None
        else ExecutionProfile(
            runtime_id=str(profile["runtime_id"]),
            provider=str(profile["provider"]),
            model_id=str(profile["model_id"]),
            max_tokens=int(profile["max_tokens"]) if profile.get("max_tokens") is not None else None,
            timeout_seconds=int(profile["timeout_seconds"]),
            configuration_id=str(profile["configuration_id"]) if profile.get("configuration_id") else None,
            execution_timeout_seconds=(
                int(profile["execution_timeout_seconds"])
                if profile.get("execution_timeout_seconds") is not None
                else None
            ),
        ),
        cancellation_requested_at=(
            _utc(model.cancellation_requested_at) if model.cancellation_requested_at is not None else None
        ),
        output_text=model.output_text,
        failure_code=model.failure_code,
        recovery_read_only=model.recovery_read_only,
        collaboration_context=model.collaboration_context,
        fencing_token=model.fencing_token,
        lease_expires_at=_utc(model.lease_expires_at) if model.lease_expires_at is not None else None,
    )


class SqlAlchemyRunStore:
    """所有用户查询都以工作空间和发起人为必要条件。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_submission(self, *, actor: RunActor, key: str) -> RunSubmission | None:
        model = await self._session.scalar(
            select(RunModel).where(
                RunModel.workspace_id == actor.workspace_id,
                RunModel.created_by == actor.user_id,
                RunModel.idempotency_key == key,
            )
        )
        return None if model is None else RunSubmission(_record(model), key, model.request_sha256)

    async def add(self, submission: RunSubmission, *, request_id: str) -> None:
        """运行先 flush，子记录随后写入；任意一步失败均由 UoW 回滚。"""
        run = submission.run
        self._session.add(
            RunModel(
                id=run.id,
                task_id=run.task_id,
                workspace_id=run.workspace_id,
                created_by=run.created_by,
                agent_version_id=run.agent_version_id,
                agent_name=run.agent_name,
                agent_version=run.agent_version,
                idempotency_key=submission.idempotency_key,
                request_sha256=submission.request_sha256,
                status=run.status,
                last_sequence=run.last_sequence,
                created_at=run.created_at,
                updated_at=run.updated_at,
                execution_profile=asdict(run.execution_profile) if run.execution_profile is not None else None,
                collaboration_context=run.collaboration_context,
            )
        )
        await self._session.flush()
        self._session.add_all(
            [
                RunStepModel(run_id=run.id, position=1, kind="AGENT_RESPONSE", status="PENDING"),
                RunEventModel(
                    run_id=run.id,
                    sequence=1,
                    event_type="run.queued",
                    request_id=request_id,
                    occurred_at=run.created_at,
                ),
                RunDispatchModel(run_id=run.id, status="PENDING", created_at=run.created_at, updated_at=run.created_at),
            ]
        )

    async def get_private(self, *, actor: RunActor, run_id: UUID, lock: bool = False) -> RunRecord | None:
        query = (
            select(RunModel)
            .where(
                RunModel.id == run_id,
                RunModel.workspace_id == actor.workspace_id,
                RunModel.created_by == actor.user_id,
            )
            .execution_options(populate_existing=True)
        )
        model = await self._session.scalar(query.with_for_update() if lock else query)
        return None if model is None else _record(model)

    async def task_runs(self, *, actor: RunActor, task_id: UUID, limit: int = 8) -> list[RunRecord]:
        rows = await self._session.scalars(select(RunModel).where(
            RunModel.workspace_id == actor.workspace_id, RunModel.created_by == actor.user_id,
            RunModel.task_id == task_id,
        ).order_by(RunModel.created_at.desc(), RunModel.id.desc()).limit(limit))
        return [_record(row) for row in rows]

    async def active_task_run(self, *, actor: RunActor, task_id: UUID) -> RunRecord | None:
        return await self.active_run_for_task(actor.workspace_id, task_id)

    async def active_run_for_task(self, workspace_id: UUID, task_id: UUID) -> RunRecord | None:
        """供已完成任务鉴权的用例查询；活跃约束跨执行主体。"""
        row = await self._session.scalar(select(RunModel).where(
            RunModel.workspace_id == workspace_id,
            RunModel.task_id == task_id, RunModel.status.not_in(["SUCCEEDED", "FAILED", "CANCELLED"]),
        ))
        return _record(row) if row else None

    async def list_private(self, *, actor: RunActor, limit: int, before: RunRecord | None) -> list[RunRecord]:
        query = select(RunModel).where(
            RunModel.workspace_id == actor.workspace_id,
            RunModel.created_by == actor.user_id,
        )
        if before is not None:
            query = query.where(
                or_(
                    RunModel.created_at < before.created_at,
                    and_(RunModel.created_at == before.created_at, RunModel.id < before.id),
                )
            )
        models = await self._session.scalars(
            query.order_by(RunModel.created_at.desc(), RunModel.id.desc()).limit(limit)
        )
        return [_record(model) for model in models]

    async def list_events(
        self,
        *,
        actor: RunActor,
        run_id: UUID,
        after: int,
        limit: int,
    ) -> list[RunEventRecord]:
        models = await self._session.scalars(
            select(RunEventModel)
            .join(
                RunModel,
                RunModel.id == RunEventModel.run_id,
            )
            .where(
                RunModel.workspace_id == actor.workspace_id,
                RunModel.created_by == actor.user_id,
                RunEventModel.run_id == run_id,
                RunEventModel.sequence > after,
            )
            .order_by(RunEventModel.sequence)
            .limit(limit)
        )
        return [
            RunEventRecord(
                run_id=model.run_id,
                sequence=model.sequence,
                event_type=model.event_type,
                request_id=model.request_id,
                occurred_at=_utc(model.occurred_at),
            )
            for model in models
        ]

    async def cancel_queued(self, *, actor: RunActor, run_id: UUID, request_id: str) -> bool:
        """状态比较更新是取消的最终防线；只有赢得更新者追加终态事件。"""
        now = datetime.now(UTC)
        sequence = await self._session.scalar(
            update(RunModel)
            .where(
                RunModel.id == run_id,
                RunModel.workspace_id == actor.workspace_id,
                RunModel.created_by == actor.user_id,
                RunModel.status.in_(["QUEUED", "WAITING_CONTEXT", "WAITING_APPROVAL", "RECOVERY_REQUIRED"]),
            )
            .values(
                status="CANCELLED",
                last_sequence=RunModel.last_sequence + 1,
                updated_at=now,
            )
            .returning(RunModel.last_sequence)
        )
        if sequence is None:
            return False
        await self._session.execute(
            update(RunStepModel)
            .where(
                RunStepModel.run_id == run_id,
                RunStepModel.status == "PENDING",
            )
            .values(status="CANCELLED")
        )
        await self._session.execute(
            update(RunDispatchModel)
            .where(
                RunDispatchModel.run_id == run_id,
                RunDispatchModel.status == "PENDING",
            )
            .values(status="CANCELLED", updated_at=now)
        )
        self._session.add(
            RunEventModel(
                run_id=run_id,
                sequence=sequence,
                event_type="run.cancelled",
                request_id=request_id,
                occurred_at=now,
            )
        )
        return True

    async def request_running_cancellation(self, *, actor: RunActor, run_id: UUID, request_id: str) -> bool:
        """运行中仅记录取消请求；本地 Runtime 退出前不显示已停止。"""
        now = datetime.now(UTC)
        sequence = await self._session.scalar(
            update(RunModel)
            .where(
                RunModel.id == run_id,
                RunModel.workspace_id == actor.workspace_id,
                RunModel.created_by == actor.user_id,
                RunModel.status == "RUNNING",
                RunModel.cancellation_requested_at.is_(None),
                RunModel.lease_owner.is_not(None),
                RunModel.lease_expires_at.is_not(None),
            )
            .values(
                cancellation_requested_at=now,
                last_sequence=RunModel.last_sequence + 1,
                updated_at=now,
            )
            .returning(RunModel.last_sequence)
        )
        if sequence is None:
            return False
        self._session.add(
            RunEventModel(
                run_id=run_id,
                sequence=sequence,
                event_type="run.cancel_requested",
                request_id=request_id,
                occurred_at=now,
            )
        )
        return True


class SqlAlchemyRunsUnitOfWork:
    """事务装配点将协作存储与运行存储绑定同一 Session，不暴露给领域层。"""

    store: RunStore
    tasks: TaskService

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> "SqlAlchemyRunsUnitOfWork":
        self._session = self._session_factory()
        self.store = SqlAlchemyRunStore(self._session)
        self.tasks = TaskService(SqlAlchemyTaskStore(self._session))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        try:
            if exc_value is not None:
                await self._session.rollback()
        finally:
            await self._session.close()
        if isinstance(exc_value, IntegrityError):
            # 仅映射提交键冲突，不能把外键、状态或其他唯一约束错误伪装成重复提交。
            details = str(exc_value.orig)
            if (
                "uq_runs_submission_key" in details
                or "runs.workspace_id, runs.created_by, runs.idempotency_key" in details
                or "uq_task_input_key" in details
                or "task_inputs.workspace_id, task_inputs.created_by, task_inputs.idempotency_key" in details
                or "task_threads_pkey" in details
                or "task_threads.workspace_id, task_threads.created_by, task_threads.thread_key" in details
            ):
                raise RunSubmissionRaceError from exc_value

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("Unit of Work 尚未进入事务上下文")
        await self._session.commit()
