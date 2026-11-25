"""保存后台评分策略和领取、失败、结算历史。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0052"
down_revision = "20261119_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_scoring_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("run_mode", sa.String(12), nullable=False),
        sa.Column("action", sa.String(4), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("run_mode IN ('attended', 'unattended')", name="ck_knowledge_scoring_policy_mode"),
        sa.CheckConstraint("action IN ('off', 'ask', 'auto')", name="ck_knowledge_scoring_policy_action"),
        sa.CheckConstraint("daily_limit BETWEEN 0 AND 100", name="ck_knowledge_scoring_policy_quota"),
    )
    op.create_index(
        "ix_knowledge_scoring_policy_version", "knowledge_scoring_policies", ["workspace_id", "run_mode", "id"],
    )
    op.create_table(
        "knowledge_scoring_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("archive_id", sa.Uuid(), nullable=False),
        sa.Column("archive_revision", sa.Integer(), nullable=False),
        sa.Column("scorer_id", sa.Uuid(), nullable=False),
        sa.Column("scorer_revision", sa.Integer(), nullable=False),
        sa.Column("judge_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("policy_id", sa.Integer(), sa.ForeignKey("knowledge_scoring_policies.id"), nullable=False),
        sa.Column("run_mode", sa.String(12), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(80)),
        sa.Column("label_id", sa.Integer(), sa.ForeignKey("knowledge_session_labels.id")),
        sa.ForeignKeyConstraint(["archive_id", "archive_revision"], [
            "knowledge_shared_session_revisions.archive_id", "knowledge_shared_session_revisions.revision",
        ]),
        sa.ForeignKeyConstraint(["scorer_id", "scorer_revision"], [
            "knowledge_session_scorers.id", "knowledge_session_scorers.revision",
        ]),
        sa.UniqueConstraint(
            "archive_id", "archive_revision", "scorer_id", "scorer_revision", "judge_id", "sequence",
            name="uq_knowledge_scoring_attempt_sequence",
        ),
        sa.CheckConstraint("state IN ('open', 'failed', 'completed')", name="ck_knowledge_scoring_attempt_state"),
        sa.CheckConstraint("sequence BETWEEN 1 AND 4", name="ck_knowledge_scoring_attempt_sequence"),
    )
    op.create_index(
        "ix_knowledge_scoring_attempt_quota", "knowledge_scoring_attempts", ["workspace_id", "judge_id", "started_at"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_scoring_policies LIMIT 1")):
        raise RuntimeError("存在后台评分策略，回退会丢失策略和评分尝试历史。")
    op.drop_table("knowledge_scoring_attempts")
    op.drop_table("knowledge_scoring_policies")
