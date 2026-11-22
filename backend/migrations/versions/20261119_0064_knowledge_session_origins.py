"""记录项目会话可追溯的 Git 起点。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0064"
down_revision = "20261119_0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_session_origins",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("author_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("subject_key", sa.String(64), primary_key=True),
        sa.Column("repository_id", sa.Uuid(), sa.ForeignKey("project_repositories.id"), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("commit", sa.String(64), nullable=False),
        sa.Column("tracked_changes", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("knowledge_session_origins")
