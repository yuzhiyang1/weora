"""持久化工作流评分的不可变判定快照。"""

import sqlalchemy as sa
from alembic import op

revision = "20261124_0103"
down_revision = "20261121_0102"
branch_labels = None
depends_on = None

TABLE = "knowledge_workflow_scores"
WINDOW = "ix_knowledge_workflow_scores_window"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("label_key", sa.String(64), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("imported_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index(WINDOW, TABLE, ["workspace_id", "project_id", "scored_at"])


def downgrade() -> None:
    # 评分无法从普通工作流终态重建，降级只允许移除尚未使用的表。
    connection = op.get_bind()
    if connection.scalar(sa.text(f"SELECT 1 FROM {TABLE} LIMIT 1")) is not None:
        raise RuntimeError("工作流评分表已有导入记录，不能降级丢弃。")
    op.drop_table(TABLE)
