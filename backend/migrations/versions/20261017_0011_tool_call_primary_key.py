"""只保留工具调用复合主键，移除等价的冗余唯一约束。"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261017_0011"
down_revision: str | None = "20261017_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """不删除行或主键；兼容 PostgreSQL 建表时已合并重复声明的情况。"""
    if op.get_context().dialect.name == "postgresql":
        # PostgreSQL 可能把显式 UNIQUE 名称用于合并后的主键，不能只按名字删除。
        op.execute(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'tool_invocations'::regclass AND contype = 'p'
                ) THEN
                    RAISE EXCEPTION 'tool_invocations primary key is required';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'tool_invocations'::regclass
                    AND conname = 'uq_tool_invocation_call' AND contype = 'p'
                ) THEN
                    ALTER TABLE tool_invocations
                    RENAME CONSTRAINT uq_tool_invocation_call TO tool_invocations_pkey;
                ELSIF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'tool_invocations'::regclass
                    AND conname = 'uq_tool_invocation_call' AND contype = 'u'
                ) THEN
                    ALTER TABLE tool_invocations DROP CONSTRAINT uq_tool_invocation_call;
                END IF;
            END $$;
            """
        )
    else:
        # SQLite 需要重建表以删除命名约束，由 Alembic 复制现有数据与主键。
        with op.batch_alter_table("tool_invocations") as batch:
            batch.drop_constraint("uq_tool_invocation_call", type_="unique")


def downgrade() -> None:
    """恢复历史定义；复合主键在两个方向都保持有效。"""
    with op.batch_alter_table("tool_invocations") as batch:
        batch.create_unique_constraint("uq_tool_invocation_call", ["run_id", "call_id"])
