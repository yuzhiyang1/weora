"""数据库预算和调用幂等；运行重启不会清零工具调用量。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from weora.modules.tools.application.gateway import ToolInvocation, ToolScope
from weora.shared.infrastructure.database import Base, SessionFactory


class ToolBudgetModel(Base):
    __tablename__ = "tool_budgets"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    used: Mapped[int] = mapped_column(Integer(), default=0)


class ToolInvocationModel(Base):
    __tablename__ = "tool_invocations"

    # 复合主键本身保证调用身份唯一；重复声明 UNIQUE 会造成 PostgreSQL 元数据差异。
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(32))
    # 旧记录可空；新调用绑定具体执行尝试，防止恢复后的租约掩盖旧调用中断。
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fencing_token: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    name: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    arguments: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SqlAlchemyToolStore:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def reserve(
        self,
        scope: ToolScope,
        call_id: str,
        name: str,
        fingerprint: str,
        arguments: dict[str, object],
        *,
        max_calls: int,
    ) -> tuple[str, dict[str, object] | None]:
        # 首次初始化可竞争；失败事务回滚后重新查同一预算行，不放宽次数限制。
        for _ in range(3):
            try:
                async with self._sessions() as session, session.begin():
                    previous = await session.get(ToolInvocationModel, (scope.run_id, call_id))
                    if previous is not None:
                        if previous.fingerprint != fingerprint or previous.role != scope.role:
                            return "TOOL_CALL_CONFLICT", None
                        if previous.status in {"RUNNING", "UNKNOWN"}:
                            return "TOOL_RECOVERY_REQUIRED", None
                        return "CACHED", previous.result
                    budget = await session.get(ToolBudgetModel, scope.run_id)
                    if budget is None:
                        session.add(ToolBudgetModel(run_id=scope.run_id, used=0))
                        await session.flush()
                    reserved = await session.scalar(
                        update(ToolBudgetModel)
                        .where(ToolBudgetModel.run_id == scope.run_id, ToolBudgetModel.used < max_calls)
                        .values(used=ToolBudgetModel.used + 1)
                        .returning(ToolBudgetModel.run_id)
                    )
                    if reserved is None:
                        return "TOOL_BUDGET_EXCEEDED", None
                    now = datetime.now(UTC)
                    session.add(
                        ToolInvocationModel(
                            run_id=scope.run_id,
                            call_id=call_id,
                            workspace_id=scope.actor.workspace_id,
                            user_id=scope.actor.user_id,
                            role=scope.role,
                            lease_owner=scope.lease_owner,
                            fencing_token=scope.fencing_token,
                            name=name,
                            fingerprint=fingerprint,
                            arguments=arguments,
                            result=None,
                            status="RUNNING",
                            error_code=None,
                            created_at=now,
                            expires_at=now + timedelta(days=7),
                        )
                    )
                return "NEW", None
            except IntegrityError:
                continue
        return "TOOL_CALL_CONFLICT", None

    async def finish(
        self, scope: ToolScope, call_id: str, *, result: dict[str, object], status: str, error_code: str | None
    ) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(ToolInvocationModel)
                .where(
                    ToolInvocationModel.run_id == scope.run_id,
                    ToolInvocationModel.call_id == call_id,
                    ToolInvocationModel.workspace_id == scope.actor.workspace_id,
                    ToolInvocationModel.user_id == scope.actor.user_id,
                    ToolInvocationModel.status == "RUNNING",
                )
                .values(result=result, status=status, error_code=error_code)
            )

    async def list_calls(self, scope: ToolScope) -> list[ToolInvocation]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ToolInvocationModel)
                .where(
                    ToolInvocationModel.run_id == scope.run_id,
                    ToolInvocationModel.workspace_id == scope.actor.workspace_id,
                    ToolInvocationModel.user_id == scope.actor.user_id,
                )
                .order_by(ToolInvocationModel.created_at, ToolInvocationModel.call_id)
            )
            return [
                ToolInvocation(
                    row.call_id, row.name, row.status, row.arguments or {}, row.result, row.error_code, row.created_at
                )
                for row in rows
            ]

    async def pending_attempts(self) -> list[ToolScope]:
        """仅给后台补偿读取执行标识；不返回工具参数或正文。"""
        from weora.modules.runs.application.ports import RunActor

        async with self._sessions() as session:
            rows = await session.scalars(select(ToolInvocationModel).where(ToolInvocationModel.status == "RUNNING"))
            return [
                ToolScope(
                    row.run_id,
                    RunActor(row.user_id, row.workspace_id),
                    row.lease_owner or "",
                    row.fencing_token or 0,
                    row.role,
                )
                for row in rows
            ]

    async def interrupt_attempt(self, scope: ToolScope) -> int:
        """租约已失效的在途调用改为 UNKNOWN，保留成功记录和幂等键，绝不重试副作用。"""
        from weora.modules.tools.application.gateway import failure

        async with self._sessions() as session, session.begin():
            rows = await session.scalars(
                update(ToolInvocationModel)
                .where(
                    ToolInvocationModel.run_id == scope.run_id,
                    ToolInvocationModel.workspace_id == scope.actor.workspace_id,
                    ToolInvocationModel.status == "RUNNING",
                    ToolInvocationModel.lease_owner == (scope.lease_owner or None),
                    ToolInvocationModel.fencing_token == (scope.fencing_token or None),
                )
                .values(status="UNKNOWN", error_code="TOOL_INTERRUPTED", result=failure("TOOL_INTERRUPTED"))
                .returning(ToolInvocationModel.call_id)
            )
            return len(rows.all())

    async def purge(self) -> int:
        """只清理正文；调用 ID、摘要、状态保留，禁止过期后重放副作用。"""
        async with self._sessions() as session, session.begin():
            ids = await session.scalars(
                update(ToolInvocationModel)
                .where(
                    ToolInvocationModel.expires_at <= datetime.now(UTC),
                    ToolInvocationModel.arguments.is_not(None),
                )
                .values(arguments=None, result=None)
                .returning(ToolInvocationModel.call_id)
            )
            return len(ids.all())

    async def purge_runs(self, workspace_id: UUID, run_ids: set[UUID]) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(ToolInvocationModel)
                .where(ToolInvocationModel.workspace_id == workspace_id, ToolInvocationModel.run_id.in_(run_ids))
                .values(arguments=None, result=None)
            )
