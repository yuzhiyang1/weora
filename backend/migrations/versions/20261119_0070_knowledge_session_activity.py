"""保存独立于会话正文的人工干预计数。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0070"
down_revision = "20261119_0069"
branch_labels = None
depends_on = None


def _policy_kinds(*, activity: bool) -> None:
    op.drop_constraint("ck_knowledge_session_policy_kind", "knowledge_session_policies", type_="check")
    choices = "'recall', 'share-reminder', 'recall-adoption'"
    if activity:
        choices += ", 'usage-report'"
    op.create_check_constraint(
        "ck_knowledge_session_policy_kind", "knowledge_session_policies", f"kind IN ({choices})",
    )


def upgrade() -> None:
    _policy_kinds(activity=True)
    op.create_table(
        "knowledge_session_activity",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("run_mode", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interrupts", sa.Integer(), nullable=False),
        sa.Column("tool_rejections", sa.Integer(), nullable=False),
        sa.Column("corrections", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "run_mode IN ('attended', 'unattended', 'not_recorded')", name="ck_knowledge_activity_mode",
        ),
        sa.CheckConstraint(
            "interrupts >= 0 AND tool_rejections >= 0 AND corrections >= 0", name="ck_knowledge_activity_counts",
        ),
    )
    op.create_index("ix_knowledge_activity_scope", "knowledge_session_activity", ["workspace_id", "project_id"])


def downgrade() -> None:
    db = op.get_bind()
    if db.scalar(sa.text("SELECT 1 FROM knowledge_session_activity LIMIT 1")) or db.scalar(sa.text(
        "SELECT 1 FROM knowledge_session_policies WHERE kind = 'usage-report' LIMIT 1",
    )):
        raise RuntimeError("存在会话活动或上报策略历史，回退会丢失记录。")
    op.drop_table("knowledge_session_activity")
    _policy_kinds(activity=False)
