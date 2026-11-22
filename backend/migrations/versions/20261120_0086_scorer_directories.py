"""保留评分标准的目录身份，让同名标准各自保有发布历史。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0086"
down_revision = "20261120_0085"
branch_labels = None
depends_on = None

_TABLE = "knowledge_session_scorers"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("namespace", sa.String(64), nullable=False, server_default=""))


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM knowledge_session_scorers WHERE namespace <> '' LIMIT 1")).first():
        raise RuntimeError("已有评分器目录及版本记录，无法无损回退")
    op.drop_column(_TABLE, "namespace")
