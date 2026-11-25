"""保留提交者确认分享的会话版本和项目作用域。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0048"
down_revision = "20261119_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_shared_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("author_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("runtime", sa.String(16), nullable=False),
        sa.Column("latest_revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("runtime IN ('codex', 'claude-code')", name="ck_knowledge_shared_session_runtime"),
    )
    op.create_index(
        "ix_knowledge_shared_sessions_scope", "knowledge_shared_sessions",
        ["workspace_id", "project_id", "updated_at", "id"],
    )
    op.create_table(
        "knowledge_shared_session_revisions",
        sa.Column("archive_id", sa.Uuid(), sa.ForeignKey("knowledge_shared_sessions.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("turns", sa.JSON(), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(128)),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("archive_id", "content_digest", name="uq_knowledge_shared_session_contents"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_shared_session_revisions LIMIT 1")):
        raise RuntimeError("存在已分享会话，请先保留归档再处理回退。")
    op.drop_table("knowledge_shared_session_revisions")
    op.drop_table("knowledge_shared_sessions")
