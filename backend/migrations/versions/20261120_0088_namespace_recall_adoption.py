"""评分目录可独立控制自动召回采纳问题。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0088"
down_revision = "20261120_0087"
branch_labels = None
depends_on = None

_TABLE = "knowledge_scoring_policies"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("recall_adoption", sa.Boolean(), nullable=True))


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM knowledge_scoring_policies WHERE recall_adoption IS NOT NULL LIMIT 1",
    )).first():
        raise RuntimeError("已有目录采纳评分设置，无法无损回退")
    op.drop_column(_TABLE, "recall_adoption")
