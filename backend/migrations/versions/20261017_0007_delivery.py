"""计划版本、审批证据、产物与远端副作用状态。

Revision ID: 20261017_0007
Revises: 20261017_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261017_0007"
down_revision: str | None = "20261017_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivery_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("scope_sha256", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("risk", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("run_id", "version", name="uq_delivery_plan_version"),
    )
    op.create_index("ix_delivery_plans_run_id", "delivery_plans", ["run_id"])
    op.create_table(
        "delivery_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("command", sa.JSON(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("repository_sha256", sa.String(64), nullable=True),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("delivery_plans.id"), nullable=True),
        sa.UniqueConstraint("run_id", "sequence", name="uq_delivery_artifact_sequence"),
    )
    op.create_index("ix_delivery_artifacts_run_id", "delivery_artifacts", ["run_id"])
    op.create_index("ix_delivery_artifacts_expires_at", "delivery_artifacts", ["expires_at"])
    op.create_table(
        "delivery_remote_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("business_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("remote_id", sa.String(128), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("delivery_plans.id"), nullable=True),
        sa.UniqueConstraint("workspace_id", "run_id", "business_key", name="uq_delivery_remote_key"),
    )
    op.create_index("ix_delivery_remote_actions_run_id", "delivery_remote_actions", ["run_id"])


def downgrade() -> None:
    op.drop_table("delivery_remote_actions")
    op.drop_table("delivery_artifacts")
    op.drop_table("delivery_plans")
