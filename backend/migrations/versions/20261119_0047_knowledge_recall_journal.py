"""记录有会话归因的召回结果和关系使用。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0047"
down_revision = "20261119_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_recall_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("session_key", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(128)),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("top_score", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("via_relation_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_knowledge_recall_runs_scope", "knowledge_recall_runs",
        ["workspace_id", "user_id", "project_id", "session_key", "created_at"],
    )
    op.create_index("ix_knowledge_recall_runs_created", "knowledge_recall_runs", ["created_at"])
    op.create_table(
        "knowledge_recall_run_hits",
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("knowledge_recall_runs.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("relevant", sa.Boolean(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("related", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_recall_runs LIMIT 1")):
        raise RuntimeError("存在会话召回记录，请在回退前保留运行审计。")
    op.drop_table("knowledge_recall_run_hits")
    op.drop_table("knowledge_recall_runs")
