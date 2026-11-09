"""Agent 资产与不可变版本 SQLAlchemy 模型。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from weora.shared.infrastructure.database import Base


class AgentModel(Base):
    """工作空间中的可管理 Agent 资产。"""

    __tablename__ = "agents"
    __table_args__ = (Index("ix_agents_workspace_updated_at", "workspace_id", "updated_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    display_name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(String(16), default="active")
    latest_published_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentDraftModel(Base):
    """一个 Agent 唯一的当前可变草稿。"""

    __tablename__ = "agent_drafts"

    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    system_prompt: Mapped[str] = mapped_column(Text())
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentDraftSkillBindingModel(Base):
    """草稿按顺序绑定的已发布 SkillVersion 快照。"""

    __tablename__ = "agent_draft_skill_bindings"
    __table_args__ = (UniqueConstraint("agent_id", "skill_version_id", name="uq_agent_draft_skill_version"),)

    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agent_drafts.agent_id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer(), primary_key=True)
    skill_version_id: Mapped[UUID] = mapped_column(ForeignKey("skill_versions.id"))
    skill_id: Mapped[UUID] = mapped_column(ForeignKey("skills.id"))
    skill_slug: Mapped[str] = mapped_column(String(64))
    skill_display_name: Mapped[str] = mapped_column(String(120))
    version: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(1024))
    package_size: Mapped[int] = mapped_column(Integer())
    package_sha256: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))


class AgentVersionModel(Base):
    """发布后不可修改的 Agent 定义快照。"""

    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_version"),
        Index("ix_agent_versions_workspace_published_at", "workspace_id", "published_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    version: Mapped[str] = mapped_column(String(64))
    changelog: Mapped[str] = mapped_column(Text())
    display_name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text())
    system_prompt: Mapped[str] = mapped_column(Text())
    published_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentVersionSkillBindingModel(Base):
    """随 AgentVersion 一起冻结的 SkillVersion 绑定快照。"""

    __tablename__ = "agent_version_skill_bindings"
    __table_args__ = (UniqueConstraint("agent_version_id", "skill_version_id", name="uq_agent_version_skill_version"),)

    agent_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_versions.id"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer(), primary_key=True)
    skill_version_id: Mapped[UUID] = mapped_column(ForeignKey("skill_versions.id"))
    skill_id: Mapped[UUID] = mapped_column(ForeignKey("skills.id"))
    skill_slug: Mapped[str] = mapped_column(String(64))
    skill_display_name: Mapped[str] = mapped_column(String(120))
    version: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(1024))
    package_size: Mapped[int] = mapped_column(Integer())
    package_sha256: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))


class AgentEventModel(Base):
    """Agent 创建、草稿更新和发布的审计事件。"""

    __tablename__ = "agent_events"
    __table_args__ = (Index("ix_agent_events_agent_occurred_at", "agent_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str] = mapped_column(String(128))
    event_data: Mapped[dict[str, Any]] = mapped_column(JSON())
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
