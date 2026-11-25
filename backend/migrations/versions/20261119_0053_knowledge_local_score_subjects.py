"""允许团队会话标签引用不包含转录正文的本机证据版本。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0053"
down_revision = "20261119_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_shared_sessions", sa.Column(
        "score_only", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_shared_sessions WHERE score_only LIMIT 1")):
        raise RuntimeError("存在本机评分证据记录，回退会破坏转录的可见性规则。")
    op.drop_column("knowledge_shared_sessions", "score_only")
