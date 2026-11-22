"""原子保存自我改进的资源差异与新增回归任务。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0067"
down_revision = "20261119_0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_improvement_revisions",
        sa.Column("proposal_id", sa.Uuid(), sa.ForeignKey("knowledge_regression_proposals.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("suite_id", sa.Uuid(), sa.ForeignKey("knowledge_regression_suites.id"), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("resource_repository_id", sa.Uuid(), sa.ForeignKey("project_repositories.id"), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("base_commit", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("task_ids", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("suite_id", "operation_id", name="uq_knowledge_improvement_operation"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_improvement_revisions")
