"""为补发的手工反馈记录最后一次操作及发生时间。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0078"
down_revision = "20261120_0077"
branch_labels = None
depends_on = None

_TABLE = "knowledge_recalls"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("manual_vote_at", sa.DateTime(timezone=True)))
    op.add_column(_TABLE, sa.Column("manual_vote_id", sa.Uuid()))


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {_TABLE} WHERE manual_vote_at IS NOT NULL LIMIT 1")):
        raise RuntimeError("已有手工反馈顺序记录，回退会丢失补发保护。")
    op.drop_column(_TABLE, "manual_vote_id")
    op.drop_column(_TABLE, "manual_vote_at")
