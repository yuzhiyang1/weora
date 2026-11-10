"""Identity 模块的 SQLAlchemy 持久化模型。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from weora.shared.infrastructure.database import Base


class UserModel(Base):
    """平台级用户账户。"""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80))
    # 仅保存已校验的小尺寸 PNG，不允许任意 URL、SVG 或外部追踪图片。
    avatar_data_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_normalized: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkspaceModel(Base):
    """团队资产所属的工作空间。"""

    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkspaceMembershipModel(Base):
    """用户在工作空间中的成员身份。"""

    __tablename__ = "workspace_memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership"),
        Index("ix_workspace_memberships_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthSessionModel(Base):
    """可独立撤销的浏览器认证会话。"""

    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    # 旧会话为空时沿用最早的有效成员关系；每次认证仍复查成员状态。
    workspace_id: Mapped[UUID | None] = mapped_column(ForeignKey("workspaces.id"), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceInviteModel(Base):
    """一次性邀请的权威状态；链接签名由部署密钥计算，不存库。"""

    __tablename__ = "workspace_invites"
    __table_args__ = (Index("ix_workspace_invites_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthEventModel(Base):
    """认证审计事件，不保存密码或原始会话令牌。"""

    __tablename__ = "auth_events"
    __table_args__ = (Index("ix_auth_events_subject_occurred_at", "subject", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    request_id: Mapped[str] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
