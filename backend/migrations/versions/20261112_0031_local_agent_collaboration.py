"""持久化本地 Agent 协作的设备、会话、授权、消息和证据。"""

import sqlalchemy as sa
from alembic import op

revision = "20261112_0031"
down_revision = "20261111_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collab_agent_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("pairing_hash", sa.String(length=64), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pairing_hash"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_table(
        "collab_agent_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("runtime", sa.String(length=16), nullable=False),
        sa.Column("profile", sa.String(length=64), nullable=False),
        sa.Column("native_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("repo", sa.String(length=120), nullable=False),
        sa.Column("branch", sa.String(length=120), nullable=False),
        sa.Column("delivery", sa.String(length=16), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["collab_agent_devices.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id", "runtime", "profile", "native_id", name="uq_agent_native_session"),
    )
    op.create_table(
        "collab_agent_contact_grants",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["collab_agent_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("session_id", "user_id"),
    )
    op.create_table(
        "collab_agent_task_sessions",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["collab_agent_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["collaboration_tasks.id"],
        ),
        sa.PrimaryKeyConstraint("task_id", "session_id"),
    )
    op.create_table(
        "collab_agent_mail",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("sender_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("reply_to", sa.Uuid(), nullable=True),
        sa.Column("send_key", sa.String(length=128), nullable=False),
        sa.Column("claim_key", sa.String(length=128), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("expects_reply", sa.Boolean(), nullable=False),
        sa.Column("wake", sa.Boolean(), nullable=False),
        sa.Column("wake_state", sa.String(length=16), nullable=True),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending','claimed','needs_confirmation','replied','acknowledged','cancelled','expired')",
            name="ck_agent_mail_state",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["collab_agent_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["reply_to"],
            ["collab_agent_mail.id"],
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"],
            ["collab_agent_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["collaboration_tasks.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recipient_id", "claim_key", name="uq_agent_mail_claim"),
        sa.UniqueConstraint("reply_to"),
        sa.UniqueConstraint("sender_id", "send_key", name="uq_agent_mail_send"),
    )
    op.create_table(
        "collab_agent_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("publish_key", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["collab_agent_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["collaboration_tasks.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "publish_key", name="uq_agent_evidence_publish"),
    )
    op.create_index(
        op.f("ix_collab_agent_devices_workspace_id"), "collab_agent_devices", ["workspace_id"], unique=False
    )
    op.create_index(op.f("ix_collab_agent_sessions_device_id"), "collab_agent_sessions", ["device_id"], unique=False)
    op.create_index(
        op.f("ix_collab_agent_contact_grants_user_id"), "collab_agent_contact_grants", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_collab_agent_task_sessions_session_id"), "collab_agent_task_sessions", ["session_id"], unique=False
    )
    op.create_index(
        "ix_agent_mail_inbox", "collab_agent_mail", ["recipient_id", "state", "created_at", "id"], unique=False
    )
    op.create_index("ix_agent_mail_task", "collab_agent_mail", ["task_id", "created_at", "id"], unique=False)
    op.create_index(op.f("ix_collab_agent_evidence_task_id"), "collab_agent_evidence", ["task_id"], unique=False)

    # 提交后只通知目标设备，接收者再从持久化收件箱补取。
    # SQLite 测试可以创建表；消息唤醒只在 PostgreSQL 上安装通知触发器。
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE FUNCTION weora_notify_agent_mail_changed() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('weora_agent_mail_changed', s.device_id::text)
                FROM collab_agent_sessions AS s WHERE s.id = NEW.recipient_id;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER weora_agent_mail_changed
        AFTER INSERT OR UPDATE OF state ON collab_agent_mail
        FOR EACH ROW EXECUTE FUNCTION weora_notify_agent_mail_changed()
    """)


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS weora_agent_mail_changed ON collab_agent_mail")
        op.execute("DROP FUNCTION IF EXISTS weora_notify_agent_mail_changed()")
    op.drop_table("collab_agent_evidence")
    op.drop_table("collab_agent_mail")
    op.drop_table("collab_agent_task_sessions")
    op.drop_table("collab_agent_contact_grants")
    op.drop_table("collab_agent_sessions")
    op.drop_table("collab_agent_devices")
