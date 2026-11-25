"""持久化基准运行的执行条件和首次登记者。"""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0099"
down_revision = "20261121_0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_regression_runs", sa.Column("execution", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    # 已登记运行依赖配置校验保护结算。回退不能把它们变成未绑定运行。
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_regression_runs WHERE execution IS NOT NULL LIMIT 1")):
        raise RuntimeError("存在已登记执行条件的基准运行，无法安全移除该列。")
    op.drop_column("knowledge_regression_runs", "execution")
