"""为项目回归套件、任务和改进提案提供持久化。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0065"
down_revision = "20261119_0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_regression_suites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("path", sa.String(250), nullable=False),
        sa.Column("repository_id", sa.Uuid(), sa.ForeignKey("project_repositories.id"), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=False),
        sa.Column("repetitions", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "project_id", "path", name="uq_knowledge_regression_suite_path"),
        sa.CheckConstraint("repetitions BETWEEN 1 AND 10", name="ck_knowledge_regression_repetitions"),
        sa.CheckConstraint("timeout_seconds > 0", name="ck_knowledge_regression_timeout"),
    )
    op.create_table(
        "knowledge_regression_proposals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("suite_id", sa.Uuid(), sa.ForeignKey("knowledge_regression_suites.id"), nullable=False),
        sa.Column("scorer_id", sa.Uuid(), nullable=False),
        sa.Column("scorer", sa.String(100), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('open', 'merged', 'closed')", name="ck_knowledge_regression_proposal_status"),
    )
    op.create_index("ix_knowledge_regression_proposals_suite_id", "knowledge_regression_proposals", ["suite_id"])
    op.create_index(
        "uq_knowledge_regression_open_proposal", "knowledge_regression_proposals", ["suite_id", "scorer_id"],
        unique=True, postgresql_where=sa.text("status = 'open'"), sqlite_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "knowledge_regression_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("suite_id", sa.Uuid(), sa.ForeignKey("knowledge_regression_suites.id"), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), sa.ForeignKey("knowledge_regression_proposals.id")),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("label_id", sa.Integer(), sa.ForeignKey("knowledge_session_labels.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("starting_commit", sa.String(64), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("success_criteria", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("suite_id", "operation_id", name="uq_knowledge_regression_task_operation"),
    )
    op.create_index("ix_knowledge_regression_tasks_proposal_id", "knowledge_regression_tasks", ["proposal_id"])
    op.create_index("ix_knowledge_regression_task_subject", "knowledge_regression_tasks", ["suite_id", "subject_id"])


def downgrade() -> None:
    op.drop_table("knowledge_regression_tasks")
    op.drop_table("knowledge_regression_proposals")
    op.drop_table("knowledge_regression_suites")
