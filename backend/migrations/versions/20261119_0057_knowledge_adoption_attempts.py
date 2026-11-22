"""保存会话证据的归因键及生成式采纳评分历史。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0057"
down_revision = "20261119_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_shared_session_revisions", sa.Column("recall_session_key", sa.String(64)))
    op.create_table(
        "knowledge_adoption_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("archive_id", sa.Uuid(), nullable=False),
        sa.Column("archive_revision", sa.Integer(), nullable=False),
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("entry_revision", sa.Integer(), nullable=False),
        sa.Column("session_key", sa.String(64), nullable=False),
        sa.Column("judge_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("policy_id", sa.Integer(), sa.ForeignKey("knowledge_scoring_policies.id"), nullable=False),
        sa.Column("run_mode", sa.String(12), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(80)),
        sa.Column("label", sa.String(12)),
        sa.Column("applied", sa.Boolean()),
        sa.Column("request_id", sa.String(128)),
        sa.ForeignKeyConstraint(["archive_id", "archive_revision"], [
            "knowledge_shared_session_revisions.archive_id", "knowledge_shared_session_revisions.revision",
        ]),
        sa.ForeignKeyConstraint(["entry_id", "entry_revision"], [
            "knowledge_revisions.entry_id", "knowledge_revisions.revision",
        ]),
        sa.UniqueConstraint(
            "judge_id", "session_key", "entry_id", "entry_revision", "sequence",
            name="uq_knowledge_adoption_attempt_sequence",
        ),
        sa.CheckConstraint("state IN ('open', 'failed', 'completed')", name="ck_knowledge_adoption_attempt_state"),
        sa.CheckConstraint("sequence BETWEEN 1 AND 4", name="ck_knowledge_adoption_attempt_sequence"),
        sa.CheckConstraint("label IN ('adopted', 'ignored')", name="ck_knowledge_adoption_attempt_label"),
    )
    op.create_index(
        "ix_knowledge_adoption_attempt_quota", "knowledge_adoption_attempts",
        ["workspace_id", "judge_id", "started_at"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_adoption_attempts LIMIT 1")):
        raise RuntimeError("存在后台采纳评分历史，回退会丢失已领取的任务和评分收据。")
    op.drop_table("knowledge_adoption_attempts")
    op.drop_column("knowledge_shared_session_revisions", "recall_session_key")
