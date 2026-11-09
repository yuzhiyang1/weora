"""在同一 Task 上保存共享权限和交接记录，允许不同成员发起 Run。"""

import sqlalchemy as sa
from alembic import op

revision = "20261109_0021"
down_revision = "20261107_0020"
branch_labels = None
depends_on = None

NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def upgrade() -> None:
    bind = op.get_bind()
    foreign_key = next(
        fk
        for fk in sa.inspect(bind).get_foreign_keys("runs")
        if fk["constrained_columns"] == ["task_id", "workspace_id", "created_by"]
    )
    with op.batch_alter_table("collaboration_tasks") as batch:
        batch.create_unique_constraint("uq_tasks_workspace", ["id", "workspace_id"])
        batch.add_column(sa.Column("collaboration_title", sa.String(160)))
        batch.add_column(sa.Column("collaboration_summary", sa.Text()))
        batch.add_column(sa.Column("collaboration_assignment", sa.JSON()))
        batch.add_column(sa.Column("collaboration_pending", sa.JSON()))
        batch.add_column(sa.Column("collaboration_revision", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("collaboration_accepted", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("runs", naming_convention=NAMING) as batch:
        batch.drop_constraint(foreign_key["name"] or "fk_runs_task_id_collaboration_tasks", type_="foreignkey")
        batch.create_foreign_key(
            "fk_runs_task_workspace", "collaboration_tasks", ["task_id", "workspace_id"], ["id", "workspace_id"]
        )
        batch.add_column(sa.Column("collaboration_context", sa.JSON()))
    for name, fields in (
        ("task_participants", [sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True)]),
        (
            "task_activities",
            [
                sa.Column("revision", sa.Integer(), primary_key=True),
                sa.Column("event", sa.JSON(), nullable=False),
            ],
        ),
    ):
        op.create_table(
            name,
            sa.Column("task_id", sa.Uuid(), primary_key=True),
            sa.Column("workspace_id", sa.Uuid(), primary_key=True),
            *fields,
            sa.ForeignKeyConstraint(
                ["task_id", "workspace_id"], ["collaboration_tasks.id", "collaboration_tasks.workspace_id"]
            ),
        )
    op.create_index("ix_task_participants_member", "task_participants", ["workspace_id", "user_id"])


def downgrade() -> None:
    """有协作历史时保留账本，避免回滚造成权限或审计记录丢失。"""
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM task_activities")):
        raise RuntimeError("已有任务协作记录，请保留数据库版本并部署兼容应用")
    op.drop_table("task_activities")
    op.drop_table("task_participants")
    with op.batch_alter_table("runs", naming_convention=NAMING) as batch:
        batch.drop_column("collaboration_context")
        batch.drop_constraint("fk_runs_task_workspace", type_="foreignkey")
        batch.create_foreign_key(
            "fk_runs_task_id_collaboration_tasks",
            "collaboration_tasks",
            ["task_id", "workspace_id", "created_by"],
            ["id", "workspace_id", "created_by"],
        )
    with op.batch_alter_table("collaboration_tasks") as batch:
        for field in ("title", "summary", "assignment", "pending", "revision", "accepted"):
            batch.drop_column(f"collaboration_{field}")
        batch.drop_constraint("uq_tasks_workspace", type_="unique")
