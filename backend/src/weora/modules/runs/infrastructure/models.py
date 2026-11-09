"""运行、步骤、事件及待调度记录；不包含 Pi 消息或 DeepSeek 专用类型。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from weora.modules.runs.application.ports import RunStatus
from weora.shared.infrastructure.database import Base


class RunModel(Base):
    """运行固定发布版本，提交键在成员与工作空间内唯一。"""

    __tablename__ = "runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "workspace_id", "created_by"],
            ["collaboration_tasks.id", "collaboration_tasks.workspace_id", "collaboration_tasks.created_by"],
        ),
        UniqueConstraint("workspace_id", "created_by", "idempotency_key", name="uq_runs_submission_key"),
        CheckConstraint("last_sequence >= 1", name="ck_runs_sequence_positive"),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','WAITING_CONTEXT','WAITING_APPROVAL',"
            "'RECOVERY_REQUIRED','SUCCEEDED','FAILED','CANCELLED')",
            name="ck_runs_status",
        ),
        Index("ix_runs_private_created", "workspace_id", "created_by", "created_at", "id"),
        # 一个目标只能有一次活跃尝试；未来显式重试仍可保留多个终态 Run。
        Index(
            "uq_runs_active_task",
            "task_id",
            unique=True,
            postgresql_where=text("status NOT IN ('SUCCEEDED','FAILED','CANCELLED')"),
            sqlite_where=text("status NOT IN ('SUCCEEDED','FAILED','CANCELLED')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    task_id: Mapped[UUID] = mapped_column()
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    agent_version_id: Mapped[UUID] = mapped_column(ForeignKey("agent_versions.id"))
    agent_name: Mapped[str] = mapped_column(String(120))
    agent_version: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[RunStatus] = mapped_column(String(32))
    last_sequence: Mapped[int] = mapped_column(Integer())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    execution_profile: Mapped[dict[str, str | int] | None] = mapped_column(JSON(), nullable=True)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    output_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer(), default=0, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_read_only: Mapped[bool] = mapped_column(Boolean(), default=False, server_default="false")


class RunStepModel(Base):
    """预先登记响应步骤；PENDING 不代表已经调用了模型。"""

    __tablename__ = "run_steps"
    __table_args__ = (
        CheckConstraint("position >= 1", name="ck_run_steps_position"),
        CheckConstraint("status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')", name="ck_run_steps_status"),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))


class RunEventModel(Base):
    """每 Run 单调序号的事实事件，不复制目标正文。"""

    __tablename__ = "run_events"
    __table_args__ = (CheckConstraint("sequence >= 1", name="ck_run_events_sequence"),)

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer(), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunDispatchModel(Base):
    """事务型待调度记录；后续发布器成功投递后才能更新投递状态。"""

    __tablename__ = "run_dispatch_outbox"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','DISPATCHED','CANCELLED')", name="ck_run_dispatch_status"),
        Index("ix_run_dispatch_status_created", "status", "created_at"),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
