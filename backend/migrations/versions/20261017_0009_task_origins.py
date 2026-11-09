"""固定飞书任务原会话，不开放私有结果转群聊。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261017_0009"
down_revision: str | None = "20261017_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("collaboration_tasks") as batch:
        batch.add_column(sa.Column("origin_metadata", sa.JSON(), nullable=True))
        batch.drop_constraint("ck_tasks_private_console", type_="check")
        batch.create_check_constraint(
            "ck_tasks_origin_audience",
            "(origin = 'console' AND audience = 'private') OR (origin = 'feishu' AND audience = 'group')",
        )


def downgrade() -> None:
    # 不能把已有群任务伪装成私有来源；存在群任务时要求先显式归档，拒绝有损回退。
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT count(*) FROM collaboration_tasks WHERE origin='feishu'")):
        raise RuntimeError("存在飞书任务，不能有损回退来源约束")
    with op.batch_alter_table("collaboration_tasks") as batch:
        batch.drop_constraint("ck_tasks_origin_audience", type_="check")
        batch.create_check_constraint("ck_tasks_private_console", "origin = 'console' AND audience = 'private'")
        batch.drop_column("origin_metadata")
