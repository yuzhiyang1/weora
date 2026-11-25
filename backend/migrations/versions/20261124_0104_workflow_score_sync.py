"""保存团队报表分支绑定、同步租约及成功快照。"""

import sqlalchemy as sa
from alembic import op

revision = "20261124_0104"
down_revision = "20261124_0103"
branch_labels = None
depends_on = None

SOURCE = "knowledge_workflow_score_sync_sources"
FILE = "knowledge_workflow_score_sync_files"


def upgrade() -> None:
    op.create_table(
        SOURCE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("scope_key", sa.String(36), nullable=False),
        sa.Column("source_project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("repository_id", sa.Uuid(), sa.ForeignKey("project_repositories.id"), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("configured_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("configured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.String(64)),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_id", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(80)),
        sa.UniqueConstraint("workspace_id", "scope_key", name="uq_workflow_score_sync_scope"),
    )
    op.create_index("ix_workflow_score_sync_due", SOURCE, ["enabled", "next_sync_at"])
    op.create_table(
        FILE,
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey(f"{SOURCE}.id"), primary_key=True),
        sa.Column("path", sa.String(280), primary_key=True),
        sa.Column("oid", sa.String(64), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    # 同步快照由 Git 重建，本地手动导入表不受此迁移影响。
    op.drop_table(FILE)
    op.drop_table(SOURCE)
