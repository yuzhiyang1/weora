"""区分本机快照采集与平台发布时点，用于归档评分。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0049"
down_revision = "20261119_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 历史版本只有保存时间，保留该差异以免伪造本机采集证据。
    op.add_column("knowledge_shared_session_revisions", sa.Column("captured_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(
        "SELECT 1 FROM knowledge_shared_session_revisions WHERE captured_at IS NOT NULL LIMIT 1",
    )):
        raise RuntimeError("存在已归档的会话采集时间，回退会丢失评分证据边界。")
    op.drop_column("knowledge_shared_session_revisions", "captured_at")
