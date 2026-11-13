"""新增 CLI 浏览器登录请求。"""

import sqlalchemy as sa
from alembic import op

revision = "20261112_0033"
down_revision = "20261112_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cli_login_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("verifier_hash", sa.String(64), nullable=False),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("denied_at", sa.DateTime(timezone=True)),
        sa.Column("device_id", sa.Uuid(), sa.ForeignKey("collab_agent_devices.id")),
    )
    op.create_index("ix_cli_login_requests_source_created", "cli_login_requests", ["source_key", "created_at"])
    op.create_index("ix_cli_login_requests_expires_at", "cli_login_requests", ["expires_at"])


def downgrade() -> None:
    op.drop_table("cli_login_requests")
