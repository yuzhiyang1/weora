"""建立按成员投递、独立记录已读和归档的收件箱。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op

revision = "20261109_0023"
down_revision = "20261109_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    items = op.create_table(
        "inbox_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("recipient_type", sa.String(16), nullable=False, server_default="member"),
        sa.Column("recipient_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("collaboration_tasks.id")),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id")),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("delivery_plans.id")),
        sa.Column("read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_inbox_recipient",
        "inbox_items",
        ["workspace_id", "recipient_type", "recipient_id", "archived", "created_at"],
    )
    bind = op.get_bind()
    # 迁移前已有的待审批与审核中任务也应出现在收件箱。
    metadata = sa.MetaData()
    plans = sa.Table("delivery_plans", metadata, autoload_with=bind)
    newer = plans.alias("newer_plan")
    runs = sa.Table("runs", metadata, autoload_with=bind)
    current = (
        sa.select(plans)
        .join(
            runs,
            sa.and_(
                runs.c.id == plans.c.run_id,
                runs.c.workspace_id == plans.c.workspace_id,
            ),
        )
        .where(
            plans.c.status == "PENDING",
            runs.c.status == "WAITING_APPROVAL",
            ~sa.exists(
                sa.select(newer.c.id).where(
                    newer.c.run_id == plans.c.run_id,
                    newer.c.version > plans.c.version,
                ),
            ),
        )
    )
    for row in bind.execute(current):
        bind.execute(
            items.insert().values(
                id=uuid4(),
                workspace_id=UUID(str(row.workspace_id)),
                recipient_id=UUID(str(row.created_by)),
                type="approval",
                run_id=UUID(str(row.run_id)),
                plan_id=UUID(str(row.id)),
                created_at=row.created_at,
            )
        )
    tasks = sa.Table("collaboration_tasks", metadata, autoload_with=bind)
    for row in bind.execute(
        sa.select(tasks).where(
            tasks.c.status == "in_review",
            tasks.c.collaboration_title.is_not(None),
        )
    ):
        bind.execute(
            items.insert().values(
                id=uuid4(),
                workspace_id=UUID(str(row.workspace_id)),
                recipient_id=UUID(str(row.created_by)),
                type="review",
                task_id=UUID(str(row.id)),
                created_at=datetime.now(UTC),
            )
        )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM inbox_items)")):
        raise RuntimeError("收件箱已有处理记录，请保留当前数据库版本")
    op.drop_table("inbox_items")
