"""Skill Registry SQLAlchemy 持久化模型。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from weora.shared.infrastructure.database import Base


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
