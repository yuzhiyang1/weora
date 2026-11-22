"""保存工作空间近期合并提醒的设置历史。"""

import sqlalchemy as sa
from alembic import op

revision = "20261118_0036"
down_revision = "20261118_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_hint_policies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("lookback_days", sa.Integer(), nullable=False),
        sa.Column("max_mrs", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("lookback_days BETWEEN 1 AND 90", name="ck_knowledge_hint_days"),
        sa.CheckConstraint("max_mrs BETWEEN 1 AND 100", name="ck_knowledge_hint_max"),
    )
    op.create_index("ix_knowledge_hint_policies_workspace_version", "knowledge_hint_policies", ["workspace_id", "id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_hint_policies LIMIT 1")):
        raise RuntimeError("请备份工作空间提醒设置的历史后处理迁移回退。")
    op.drop_table("knowledge_hint_policies")
