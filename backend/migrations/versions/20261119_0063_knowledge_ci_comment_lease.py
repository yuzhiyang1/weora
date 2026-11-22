"""串行处理同一评审的远程评论和合并发布。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0063"
down_revision = "20261119_0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_ci_extractions", sa.Column("comment_lease_id", sa.Uuid()))
    op.add_column("knowledge_ci_extractions", sa.Column("comment_lease_until", sa.DateTime(timezone=True)))


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(
        "SELECT 1 FROM knowledge_ci_extractions WHERE comment_lease_until > CURRENT_TIMESTAMP LIMIT 1"
    )):
        raise RuntimeError("存在进行中的 CI 评论操作，无法移除评论租约。")
    op.drop_column("knowledge_ci_extractions", "comment_lease_until")
    op.drop_column("knowledge_ci_extractions", "comment_lease_id")
