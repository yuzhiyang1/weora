"""审批和交付以数据库为事实来源，进程重启不丢失批准或未知远端结果。"""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from weora.modules.delivery.domain.models import ArtifactRecord, PlanRecord, PlanScope, PlanStatus, RemoteRecord
from weora.shared.infrastructure.database import Base, SessionFactory


class DeliveryPlanModel(Base):
    __tablename__ = "delivery_plans"
    __table_args__ = (UniqueConstraint("run_id", "version", name="uq_delivery_plan_version"),)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer())
    scope: Mapped[dict[str, object]] = mapped_column(JSON())
    scope_sha256: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text())
    risk: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text(), nullable=True)


class DeliveryArtifactModel(Base):
    __tablename__ = "delivery_artifacts"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_delivery_artifact_sequence"),)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    body: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    command: Mapped[list[str]] = mapped_column(JSON())
    exit_code: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer())
    plan_id: Mapped[UUID | None] = mapped_column(ForeignKey("delivery_plans.id"), nullable=True)
    repository_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DeliveryRemoteModel(Base):
    __tablename__ = "delivery_remote_actions"
    __table_args__ = (UniqueConstraint("workspace_id", "run_id", "business_key", name="uq_delivery_remote_key"),)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    business_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    remote_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_id: Mapped[UUID | None] = mapped_column(ForeignKey("delivery_plans.id"), nullable=True)


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def _plan(model: DeliveryPlanModel) -> PlanRecord:
    scope = model.scope
    return PlanRecord(
        model.id,
        model.run_id,
        model.workspace_id,
        model.created_by,
        model.version,
        PlanScope(
            str(scope["repository_id"]),
            str(scope["base_revision"]),
            tuple(cast(list[str], scope["paths"])),
            tuple(tuple(item) for item in cast(list[list[str]], scope["commands"])),
            tuple(cast(list[str], scope["actions"])),
            cast(str | None, scope["remote_id"]),
            cast(str | None, scope["target_branch"]),
            cast(str | None, scope.get("remote_binding")),
            cast(str | None, scope.get("source_delivery_id")),
            cast(str | None, scope.get("source_branch")),
            cast(str | None, scope.get("source_revision")),
        ),
        model.summary,
        model.risk,
        cast(PlanStatus, model.status),
        cast(datetime, _utc(model.created_at)),
        model.decided_by,
        _utc(model.decided_at),
        _utc(model.expires_at),
        model.reason,
    )


def _remote(model: DeliveryRemoteModel) -> RemoteRecord:
    return RemoteRecord(
        model.id,
        model.run_id,
        model.workspace_id,
        model.business_key,
        model.status,
        model.attempts,
        cast(datetime, _utc(model.created_at)),
        cast(datetime, _utc(model.updated_at)),
        model.remote_id,
        model.url,
        model.error_code,
        model.plan_id,
    )


class SqlAlchemyDeliveryStore:
    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    async def remote_by_id(self, workspace_id: UUID, record_id: UUID) -> RemoteRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(DeliveryRemoteModel).where(
                    DeliveryRemoteModel.workspace_id == workspace_id, DeliveryRemoteModel.id == record_id
                )
            )
            return _remote(row) if row else None

    async def latest_plan(self, workspace_id: UUID, run_id: UUID) -> PlanRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(DeliveryPlanModel)
                .where(DeliveryPlanModel.workspace_id == workspace_id, DeliveryPlanModel.run_id == run_id)
                .order_by(DeliveryPlanModel.version.desc())
                .limit(1)
            )
            return _plan(row) if row else None

    async def get_plan(self, workspace_id: UUID, plan_id: UUID) -> PlanRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(DeliveryPlanModel).where(
                    DeliveryPlanModel.workspace_id == workspace_id, DeliveryPlanModel.id == plan_id
                )
            )
            return _plan(row) if row else None

    async def save_plan(self, plan: PlanRecord, *, expected_version: int, expected_status: str | None) -> bool:
        values = asdict(plan)
        values["scope"] = asdict(plan.scope)
        values["scope_sha256"] = plan.scope.digest
        try:
            async with self.sessions() as session, session.begin():
                if plan.version > expected_version:
                    if expected_version:
                        changed = await session.execute(
                            update(DeliveryPlanModel)
                            .where(
                                DeliveryPlanModel.workspace_id == plan.workspace_id,
                                DeliveryPlanModel.run_id == plan.run_id,
                                DeliveryPlanModel.version == expected_version,
                                DeliveryPlanModel.status == expected_status,
                            )
                            .values(status="SUPERSEDED")
                        )
                        if changed.rowcount != 1:  # type: ignore[attr-defined]
                            return False
                    session.add(DeliveryPlanModel(**values))
                    await session.flush()
                    return True
                result = await session.execute(
                    update(DeliveryPlanModel)
                    .where(
                        DeliveryPlanModel.id == plan.id,
                        DeliveryPlanModel.workspace_id == plan.workspace_id,
                        DeliveryPlanModel.version == expected_version,
                        DeliveryPlanModel.status == expected_status,
                    )
                    .values(**values)
                )
                return result.rowcount == 1  # type: ignore[attr-defined,no-any-return]
        except IntegrityError:
            # 唯一版本约束负责并发插入；异常不包含在客户端响应中。
            return False

    async def add_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        # 同 Run 的事实顺序不能依赖墙上时钟；Windows 同一 tick 可能产生多个工具结果。
        for _ in range(10):
            try:
                async with self.sessions() as session, session.begin():
                    previous = await session.scalar(
                        select(func.max(DeliveryArtifactModel.sequence)).where(
                            DeliveryArtifactModel.run_id == artifact.run_id
                        )
                    )
                    ordered = replace(artifact, sequence=(previous or 0) + 1)
                    values = asdict(ordered)
                    values["command"] = list(artifact.command)
                    session.add(DeliveryArtifactModel(**values))
                    await session.flush()
                return ordered
            except IntegrityError:
                continue
        raise RuntimeError("ARTIFACT_SEQUENCE_CONFLICT")

    async def artifacts(self, workspace_id: UUID, run_id: UUID) -> list[ArtifactRecord]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(DeliveryArtifactModel)
                    .where(DeliveryArtifactModel.workspace_id == workspace_id, DeliveryArtifactModel.run_id == run_id)
                    .order_by(DeliveryArtifactModel.sequence)
                )
            ).all()
            return [
                ArtifactRecord(
                    row.id,
                    row.run_id,
                    row.workspace_id,
                    row.kind,
                    row.status,
                    row.body,
                    cast(datetime, _utc(row.created_at)),
                    cast(datetime, _utc(row.expires_at)),
                    row.sha256,
                    tuple(row.command),
                    row.exit_code,
                    row.sequence,
                    row.plan_id,
                    row.repository_sha256,
                )
                for row in rows
            ]

    async def purge_expired(self, now: datetime) -> int:
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                update(DeliveryArtifactModel)
                .where(DeliveryArtifactModel.expires_at <= now, DeliveryArtifactModel.body.is_not(None))
                .values(body=None, command=[], status="EXPIRED")
            )
            count = cast(int, result.rowcount)  # type: ignore[attr-defined]
            # 即使没有外部来源，计划中的自然语言也不能永久保留。
            plans = (
                await session.scalars(
                    select(DeliveryPlanModel).where(DeliveryPlanModel.created_at <= now - timedelta(days=7))
                )
            ).all()
            for plan in plans:
                self._redact_plan(plan)
            return count + len(plans)

    @staticmethod
    def _redact_plan(plan: DeliveryPlanModel) -> None:
        plan.summary = "[上下文已过期]"
        plan.risk = "[上下文已过期]"
        plan.reason = None
        plan.scope = {**plan.scope, "commands": [], "actions": []}
        if plan.status in {"PENDING", "REVISION_REQUIRED", "APPROVED"}:
            plan.status = "REVOKED"
        # scope_sha256 保留原始摘要供审计；清理不能产生新的执行授权。

    async def purge_runs(self, workspace_id: UUID, run_ids: tuple[UUID, ...]) -> int:
        if not run_ids:
            return 0
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                update(DeliveryArtifactModel)
                .where(
                    DeliveryArtifactModel.workspace_id == workspace_id,
                    DeliveryArtifactModel.run_id.in_(run_ids),
                )
                .values(body=None, command=[], status="EXPIRED")
            )
            plans = (
                await session.scalars(
                    select(DeliveryPlanModel).where(
                        DeliveryPlanModel.workspace_id == workspace_id, DeliveryPlanModel.run_id.in_(run_ids)
                    )
                )
            ).all()
            for plan in plans:
                self._redact_plan(plan)
            return cast(int, result.rowcount) + len(plans)  # type: ignore[attr-defined]

    async def get_remote(self, workspace_id: UUID, run_id: UUID, key: str) -> RemoteRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(DeliveryRemoteModel).where(
                    DeliveryRemoteModel.workspace_id == workspace_id,
                    DeliveryRemoteModel.run_id == run_id,
                    DeliveryRemoteModel.business_key == key,
                )
            )
            return _remote(row) if row else None

    async def remotes(self, workspace_id: UUID, run_id: UUID) -> list[RemoteRecord]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(DeliveryRemoteModel)
                    .where(DeliveryRemoteModel.workspace_id == workspace_id, DeliveryRemoteModel.run_id == run_id)
                    .order_by(DeliveryRemoteModel.created_at)
                )
            ).all()
            return [_remote(row) for row in rows]

    async def claim_remote(self, record: RemoteRecord) -> bool:
        try:
            async with self.sessions() as session, session.begin():
                session.add(DeliveryRemoteModel(**asdict(record)))
                await session.flush()
            return True
        except IntegrityError:
            return False

    async def finish_remote(self, record: RemoteRecord, *, expected_status: str) -> bool:
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                update(DeliveryRemoteModel)
                .where(
                    DeliveryRemoteModel.id == record.id,
                    DeliveryRemoteModel.workspace_id == record.workspace_id,
                    DeliveryRemoteModel.status == expected_status,
                )
                .values(**asdict(record))
            )
            return result.rowcount == 1  # type: ignore[attr-defined,no-any-return]
