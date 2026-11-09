"""项目、多仓库登记与任务冻结资料；既有任务保持 project_context=NULL。"""

import sqlalchemy as sa
from alembic import op

revision = "20261020_0013"
down_revision = "20261019_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("architecture", sa.Text(), nullable=False),
        sa.Column("conventions", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_projects_workspace_id", "projects", ["workspace_id"])
    op.create_table(
        "project_repositories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("git_url", sa.String(2048), nullable=False),
        sa.Column("default_branch", sa.String(120), nullable=False),
        sa.Column("technology", sa.String(500), nullable=False),
        sa.Column("development_notes", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("credential_ciphertext", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "git_url", name="uq_project_repository_url"),
    )
    op.create_index("ix_project_repositories_project_id", "project_repositories", ["project_id"])
    op.create_index("ix_project_repositories_workspace_id", "project_repositories", ["workspace_id"])
    op.add_column("collaboration_tasks", sa.Column("project_context", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("collaboration_tasks", "project_context")
    op.drop_table("project_repositories")
    op.drop_table("projects")
