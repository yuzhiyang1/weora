"""按策略和不可变评分标准记录最低运行隔离级别。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0089"
down_revision = "20261120_0088"
branch_labels = None
depends_on = None

_TARGETS = (
    ("knowledge_scoring_policies", "ck_knowledge_scoring_policy_isolation"),
    ("knowledge_session_scorers", "ck_knowledge_scorer_isolation"),
    ("knowledge_session_labels", "ck_knowledge_session_label_isolation"),
    ("knowledge_adoption_attempts", "ck_knowledge_adoption_attempt_isolation"),
)
_ATTEMPTS = ("knowledge_scoring_attempts", "knowledge_adoption_attempts")


def upgrade() -> None:
    for table, constraint in _TARGETS:
        op.add_column(table, sa.Column("isolation", sa.String(12), nullable=True))
        op.create_check_constraint(constraint, table, "isolation IN ('best-effort', 'verified')")
    for table in _ATTEMPTS:
        op.add_column(table, sa.Column("requires_verified", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    for table, _ in _TARGETS:
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} WHERE isolation IS NOT NULL LIMIT 1")).first():
            raise RuntimeError("已有评分隔离设置，回退会丢失评分约束")
    for table in _ATTEMPTS:
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} WHERE requires_verified LIMIT 1")).first():
            raise RuntimeError("已有评分隔离设置，回退会丢失租约约束")
    for table in _ATTEMPTS:
        op.drop_column(table, "requires_verified")
    for table, constraint in reversed(_TARGETS):
        op.drop_constraint(constraint, table, type_="check")
        op.drop_column(table, "isolation")
