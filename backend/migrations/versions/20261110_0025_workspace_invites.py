"""保存一次性工作空间邀请及会话选中的工作空间。"""

import sqlalchemy as sa
from alembic import op

revision = "20261110_0025"
down_revision = "20261109_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("auth_sessions") as batch:
        batch.add_column(sa.Column("workspace_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key("fk_auth_sessions_workspace", "workspaces", ["workspace_id"], ["id"])
    op.create_table(
        "workspace_invites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_workspace_invites_workspace_created", "workspace_invites", ["workspace_id", "created_at"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM workspace_invites LIMIT 1")):
        raise RuntimeError("已有邀请记录，请保留数据库版本并部署兼容应用")
    op.drop_table("workspace_invites")
    with op.batch_alter_table("auth_sessions") as batch:
        batch.drop_constraint("fk_auth_sessions_workspace", type_="foreignkey")
        batch.drop_column("workspace_id")
