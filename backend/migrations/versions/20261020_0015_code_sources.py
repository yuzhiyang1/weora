"""代码源 API 身份与仓库构建建议；不复制或迁移本机令牌文件。"""

import sqlalchemy as sa
from alembic import op

revision = "20261020_0015"
down_revision = "20261020_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "code_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("api_origin", sa.String(200), nullable=False),
        sa.Column("organization", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("credential_ciphertext", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_code_sources_workspace_id", "code_sources", ["workspace_id"])
    op.add_column("project_repositories", sa.Column("code_source_id", sa.Uuid(), nullable=True))
    op.add_column(
        "project_repositories", sa.Column("platform_repository", sa.String(20), nullable=False, server_default="")
    )
    op.add_column("project_repositories", sa.Column("build_config", sa.JSON(), nullable=True))
    with op.batch_alter_table("project_repositories") as batch:
        batch.create_foreign_key("fk_repository_code_source", "code_sources", ["code_source_id"], ["id"])
        batch.alter_column("platform_repository", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("project_repositories") as batch:
        batch.drop_constraint("fk_repository_code_source", type_="foreignkey")
        batch.drop_column("build_config")
        batch.drop_column("platform_repository")
        batch.drop_column("code_source_id")
    op.drop_index("ix_code_sources_workspace_id", table_name="code_sources")
    op.drop_table("code_sources")
