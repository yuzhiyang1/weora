"""为独立仓库讨论登记原生任务来源；审批、受众与任务所有者约束保持。

Revision ID: 20261104_0018
Revises: 20261103_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261104_0018"
down_revision: str | None = "20261103_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """只放宽已明确建模的来源，不能把私有 Console 任务变成共享任务。"""
    with op.batch_alter_table("collaboration_tasks") as batch:
        batch.drop_constraint("ck_tasks_origin_audience", type_="check")
        batch.create_check_constraint(
            "ck_tasks_origin_audience",
            "(origin = 'console' AND audience = 'private') OR "
            "(origin IN ('feishu', 'code_hosting') AND audience = 'group')",
        )


def downgrade() -> None:
    """已有仓库任务不能改称飞书任务；显式迁移前拒绝丢失来源语义。"""
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM collaboration_tasks WHERE origin = 'code_hosting')")):
        raise RuntimeError("存在仓库来源任务，须先备份并显式迁移，不能直接回退来源约束")
    with op.batch_alter_table("collaboration_tasks") as batch:
        batch.drop_constraint("ck_tasks_origin_audience", type_="check")
        batch.create_check_constraint(
            "ck_tasks_origin_audience",
            "(origin = 'console' AND audience = 'private') OR (origin = 'feishu' AND audience = 'group')",
        )
