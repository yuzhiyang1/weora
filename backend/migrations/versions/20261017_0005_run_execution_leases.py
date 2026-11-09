"""为运行增加固定模型配置、租约、取消请求与私有输出。

Revision ID: 20261017_0005
Revises: 20261017_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261017_0005"
down_revision: str | None = "20261017_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """既有排队任务不自动套用新的模型配置，也不在迁移中调用外部服务。"""
    op.add_column("runs", sa.Column("execution_profile", sa.JSON(), nullable=True))
    op.add_column("runs", sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("output_text", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("failure_code", sa.String(64), nullable=True))
    op.add_column("runs", sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column("runs", sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """仅回退新增列；回退会丢失模型输出和租约证据，必须先停 Worker 并备份。"""
    with op.batch_alter_table("runs") as batch:
        for name in (
            "lease_expires_at",
            "fencing_token",
            "lease_owner",
            "failure_code",
            "output_text",
            "cancellation_requested_at",
            "execution_profile",
        ):
            batch.drop_column(name)
