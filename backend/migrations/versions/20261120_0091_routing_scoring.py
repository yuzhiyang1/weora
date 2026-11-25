"""为自动 Skill 路由配置和重试保存独立的调用快照。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0091"
down_revision = "20261120_0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_scoring_policies", sa.Column("skill_routing", sa.Boolean(), nullable=True))
    op.create_table(
        "knowledge_routing_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("judge_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("archive_id", sa.Uuid(), nullable=False),
        sa.Column("archive_revision", sa.Integer(), nullable=False),
        sa.Column("policy_id", sa.Integer(), sa.ForeignKey("knowledge_scoring_policies.id"), nullable=False),
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("run_mode", sa.String(12), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(80)),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column("labels", sa.JSON(none_as_null=True)),
        sa.Column("requires_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(
            ["archive_id", "archive_revision"], ["knowledge_shared_session_revisions.archive_id",
                                                 "knowledge_shared_session_revisions.revision"],
        ),
        sa.UniqueConstraint("series_id", "sequence", name="uq_knowledge_routing_attempt_series"),
        sa.CheckConstraint("state IN ('open', 'failed', 'completed')", name="ck_knowledge_routing_attempt_state"),
        sa.CheckConstraint("sequence BETWEEN 1 AND 4", name="ck_knowledge_routing_attempt_sequence"),
    )
    op.create_index("ix_knowledge_routing_attempt_quota", "knowledge_routing_attempts",
                    ["workspace_id", "judge_id", "started_at"])
    op.create_index("ix_knowledge_routing_attempt_subject", "knowledge_routing_attempts",
                    ["archive_id", "archive_revision", "judge_id"])


def downgrade() -> None:
    db = op.get_bind()
    if db.execute(sa.text("SELECT 1 FROM knowledge_routing_attempts LIMIT 1")).first() or db.execute(sa.text(
        "SELECT 1 FROM knowledge_scoring_policies WHERE skill_routing IS NOT NULL LIMIT 1",
    )).first():
        raise RuntimeError("已有 Skill 路由策略或调用记录，无法无损回退")
    op.drop_table("knowledge_routing_attempts")
    op.drop_column("knowledge_scoring_policies", "skill_routing")
