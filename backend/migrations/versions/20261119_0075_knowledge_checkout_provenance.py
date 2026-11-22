"""保存本地源码快照与项目登记仓库的关联证据。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0075"
down_revision = "20261119_0074"
branch_labels = None
depends_on = None

_TABLE = "knowledge_codebases"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("local_checkout", sa.JSON(none_as_null=True)))


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {_TABLE} WHERE local_checkout IS NOT NULL LIMIT 1")):
        raise RuntimeError("已有本地源码关联记录，回退会丢失来源证据。")
    op.drop_column(_TABLE, "local_checkout")
