"""记录多个采纳问题共享的一次评分调用。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0059"
down_revision = "20261119_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_adoption_attempts", sa.Column("judge_call_id", sa.Uuid()))
    op.create_index("ix_knowledge_adoption_attempt_call", "knowledge_adoption_attempts", ["judge_call_id"])


def downgrade() -> None:
    grouped = sa.text("SELECT 1 FROM knowledge_adoption_attempts WHERE judge_call_id IS NOT NULL LIMIT 1")
    if op.get_bind().scalar(grouped):
        raise RuntimeError("存在合并采纳评分记录，回退会丢失调用分组及限额审计。")
    op.drop_index("ix_knowledge_adoption_attempt_call", table_name="knowledge_adoption_attempts")
    op.drop_column("knowledge_adoption_attempts", "judge_call_id")
