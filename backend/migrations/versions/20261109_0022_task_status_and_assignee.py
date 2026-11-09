"""以 Multica 的任务状态和负责人字段替换早期协作状态。"""

from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision = "20261109_0022"
down_revision = "20261109_0021"
branch_labels = None
depends_on = None

TASKS = "collaboration_tasks"
STATUS_CHECK = "status IN ('backlog', 'todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled')"
ASSIGNEE_CHECK = (
    "(assignee_type IS NULL AND assignee_id IS NULL) OR "
    "(assignee_type IS NOT NULL AND assignee_type IN ('member', 'agent') AND assignee_id IS NOT NULL)"
)


def _table() -> sa.TableClause:
    return sa.table(
        TASKS,
        sa.column("id", sa.Uuid()),
        sa.column("status", sa.String()),
        sa.column("assignee_type", sa.String()),
        sa.column("assignee_id", sa.Uuid()),
        sa.column("assignee_name", sa.String()),
        sa.column("assignee_version", sa.String()),
        sa.column("collaboration_manager_id", sa.Uuid()),
        sa.column("collaboration_assignment", sa.JSON(none_as_null=True)),
        sa.column("collaboration_accepted", sa.Boolean()),
    )


def upgrade() -> None:
    op.add_column(TASKS, sa.Column("status", sa.String(16), nullable=False, server_default="backlog"))
    op.add_column(TASKS, sa.Column("assignee_type", sa.String(16)))
    op.add_column(TASKS, sa.Column("assignee_id", sa.Uuid()))
    op.add_column(TASKS, sa.Column("assignee_name", sa.String(120)))
    op.add_column(TASKS, sa.Column("assignee_version", sa.String(64)))
    op.add_column(TASKS, sa.Column("collaboration_manager_id", sa.Uuid()))
    bind = op.get_bind()
    tasks = _table()
    rows = bind.execute(
        sa.select(tasks.c.id, tasks.c.collaboration_assignment, tasks.c.collaboration_accepted).where(
            tasks.c.collaboration_assignment.is_not(None),
        ),
    )
    for row in rows:
        assignment = row.collaboration_assignment
        if not assignment:
            continue
        bind.execute(
            tasks.update()
            .where(tasks.c.id == row.id)
            .values(
                status="in_progress" if row.collaboration_accepted else "todo",
                assignee_type=assignment["kind"],
                assignee_id=UUID(assignment["id"]),
                assignee_name=assignment["name"],
                assignee_version=assignment.get("version"),
                collaboration_manager_id=UUID(assignment["manager_id"]) if assignment.get("manager_id") else None,
            )
        )
    with op.batch_alter_table(TASKS) as batch:
        batch.create_check_constraint("ck_tasks_status", STATUS_CHECK)
        batch.create_check_constraint("ck_tasks_assignee", ASSIGNEE_CHECK)
        batch.drop_column("collaboration_assignment")
        batch.drop_column("collaboration_accepted")


def downgrade() -> None:
    bind = op.get_bind()
    tasks = _table()
    # 旧版只能用负责人和接手标记反推状态，无法还原独立设置的状态。
    incompatible = sa.or_(
        sa.and_(tasks.c.assignee_id.is_(None), tasks.c.status != "backlog"),
        sa.and_(tasks.c.assignee_id.is_not(None), tasks.c.status.not_in(["todo", "in_progress"])),
    )
    if bind.scalar(sa.select(sa.exists().where(incompatible))):
        raise RuntimeError("存在旧版无法表示的任务状态，请保留当前数据库版本")
    op.add_column(TASKS, sa.Column("collaboration_assignment", sa.JSON()))
    op.add_column(
        TASKS,
        sa.Column("collaboration_accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    rows = bind.execute(
        sa.select(
            tasks.c.id,
            tasks.c.status,
            tasks.c.assignee_type,
            tasks.c.assignee_id,
            tasks.c.assignee_name,
            tasks.c.assignee_version,
            tasks.c.collaboration_manager_id,
        ).where(tasks.c.assignee_id.is_not(None))
    )
    for row in rows:
        assignment = {
            "kind": row.assignee_type,
            "id": str(row.assignee_id),
            "name": row.assignee_name,
            "version": row.assignee_version,
        }
        if row.collaboration_manager_id:
            assignment["manager_id"] = str(row.collaboration_manager_id)
        bind.execute(
            tasks.update()
            .where(tasks.c.id == row.id)
            .values(
                collaboration_assignment=assignment,
                collaboration_accepted=row.status == "in_progress",
            )
        )
    with op.batch_alter_table(TASKS) as batch:
        batch.drop_constraint("ck_tasks_assignee", type_="check")
        batch.drop_constraint("ck_tasks_status", type_="check")
        for field in (
            "status",
            "assignee_type",
            "assignee_id",
            "assignee_name",
            "assignee_version",
            "collaboration_manager_id",
        ):
            batch.drop_column(field)
