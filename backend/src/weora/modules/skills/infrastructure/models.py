"""Skill Registry SQLAlchemy 持久化模型。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from weora.shared.infrastructure.database import Base


class SkillPackageModel(Base):
    """按工作空间和摘要保存不可变 ZIP，列表查询只读版本元数据。"""

    __tablename__ = "skill_packages"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    package_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    archive_bytes: Mapped[bytes] = mapped_column(LargeBinary)


class SkillModel(Base):
    """工作空间中的可管理 Skill 资产。"""

    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_skills_workspace_slug"),
        Index("ix_skills_workspace_updated_at", "workspace_id", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    slug: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(String(16), default="active")
    latest_published_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    # 保留首次上传时的成员名称，成员改名或退出后仍可追溯。
    uploader_name: Mapped[str | None] = mapped_column(String(120))
    metadata_edited: Mapped[bool] = mapped_column(Boolean(), server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SkillDraftModel(Base):
    """一个 Skill 唯一的当前草稿。"""

    __tablename__ = "skill_drafts"

    skill_id: Mapped[UUID] = mapped_column(ForeignKey("skills.id"), primary_key=True)
    package_name: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(1024))
    package_size: Mapped[int] = mapped_column(Integer())
    package_sha256: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON())
    compatibility_report: Mapped[list[dict[str, Any]]] = mapped_column(JSON())
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON())
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interface: Mapped[dict[str, str | None] | None] = mapped_column(JSON(none_as_null=True))
    uploader_name: Mapped[str | None] = mapped_column(String(120))


class SkillVersionModel(Base):
    """发布后不可修改的 Skill 版本快照。"""

    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_version"),
        Index("ix_skill_versions_workspace_published_at", "workspace_id", "published_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    skill_id: Mapped[UUID] = mapped_column(ForeignKey("skills.id"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    version: Mapped[str] = mapped_column(String(64))
    changelog: Mapped[str] = mapped_column(Text())
    storage_key: Mapped[str] = mapped_column(String(1024))
    package_name: Mapped[str] = mapped_column(String(255))
    package_size: Mapped[int] = mapped_column(Integer())
    package_sha256: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON())
    compatibility_report: Mapped[list[dict[str, Any]]] = mapped_column(JSON())
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON())
    published_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interface: Mapped[dict[str, str | None] | None] = mapped_column(JSON(none_as_null=True))
    uploader_name: Mapped[str | None] = mapped_column(String(120))


class SkillContributionModel(Base):
    """同名 Skill 的独立候选包，接受或驳回后从待审队列移出。"""

    __tablename__ = "skill_contributions"
    __table_args__ = (
        Index("ix_skill_contributions_workspace_skill", "workspace_id", "skill_id"),
        ForeignKeyConstraint(
            ["workspace_id", "package_sha256"],
            ["skill_packages.workspace_id", "skill_packages.package_sha256"],
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    skill_id: Mapped[UUID] = mapped_column(ForeignKey("skills.id"))
    uploaded_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    uploader_name: Mapped[str | None] = mapped_column(String(120))
    package_sha256: Mapped[str] = mapped_column(String(64))
    package_name: Mapped[str] = mapped_column(String(255))
    base_revision: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SkillEventModel(Base):
    """Skill 关键状态变化的工作空间审计事件。"""

    __tablename__ = "skill_events"
    __table_args__ = (Index("ix_skill_events_skill_occurred_at", "skill_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    skill_id: Mapped[UUID] = mapped_column(ForeignKey("skills.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str] = mapped_column(String(128))
    event_data: Mapped[dict[str, Any]] = mapped_column("metadata", JSON())
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SkillDeviceGrant(Base):
    """设备的同步授权及可由设备所有者调整的使用统计开关。"""

    __tablename__ = "skill_device_grants"

    device_id: Mapped[UUID] = mapped_column(ForeignKey("collab_agent_devices.id"), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    usage_enabled: Mapped[bool] = mapped_column(Boolean)


class SkillInstallation(Base):
    """一次安装代次，用于核验旧版本离线补报。"""

    __tablename__ = "skill_installations"
    __table_args__ = (Index("ix_skill_installation_slot", "device_id", "slot_id", "retired_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    device_id: Mapped[UUID] = mapped_column(ForeignKey("collab_agent_devices.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    slot_id: Mapped[UUID] = mapped_column()
    runtime: Mapped[str] = mapped_column(String(48))
    version_id: Mapped[UUID] = mapped_column(ForeignKey("skill_versions.id"))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SkillInstallationReceipt(Base):
    """设备可安全重试已提交的操作，旧回执不能改写当前代次。"""

    __tablename__ = "skill_installation_receipts"

    device_id: Mapped[UUID] = mapped_column(ForeignKey("collab_agent_devices.id"), primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SkillUsageTurn(Base):
    """同一设备和工具的一轮中，每个版本只保留一条使用记录。"""

    __tablename__ = "skill_usage_turns"
    __table_args__ = (
        UniqueConstraint("device_id", "runtime", "profile_id", "session_key", "turn_key", "version_id"),
        Index("ix_skill_usage_version_time", "workspace_id", "version_id", "used_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    device_id: Mapped[UUID] = mapped_column(ForeignKey("collab_agent_devices.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    version_id: Mapped[UUID] = mapped_column(ForeignKey("skill_versions.id"))
    runtime: Mapped[str] = mapped_column(String(48))
    profile_id: Mapped[str] = mapped_column(String(64))
    session_key: Mapped[str] = mapped_column(String(64))
    turn_key: Mapped[str] = mapped_column(String(64))
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mentioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
