"""登记固定仓库知识基线和由它派生的发布条目。"""

import sqlalchemy as sa
from alembic import op

revision = "20261118_0037"
down_revision = "20261118_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_codebases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("repository_id", sa.Uuid(), sa.ForeignKey("project_repositories.id"), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("extraction_version", sa.Integer(), nullable=False),
        sa.Column("max_files", sa.Integer(), nullable=False),
        sa.Column("candidates", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indexed_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.UniqueConstraint("repository_id", "connection_version", name="uq_knowledge_codebase_connection"),
    )
    op.add_column("knowledge_entries", sa.Column("codebase_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_knowledge_entry_codebase",
        "knowledge_entries",
        "knowledge_codebases",
        ["codebase_id"],
        ["id"],
    )
    op.create_index("ix_knowledge_entries_codebase_id", "knowledge_entries", ["codebase_id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_codebases LIMIT 1")):
        raise RuntimeError("请保留代码知识快照与发布历史后处理迁移回退。")
    op.drop_index("ix_knowledge_entries_codebase_id", table_name="knowledge_entries")
    op.drop_constraint("fk_knowledge_entry_codebase", "knowledge_entries", type_="foreignkey")
    op.drop_column("knowledge_entries", "codebase_id")
    op.drop_table("knowledge_codebases")
