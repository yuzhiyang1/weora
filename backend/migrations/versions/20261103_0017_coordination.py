"""持久化主 Agent 协调域，不改变已有 Task、Run 或审批表。

Revision ID: 20261103_0017
Revises: 20261103_0016
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261103_0017"
down_revision: str | None = "20261103_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coordination_scopes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("route_key", sa.String(256), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "source_id", "route_key", name="uq_coordination_source_route"),
    )
    op.create_index("ix_coordination_scopes_workspace_id", "coordination_scopes", ["workspace_id"])
    op.create_index("ix_coordination_scopes_source_id", "coordination_scopes", ["source_id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM coordination_scopes)")):
        raise RuntimeError("存在主 Agent 协调记录，须先备份并显式迁移，不能直接回退丢弃任务和发送未知状态")
    op.drop_table("coordination_scopes")
