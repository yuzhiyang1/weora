"""在根会话统计中独立保存提示轮次和四类 Token。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0072"
down_revision = "20261119_0071"
branch_labels = None
depends_on = None

_TABLE = "knowledge_session_activity"
_TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def upgrade() -> None:
    # 旧 Hook 只上报人工干预，缺失用量保持为空。
    op.add_column(_TABLE, sa.Column("prompts", sa.Integer()))
    op.add_column(_TABLE, sa.Column("tokens_observed_at", sa.DateTime(timezone=True)))
    for name in _TOKENS:
        op.add_column(_TABLE, sa.Column(name, sa.BigInteger()))
    op.create_check_constraint(
        "ck_knowledge_activity_conversation_counts", _TABLE,
        "prompts >= 0 AND input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
        "AND cache_creation_tokens >= 0",
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(
        "SELECT 1 FROM knowledge_session_activity WHERE prompts IS NOT NULL OR tokens_observed_at IS NOT NULL LIMIT 1",
    )):
        raise RuntimeError("已有会话用量记录，回退会丢失周报统计。")
    op.drop_constraint("ck_knowledge_activity_conversation_counts", _TABLE, type_="check")
    for name in reversed(("prompts", "tokens_observed_at", *_TOKENS)):
        op.drop_column(_TABLE, name)
