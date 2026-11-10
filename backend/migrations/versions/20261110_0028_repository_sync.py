"""登记仓库同步租约和固定本地代码的来源时间。"""

import sqlalchemy as sa
from alembic import op

revision = "20261110_0028"
down_revision = "20261110_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("project_run_repositories") as batch:
        batch.alter_column("revision", existing_type=sa.String(64), nullable=True)
        batch.add_column(sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "project_repository_syncs",
        sa.Column("repository_id", sa.Uuid(), sa.ForeignKey("project_repositories.id"), primary_key=True),
        sa.Column("connection_version", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("cache_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("revision", sa.String(64), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
    )
    op.create_index("ix_project_repository_syncs_workspace_id", "project_repository_syncs", ["workspace_id"])
    op.create_index("ix_project_repository_syncs_next_sync_at", "project_repository_syncs", ["next_sync_at"])


def downgrade() -> None:
    op.drop_table("project_repository_syncs")
    # 旧版无法表示尚未解析提交的记录。
    op.execute("DELETE FROM project_run_repositories WHERE revision IS NULL")
    with op.batch_alter_table("project_run_repositories") as batch:
        batch.drop_column("synced_at")
        batch.alter_column("revision", existing_type=sa.String(64), nullable=False)
