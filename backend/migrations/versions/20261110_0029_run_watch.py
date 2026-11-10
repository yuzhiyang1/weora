"""运行变更提交后通知控制台订阅者，序号仍以 runs 表为准。"""

from alembic import op

revision = "20261110_0029"
down_revision = "20261110_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite 使用应用层的状态回读，不需要 PostgreSQL 的通知触发器。
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE FUNCTION weora_notify_run_changed() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('weora_run_changed', NEW.id::text);
            RETURN NEW;
        END;
        $$
    """)
    # 租约心跳不改变用户可见状态，不需要唤醒订阅。
    op.execute("""
        CREATE TRIGGER weora_run_changed
        AFTER UPDATE OF status, last_sequence ON runs
        FOR EACH ROW WHEN (
            OLD.status IS DISTINCT FROM NEW.status OR
            OLD.last_sequence IS DISTINCT FROM NEW.last_sequence
        )
        EXECUTE FUNCTION weora_notify_run_changed()
    """)


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER weora_run_changed ON runs")
    op.execute("DROP FUNCTION weora_notify_run_changed()")
