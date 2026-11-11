"""记录运行中补充指令绑定的 Worker 领取代次。"""

import sqlalchemy as sa
from alembic import op

revision = "20261111_0030"
down_revision = "20261110_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_inputs") as batch:
        batch.add_column(sa.Column("steer_fence", sa.Integer(), nullable=True))
        batch.drop_constraint("ck_task_input_status", type_="check")
        batch.create_check_constraint(
            "ck_task_input_status", "status IN ('PENDING','DISPATCHED','REJECTED','STEERING','READ')"
        )
        batch.create_index("ix_task_inputs_steering", ["anchor_run_id", "steer_fence", "status"])


def downgrade() -> None:
    op.execute("UPDATE task_inputs SET status = 'REJECTED' WHERE status IN ('STEERING','READ')")
    with op.batch_alter_table("task_inputs") as batch:
        batch.drop_index("ix_task_inputs_steering")
        batch.drop_constraint("ck_task_input_status", type_="check")
        batch.create_check_constraint("ck_task_input_status", "status IN ('PENDING','DISPATCHED','REJECTED')")
        batch.drop_column("steer_fence")
