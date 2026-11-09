"""创建私有 Console 任务、运行、步骤、事件和事务型待调度记录。

Revision ID: 20261017_0004
Revises: 20260918_0003
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261017_0004"
down_revision: str | None = "20260918_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """先创建目标和运行，再创建依赖运行的事实与待调度记录。"""
    op.create_table(
        "collaboration_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.UniqueConstraint("id", "workspace_id", "created_by", name="uq_tasks_private_owner"),
        sa.CheckConstraint("origin = 'console' AND audience = 'private'", name="ck_tasks_private_console"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("agent_name", sa.String(120), nullable=False),
        sa.Column("agent_version", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"]),
        sa.ForeignKeyConstraint(
            ["task_id", "workspace_id", "created_by"],
            ["collaboration_tasks.id", "collaboration_tasks.workspace_id", "collaboration_tasks.created_by"],
        ),
        sa.UniqueConstraint("workspace_id", "created_by", "idempotency_key", name="uq_runs_submission_key"),
        sa.CheckConstraint("last_sequence >= 1", name="ck_runs_sequence_positive"),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','WAITING_CONTEXT','WAITING_APPROVAL',"
            "'RECOVERY_REQUIRED','SUCCEEDED','FAILED','CANCELLED')",
            name="ck_runs_status",
        ),
    )
    op.create_index("ix_runs_private_created", "runs", ["workspace_id", "created_by", "created_at", "id"])
    op.create_index(
        "uq_runs_active_task",
        "runs",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('SUCCEEDED','FAILED','CANCELLED')"),
        sqlite_where=sa.text("status NOT IN ('SUCCEEDED','FAILED','CANCELLED')"),
    )
    op.create_table(
        "run_steps",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "position"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.CheckConstraint("position >= 1", name="ck_run_steps_position"),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')", name="ck_run_steps_status"
        ),
    )
    op.create_table(
        "run_events",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "sequence"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.CheckConstraint("sequence >= 1", name="ck_run_events_sequence"),
    )
    op.create_table(
        "run_dispatch_outbox",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.CheckConstraint("status IN ('PENDING','DISPATCHED','CANCELLED')", name="ck_run_dispatch_status"),
    )
    op.create_index("ix_run_dispatch_status_created", "run_dispatch_outbox", ["status", "created_at"])


def downgrade() -> None:
    """只回退本迁移新增结构；有业务数据时回退会删除这些运行与目标。"""
    op.drop_index("ix_run_dispatch_status_created", table_name="run_dispatch_outbox")
    op.drop_table("run_dispatch_outbox")
    op.drop_table("run_events")
    op.drop_table("run_steps")
    op.drop_index("uq_runs_active_task", table_name="runs")
    op.drop_index("ix_runs_private_created", table_name="runs")
    op.drop_table("runs")
    op.drop_table("collaboration_tasks")
