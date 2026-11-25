"""建立项目知识、不可变正文版本、审核与召回记录。"""

import sqlalchemy as sa
from alembic import op

revision = "20261118_0035"
down_revision = "20261113_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增知识领域表，既有工作空间和项目数据无需回填。"""
    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_revision", sa.Integer()),
        sa.Column("promoted_from", sa.Uuid(), sa.ForeignKey("knowledge_entries.id")),
        sa.CheckConstraint("status IN ('pending', 'active', 'archived', 'rejected')", name="ck_knowledge_entry_status"),
        sa.CheckConstraint("kind IN ('learning', 'rule', 'doc', 'code', 'skill')", name="ck_knowledge_entry_kind"),
    )
    op.create_index("ix_knowledge_entries_scope", "knowledge_entries", ["workspace_id", "project_id", "status"])
    op.create_index("ix_knowledge_entries_promoted_from", "knowledge_entries", ["promoted_from"])
    op.create_table(
        "knowledge_revisions",
        sa.Column("entry_id", sa.Uuid(), sa.ForeignKey("knowledge_entries.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("author_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("relations", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "author_id", "operation_id", name="uq_knowledge_operation"),
        sa.CheckConstraint("status IN ('pending', 'published', 'rejected')", name="ck_knowledge_revision_status"),
    )
    op.create_table(
        "knowledge_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("entry_id", sa.Uuid(), sa.ForeignKey("knowledge_entries.id"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer()),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_events_entry", "knowledge_events", ["entry_id", "occurred_at"])
    op.create_table(
        "knowledge_recalls",
        sa.Column("entry_id", sa.Uuid(), sa.ForeignKey("knowledge_entries.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("session_key", sa.String(64), primary_key=True),
        sa.Column("recall_count", sa.Integer(), nullable=False),
        sa.Column("last_recalled_at", sa.DateTime(timezone=True)),
        sa.Column("positive", sa.Boolean()),
        sa.Column("voted_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_knowledge_recalls_entry", "knowledge_recalls", ["entry_id"])


def downgrade() -> None:
    """回退前保留已有知识的数据库备份，避免删除用户内容。"""
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_entries LIMIT 1")):
        raise RuntimeError("知识库已有条目，请归档数据库后处理迁移回退。")
    op.drop_table("knowledge_recalls")
    op.drop_table("knowledge_events")
    op.drop_table("knowledge_revisions")
    op.drop_table("knowledge_entries")
