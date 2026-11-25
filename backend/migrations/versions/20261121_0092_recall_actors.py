"""为带标记的召回记录保存执行子 Agent 的摘要。"""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0092"
down_revision = "20261120_0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 原有召回按普通调用解释；历史记录没有可以安全回填的子 Agent 身份。
    op.add_column("knowledge_recall_runs", sa.Column("recall_actor_digest", sa.String(64), nullable=True))


def downgrade() -> None:
    marked = sa.text("SELECT 1 FROM knowledge_recall_runs WHERE recall_actor_digest IS NOT NULL LIMIT 1")
    if op.get_bind().scalar(marked):
        raise RuntimeError("已有子 Agent 召回归因，无法无损回退。")
    op.drop_column("knowledge_recall_runs", "recall_actor_digest")
