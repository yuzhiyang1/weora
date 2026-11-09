"""用户与话题隔离的短期电脑测试凭据。"""

import sqlalchemy as sa
from alembic import op

revision = "20261107_0020"
down_revision = "20261105_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "computer_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("origin", sa.String(2048), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_computer_credentials_workspace_id", "computer_credentials", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_computer_credentials_workspace_id", table_name="computer_credentials")
    op.drop_table("computer_credentials")
