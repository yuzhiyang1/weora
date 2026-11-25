"""保存一次点名评分调用的原标签和采纳记账收据。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0060"
down_revision = "20261119_0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_named_score_calls",
        sa.Column("judge_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("archive_id", sa.Uuid(), nullable=False),
        sa.Column("archive_revision", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(128)),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["archive_id", "archive_revision"], [
            "knowledge_shared_session_revisions.archive_id", "knowledge_shared_session_revisions.revision",
        ]),
    )
    op.create_index(
        "ix_knowledge_named_score_call_usage", "knowledge_named_score_calls",
        ["workspace_id", "judge_id", "created_at"],
    )
    op.create_index(
        "ix_knowledge_named_score_call_subject", "knowledge_named_score_calls",
        ["archive_id", "archive_revision", "created_at"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_named_score_calls LIMIT 1")):
        raise RuntimeError("存在点名评分调用，回退会丢失生成标签及额度审计。")
    op.drop_table("knowledge_named_score_calls")
