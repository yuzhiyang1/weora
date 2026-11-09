"""保存收件箱版本，供不同进程的实时连接观察提交。"""

import sqlalchemy as sa
from alembic import op

revision = "20261109_0024"
down_revision = "20261109_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_revisions",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("recipient_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("inbox_revisions")
