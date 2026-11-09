"""持续任务的后续输入与来源线程关联；旧 Run、审批和幂等键保持原义。"""

import sqlalchemy as sa
from alembic import op

revision = "20261103_0016"
down_revision = "20261020_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collaboration_tasks", sa.Column("input_sequence", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "task_inputs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("anchor_run_id", sa.Uuid(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("origin_metadata", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id", "workspace_id", "created_by"],
            ["collaboration_tasks.id", "collaboration_tasks.workspace_id", "collaboration_tasks.created_by"],
        ),
        sa.UniqueConstraint("workspace_id", "created_by", "idempotency_key", name="uq_task_input_key"),
        sa.UniqueConstraint("task_id", "sequence", name="uq_task_input_sequence"),
        sa.UniqueConstraint("run_id", name="uq_task_input_run"),
        sa.CheckConstraint("status IN ('PENDING','DISPATCHED','REJECTED')", name="ck_task_input_status"),
    )
    op.create_index("ix_task_inputs_pending", "task_inputs", ["status", "created_at", "id"])
    op.create_table(
        "task_threads",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("created_by", sa.Uuid(), primary_key=True),
        sa.Column("thread_key", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("anchor_run_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "workspace_id", "created_by"],
            ["collaboration_tasks.id", "collaboration_tasks.workspace_id", "collaboration_tasks.created_by"],
        ),
    )


def downgrade() -> None:
    # 已接收的消息不能因降级静默丢失；空库测试可以验证结构回退。
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM task_inputs")):
        raise RuntimeError("存在持续任务输入，请先备份并迁移历史，不能直接降级")
    op.drop_table("task_threads")
    op.drop_index("ix_task_inputs_pending", table_name="task_inputs")
    op.drop_table("task_inputs")
    op.drop_column("collaboration_tasks", "input_sequence")
