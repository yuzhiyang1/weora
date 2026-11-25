"""补齐从早期仓库迁移链升级时可能漏掉的 revision 可空约束。"""

import sqlalchemy as sa
from alembic import op

revision = "20261125_0036"
down_revision = "20261125_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """允许首次准备仓库时写入尚无提交 ID 的记录。"""
    table = "project_run_repositories"
    # 历史 0027 可能已经执行，后来插入的 0026 在这些数据库上会被跳过。
    if not op.get_context().as_sql:
        columns = sa.inspect(op.get_bind()).get_columns(table)
        if next(column for column in columns if column["name"] == "revision")["nullable"]:
            return

    with op.batch_alter_table(table) as batch:
        batch.alter_column("revision", existing_type=sa.String(64), nullable=True)


def downgrade() -> None:
    """前一版本的应用也会写入空修订，回退时保留该约束和待准备记录。"""
