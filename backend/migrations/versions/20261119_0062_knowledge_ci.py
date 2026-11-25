"""保存 CI 评审提案、合并来源及审核结论。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0062"
down_revision = "20261119_0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_ci_extractions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("repository_id", sa.Uuid(), sa.ForeignKey("project_repositories.id"), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("review_key", sa.String(64), nullable=False),
        sa.Column("review_url", sa.String(1000), nullable=False),
        sa.Column("head_commit", sa.String(64), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("review_author", sa.String(100)),
        sa.Column("staged_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("staged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stage_request_id", sa.String(128)),
        sa.Column("merge_commit", sa.String(64)),
        sa.Column("merged_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_learning", sa.Boolean()),
        sa.Column("rejected_codebase", sa.Boolean()),
        sa.Column("completed_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("complete_request_id", sa.String(128)),
        sa.Column("entry_id", sa.Uuid(), sa.ForeignKey("knowledge_entries.id")),
        sa.UniqueConstraint(
            "repository_id", "review_key", "head_commit", "connection_version", name="uq_knowledge_ci_source",
        ),
    )
    op.create_index(
        "uq_knowledge_ci_merge", "knowledge_ci_extractions", ["repository_id", "review_key"],
        unique=True, postgresql_where=sa.text("merge_commit IS NOT NULL"),
    )


def downgrade() -> None:
    # 删除收据会丢失已评审版本的幂等状态，后续重新运行可能生成重复经验。
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_ci_extractions LIMIT 1")):
        raise RuntimeError("存在 CI 评审提案或合并收据，无法移除记录。")
    op.drop_table("knowledge_ci_extractions")
