"""持久化基准运行、两组资源版本和试验判定。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0066"
down_revision = "20261119_0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_regression_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("suite_id", sa.Uuid(), sa.ForeignKey("knowledge_regression_suites.id"), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), sa.ForeignKey("knowledge_regression_proposals.id")),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("repository_version", sa.Integer(), nullable=False),
        sa.Column("resource_repository_id", sa.Uuid(), sa.ForeignKey("project_repositories.id"), nullable=False),
        sa.Column("resource_repository_version", sa.Integer(), nullable=False),
        sa.Column("base_resources", sa.String(64), nullable=False),
        sa.Column("proposal_resources", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("suite_id", "operation_id", name="uq_knowledge_regression_run_operation"),
    )
    op.create_table(
        "knowledge_regression_trials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("knowledge_regression_runs.id"), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("knowledge_regression_tasks.id"), nullable=False),
        sa.Column("tool", sa.String(12), nullable=False),
        sa.Column("arm", sa.String(8), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(12)),
        sa.Column("judge_tool", sa.String(12)),
        sa.Column("judge_model", sa.String(160)),
        sa.Column("judge_effort", sa.String(32)),
        sa.Column("recorded_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("recorded_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "run_id", "task_id", "tool", "arm", "repetition", name="uq_knowledge_regression_trial_slot",
        ),
        sa.CheckConstraint("tool IN ('claude', 'codex')", name="ck_knowledge_regression_trial_tool"),
        sa.CheckConstraint("arm IN ('main', 'pulled')", name="ck_knowledge_regression_trial_arm"),
        sa.CheckConstraint("repetition > 0", name="ck_knowledge_regression_trial_repetition"),
        sa.CheckConstraint(
            "outcome IN ('passed', 'failed', 'error', 'unscored')", name="ck_knowledge_regression_trial_outcome",
        ),
        sa.CheckConstraint(
            "(outcome IN ('passed', 'failed') AND judge_tool IS NOT NULL AND judge_model IS NOT NULL)"
            " OR ((outcome IS NULL OR outcome IN ('error', 'unscored'))"
            " AND judge_tool IS NULL AND judge_model IS NULL AND judge_effort IS NULL)",
            name="ck_knowledge_regression_trial_judge",
        ),
    )


def downgrade() -> None:
    op.drop_table("knowledge_regression_trials")
    op.drop_table("knowledge_regression_runs")
