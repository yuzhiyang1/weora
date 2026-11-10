"""独立保存运行标题及后台生成的领取时间。"""

import sqlalchemy as sa
from alembic import op

revision = "20261110_0027"
down_revision = "20261110_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collaboration_tasks", sa.Column("title", sa.String(200), nullable=True))
    op.add_column("collaboration_tasks", sa.Column("title_due_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_collaboration_tasks_title_due_at", "collaboration_tasks", ["title_due_at"])


def downgrade() -> None:
    op.drop_index("ix_collaboration_tasks_title_due_at", table_name="collaboration_tasks")
    op.drop_column("collaboration_tasks", "title_due_at")
    op.drop_column("collaboration_tasks", "title")
