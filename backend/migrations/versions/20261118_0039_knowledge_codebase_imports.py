"""保存批量导入每个仓库成功发布后的重放凭证。"""

import sqlalchemy as sa
from alembic import op

revision = "20261118_0039"
down_revision = "20261118_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_codebase_imports",
        sa.Column("codebase_id", sa.Uuid(), sa.ForeignKey("knowledge_codebases.id"), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_codebase_imports LIMIT 1")):
        raise RuntimeError("请保留批量索引重放记录后处理迁移回退。")
    op.drop_table("knowledge_codebase_imports")
