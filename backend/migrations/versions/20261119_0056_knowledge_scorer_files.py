"""将评分目录附件保存在评分标准的不可变版本中。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0056"
down_revision = "20261119_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_session_scorers", sa.Column("files", sa.JSON(none_as_null=True)))
    op.add_column("knowledge_session_scorers", sa.Column("source_hash", sa.String(64)))


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_session_scorers WHERE files IS NOT NULL LIMIT 1")):
        raise RuntimeError("存在目录形式的评分标准，回退会丢失已固定的附件证据。")
    op.drop_column("knowledge_session_scorers", "source_hash")
    op.drop_column("knowledge_session_scorers", "files")
