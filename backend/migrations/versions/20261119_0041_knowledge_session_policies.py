"""保存工作空间召回和会话分享提醒的设置历史。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0041"
down_revision = "20261119_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_session_policies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('recall', 'share-reminder')", name="ck_knowledge_session_policy_kind"),
    )
    op.create_index(
        "ix_knowledge_session_policy_workspace_version",
        "knowledge_session_policies",
        ["workspace_id", "kind", "id"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_session_policies LIMIT 1")):
        raise RuntimeError("请保留工作空间的会话策略历史后处理迁移回退。")
    op.drop_table("knowledge_session_policies")
