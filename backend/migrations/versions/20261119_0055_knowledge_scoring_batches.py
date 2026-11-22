"""同一评分调用可包含多项标准，各标签继续独立审计。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0055"
down_revision = "20261119_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_scoring_policies", sa.Column(
        "batch", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.add_column("knowledge_scoring_attempts", sa.Column("judge_call_id", sa.Uuid()))
    op.add_column("knowledge_session_labels", sa.Column("judge_call_id", sa.Uuid()))
    op.create_unique_constraint(
        "uq_knowledge_session_label_call_scorer", "knowledge_session_labels",
        ["judge_id", "judge_call_id", "scorer_id"],
    )
    op.create_index("ix_knowledge_scoring_attempt_call", "knowledge_scoring_attempts", ["judge_call_id"])


def downgrade() -> None:
    for table, predicate in (
        ("knowledge_scoring_attempts", "judge_call_id IS NOT NULL"),
        ("knowledge_session_labels", "judge_call_id IS NOT NULL"),
        ("knowledge_scoring_policies", "batch"),
    ):
        if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1")):
            raise RuntimeError("存在合并评分配置或记录，回退会丢失调用分组及限额审计。")
    op.drop_index("ix_knowledge_scoring_attempt_call", table_name="knowledge_scoring_attempts")
    op.drop_constraint("uq_knowledge_session_label_call_scorer", "knowledge_session_labels", type_="unique")
    op.drop_column("knowledge_session_labels", "judge_call_id")
    op.drop_column("knowledge_scoring_attempts", "judge_call_id")
    op.drop_column("knowledge_scoring_policies", "batch")
