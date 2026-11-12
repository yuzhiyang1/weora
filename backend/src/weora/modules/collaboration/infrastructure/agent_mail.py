"""本地 Agent 的设备、原生会话地址、授权与持久化收件箱。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from weora.shared.infrastructure.database import Base


class AgentDevice(Base):
    """设备配对码仅兑换一次，平台只保存凭据摘要。"""

    __tablename__ = "collab_agent_devices"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(80))
    pairing_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentSession(Base):
    """UUID 寻址。原生 ID 与设备、运行时和配置实例共同标识会话。"""

    __tablename__ = "collab_agent_sessions"
    __table_args__ = (UniqueConstraint("device_id", "runtime", "profile", "native_id", name="uq_agent_native_session"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("collab_agent_devices.id"), index=True)
    runtime: Mapped[str] = mapped_column(String(16))
    profile: Mapped[str] = mapped_column(String(64))
    native_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(100))
    repo: Mapped[str] = mapped_column(String(120))
    branch: Mapped[str] = mapped_column(String(120))
    delivery: Mapped[str] = mapped_column(String(16))
    # 自动唤醒由本地接入能力决定，心跳过期后不能沿用旧能力。
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentContactGrant(Base):
    """对选定成员开放一条会话的直聊。"""

    __tablename__ = "collab_agent_contact_grants"

    session_id: Mapped[UUID] = mapped_column(ForeignKey("collab_agent_sessions.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)


class AgentTaskSession(Base):
    """设备上的一条会话可以由所有者显式接入多个 Task。"""

    __tablename__ = "collab_agent_task_sessions"

    task_id: Mapped[UUID] = mapped_column(ForeignKey("collaboration_tasks.id"), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("collab_agent_sessions.id"), primary_key=True, index=True)


class AgentMail(Base):
    """发件与收件状态共用一行，回复引用原消息并独立投递。"""

    __tablename__ = "collab_agent_mail"
    __table_args__ = (
        UniqueConstraint("sender_id", "send_key", name="uq_agent_mail_send"),
        UniqueConstraint("recipient_id", "claim_key", name="uq_agent_mail_claim"),
        CheckConstraint(
            "state IN ('pending','claimed','needs_confirmation','replied','acknowledged','cancelled','expired')",
            name="ck_agent_mail_state",
        ),
        Index("ix_agent_mail_inbox", "recipient_id", "state", "created_at", "id"),
        Index("ix_agent_mail_task", "task_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("collaboration_tasks.id"))
    sender_id: Mapped[UUID] = mapped_column(ForeignKey("collab_agent_sessions.id"))
    recipient_id: Mapped[UUID] = mapped_column(ForeignKey("collab_agent_sessions.id"))
    reply_to: Mapped[UUID | None] = mapped_column(ForeignKey("collab_agent_mail.id"), unique=True)
    send_key: Mapped[str] = mapped_column(String(128))
    claim_key: Mapped[str | None] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text)
    expects_reply: Mapped[bool] = mapped_column()
    wake: Mapped[bool] = mapped_column()
    # 在调用外部客户端前先记账，连接器崩溃后不重复注入同一个提示。
    wake_state: Mapped[str | None] = mapped_column(String(16))
    wake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24))
    request_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentEvidence(Base):
    """主动发布到 Task 的文本快照，不从本地会话读取历史。"""

    __tablename__ = "collab_agent_evidence"
    __table_args__ = (UniqueConstraint("session_id", "publish_key", name="uq_agent_evidence_publish"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("collaboration_tasks.id"), index=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("collab_agent_sessions.id"))
    publish_key: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(160))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
