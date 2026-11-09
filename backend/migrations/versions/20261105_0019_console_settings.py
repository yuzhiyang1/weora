"""用户头像与团队设置的不可变版本。

Revision ID: 20261105_0019
Revises: 20261104_0018
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105_0019"
down_revision = "20261104_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增字段可空，旧账户和固定运行无需数据回填。"""
    op.add_column("users", sa.Column("avatar_data_url", sa.Text(), nullable=True))
    op.create_table(
        "console_setting_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("encrypted_key", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "kind", "revision", name="uq_console_setting_revision"),
    )
    op.create_index("ix_console_setting_versions_workspace_id", "console_setting_versions", ["workspace_id"])
    op.create_table(
        "console_setting_heads",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("console_setting_versions.id"), nullable=False),
    )


def downgrade() -> None:
    """回退前应备份配置版本；不会修改任务、审批或认证会话。"""
    op.drop_table("console_setting_heads")
    op.drop_index("ix_console_setting_versions_workspace_id", table_name="console_setting_versions")
    op.drop_table("console_setting_versions")
    op.drop_column("users", "avatar_data_url")
