"""把触发仓库索引的 PR/MR 链接与实际采集提交关联。"""

import sqlalchemy as sa
from alembic import op

revision = "20261118_0038"
down_revision = "20261118_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_codebase_reviews",
        sa.Column("codebase_id", sa.Uuid(), sa.ForeignKey("knowledge_codebases.id"), primary_key=True),
        sa.Column("url", sa.String(1000), primary_key=True),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indexed_by", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_codebase_reviews LIMIT 1")):
        raise RuntimeError("请保留仓库的 PR/MR 来源记录后处理迁移回退。")
    op.drop_table("knowledge_codebase_reviews")
