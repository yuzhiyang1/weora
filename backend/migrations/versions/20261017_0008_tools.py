"""工具调用、持久预算与正文保留期。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261017_0008"
down_revision: str | None = "20261017_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_budgets",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("used", sa.Integer(), nullable=False),
    )
    op.create_table(
        "tool_invocations",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("call_id", sa.String(128), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "call_id", name="uq_tool_invocation_call"),
    )


def downgrade() -> None:
    op.drop_table("tool_invocations")
    op.drop_table("tool_budgets")
