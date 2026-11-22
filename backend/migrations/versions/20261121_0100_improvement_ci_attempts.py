"""记录 Actions 运行和尝试，让同一次工作流续跑固定基准。"""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0100"
down_revision = "20261121_0099"
branch_labels = None
depends_on = None

TABLE = "knowledge_improvement_checks"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("workflow_run", sa.String(30), nullable=True))
    op.add_column(TABLE, sa.Column("attempt", sa.String(30), nullable=True))
    op.create_unique_constraint(
        "uq_knowledge_improvement_check_attempt", TABLE, ["proposal_id", "revision", "workflow_run", "attempt"],
    )
    op.create_check_constraint(
        "ck_knowledge_improvement_check_attempt", TABLE,
        "(workflow_run IS NULL AND attempt IS NULL) OR (workflow_run IS NOT NULL AND attempt IS NOT NULL)",
    )
    op.create_index("ix_knowledge_improvement_checks_benchmark", TABLE, ["benchmark_id"])


def downgrade() -> None:
    tracked = sa.text("SELECT 1 FROM knowledge_improvement_checks WHERE workflow_run IS NOT NULL LIMIT 1")
    if op.get_bind().scalar(tracked):
        raise RuntimeError("存在需要尝试身份保护的 CI 基准，无法安全移除字段。")
    op.drop_index("ix_knowledge_improvement_checks_benchmark", table_name=TABLE)
    op.drop_constraint("ck_knowledge_improvement_check_attempt", TABLE, type_="check")
    op.drop_constraint("uq_knowledge_improvement_check_attempt", TABLE, type_="unique")
    op.drop_column(TABLE, "attempt")
    op.drop_column(TABLE, "workflow_run")
