"""仓库连接版本、校验状态与按 Run 固定的只读提交。"""

import sqlalchemy as sa
from alembic import op

revision = "20261020_0014"
down_revision = "20261020_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_repositories", sa.Column("credential_username", sa.String(200), nullable=False, server_default="")
    )
    op.add_column(
        "project_repositories", sa.Column("connection_version", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("project_repositories", sa.Column("connection_check", sa.JSON(), nullable=True))
    # 填充历史记录后移除 server default，与应用模型保持同一默认值所有权。
    with op.batch_alter_table("project_repositories") as batch:
        batch.alter_column("credential_username", server_default=None)
        batch.alter_column("connection_version", server_default=None)
    op.create_table(
        "project_run_repositories",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("repository_id", sa.Uuid(), sa.ForeignKey("project_repositories.id"), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("cache_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_project_run_repositories_workspace_id", "project_run_repositories", ["workspace_id"])


def downgrade() -> None:
    op.drop_table("project_run_repositories")
    with op.batch_alter_table("project_repositories") as batch:
        batch.drop_column("connection_check")
        batch.drop_column("connection_version")
        batch.drop_column("credential_username")
