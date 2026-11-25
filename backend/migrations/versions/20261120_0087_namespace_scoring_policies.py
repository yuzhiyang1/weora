"""让评分目录按字段声明策略，同时延续原有评分领取外键。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0087"
down_revision = "20261120_0086"
branch_labels = None
depends_on = None

_TABLE = "knowledge_scoring_policies"
_FIELDS = ("action", "daily_limit", "batch")
_CHECK = "ck_knowledge_scoring_policy_workspace_values"
_INDEX = "ix_knowledge_scoring_policy_namespace"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("namespace", sa.String(64), nullable=False, server_default=""))
    for column in _FIELDS:
        op.alter_column(_TABLE, column, nullable=True)
    op.create_check_constraint(
        _CHECK, _TABLE, "namespace <> '' OR (action IS NOT NULL AND daily_limit IS NOT NULL AND batch IS NOT NULL)",
    )
    op.create_index(_INDEX, _TABLE, ["workspace_id", "namespace", "run_mode", "id"])


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM knowledge_scoring_policies WHERE namespace <> '' LIMIT 1")).first():
        raise RuntimeError("已有评分目录策略或领取审计，无法无损回退")
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    for column in _FIELDS:
        op.alter_column(_TABLE, column, nullable=False)
    op.drop_column(_TABLE, "namespace")
