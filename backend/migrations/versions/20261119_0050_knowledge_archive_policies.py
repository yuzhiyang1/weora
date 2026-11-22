"""保留工作空间归档策略历史，并关联自动归档所用的策略版本。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0050"
down_revision = "20261119_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_archive_policies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("run_mode", sa.String(12), nullable=False),
        sa.Column("action", sa.String(4), nullable=False),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("run_mode IN ('attended', 'unattended')", name="ck_knowledge_archive_run_mode"),
        sa.CheckConstraint("action IN ('off', 'ask', 'auto')", name="ck_knowledge_archive_action"),
    )
    op.create_index(
        "ix_knowledge_archive_policy_version", "knowledge_archive_policies", ["workspace_id", "run_mode", "id"],
    )
    op.add_column("knowledge_shared_session_revisions", sa.Column("archive_policy_id", sa.Integer()))
    op.create_foreign_key(
        "fk_knowledge_shared_session_archive_policy", "knowledge_shared_session_revisions",
        "knowledge_archive_policies", ["archive_policy_id"], ["id"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_archive_policies LIMIT 1")):
        raise RuntimeError("存在已配置的会话归档策略，回退会丢失策略与审计历史。")
    op.drop_constraint(
        "fk_knowledge_shared_session_archive_policy", "knowledge_shared_session_revisions", type_="foreignkey",
    )
    op.drop_column("knowledge_shared_session_revisions", "archive_policy_id")
    op.drop_table("knowledge_archive_policies")
