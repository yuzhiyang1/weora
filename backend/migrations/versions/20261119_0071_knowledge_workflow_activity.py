"""记录知识工作流的已完成尝试，按逻辑运行统计最近终态。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0071"
down_revision = "20261119_0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_workflow_attempts",
        sa.Column("repository_id", sa.Uuid(), sa.ForeignKey("project_repositories.id"), primary_key=True),
        sa.Column("run_id", sa.String(20), primary_key=True),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("reported_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workflow", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint("attempt > 0 AND connection_version > 0 AND duration_ms >= 0",
                           name="ck_knowledge_workflow_attempt_counts"),
        sa.CheckConstraint("outcome IN ('succeeded', 'failed', 'cancelled', 'skipped')",
                           name="ck_knowledge_workflow_attempt_outcome"),
        sa.CheckConstraint("outcome <> 'skipped' OR duration_ms = 0", name="ck_knowledge_workflow_skipped_duration"),
    )
    op.create_index(
        "ix_knowledge_workflow_attempt_scope", "knowledge_workflow_attempts",
        ["workspace_id", "project_id", "repository_id", "run_id", "attempt"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_workflow_attempts LIMIT 1")):
        raise RuntimeError("已有工作流结算记录，回退会丢失审计和周报计数。")
    op.drop_table("knowledge_workflow_attempts")
