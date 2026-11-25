"""固定每个 PR 提交的 CI 运行与最新尝试。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0069"
down_revision = "20261119_0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_improvement_checks",
        sa.Column("proposal_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("benchmark_id", sa.Uuid(), sa.ForeignKey("knowledge_regression_runs.id"), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id", "revision"], ["knowledge_improvement_deliveries.proposal_id",
                                         "knowledge_improvement_deliveries.revision"],
        ),
        sa.UniqueConstraint("proposal_id", "revision", "operation_id", name="uq_knowledge_improvement_check_operation"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_improvement_checks")
