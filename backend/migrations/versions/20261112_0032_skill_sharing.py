"""将 Skill 包、安装回执和使用轮次持久化到 PostgreSQL。"""

import sqlalchemy as sa
from alembic import op

revision = "20261112_0032"
down_revision = "20261112_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_packages",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("package_sha256", sa.String(64), primary_key=True),
        sa.Column("archive_bytes", sa.LargeBinary(), nullable=False),
    )
    op.create_table(
        "skill_device_grants",
        sa.Column("device_id", sa.Uuid(), sa.ForeignKey("collab_agent_devices.id"), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("usage_enabled", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "skill_installations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("device_id", sa.Uuid(), sa.ForeignKey("collab_agent_devices.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("slot_id", sa.Uuid(), nullable=False),
        sa.Column("runtime", sa.String(48), nullable=False),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("skill_versions.id"), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_skill_installation_slot", "skill_installations", ["device_id", "slot_id", "retired_at"])
    op.create_table(
        "skill_installation_receipts",
        sa.Column("device_id", sa.Uuid(), sa.ForeignKey("collab_agent_devices.id"), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "skill_usage_turns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("device_id", sa.Uuid(), sa.ForeignKey("collab_agent_devices.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("skill_versions.id"), nullable=False),
        sa.Column("runtime", sa.String(48), nullable=False),
        sa.Column("profile_id", sa.String(64), nullable=False),
        sa.Column("session_key", sa.String(64), nullable=False),
        sa.Column("turn_key", sa.String(64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mentioned_at", sa.DateTime(timezone=True)),
        sa.Column("loaded_at", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("device_id", "runtime", "profile_id", "session_key", "turn_key", "version_id"),
    )
    op.create_index("ix_skill_usage_version_time", "skill_usage_turns", ["workspace_id", "version_id", "used_at"])


def downgrade() -> None:
    # PostgreSQL 独有的包无法由旧版读取，拒绝直接删除唯一副本。
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            """
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM skill_versions WHERE storage_key = '')
                    OR EXISTS (SELECT 1 FROM skill_drafts WHERE storage_key = '') THEN
                    RAISE EXCEPTION 'Skill packages exist only in PostgreSQL; restore legacy storage before downgrade';
                END IF;
            END $$;
            """
        )
    for table in (
        "skill_usage_turns",
        "skill_installation_receipts",
        "skill_installations",
        "skill_device_grants",
        "skill_packages",
    ):
        op.drop_table(table)
