"""固定改进提案的远端预留、PR 收据及合并证据。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0068"
down_revision = "20261119_0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_improvement_deliveries",
        sa.Column("proposal_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("head_commit", sa.String(64), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(8), nullable=False),
        sa.Column("pr_number", sa.Integer()),
        sa.Column("reserved_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("merge_commit", sa.String(64)),
        sa.Column("benchmark_id", sa.Uuid(), sa.ForeignKey("knowledge_regression_runs.id")),
        sa.ForeignKeyConstraint(
            ["proposal_id", "revision"], ["knowledge_improvement_revisions.proposal_id",
                                         "knowledge_improvement_revisions.revision"],
        ),
    )


def downgrade() -> None:
    op.drop_table("knowledge_improvement_deliveries")
