"""项目资料使用版本 CAS，仓库归属始终携带工作空间；凭据独立密文存储。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from weora.shared.infrastructure.database import Base


class ProjectModel(Base):
    __tablename__ = "projects"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text())
    architecture: Mapped[str] = mapped_column(Text())
    conventions: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectRepositoryModel(Base):
    __tablename__ = "project_repositories"
    __table_args__ = (UniqueConstraint("project_id", "git_url", name="uq_project_repository_url"),)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(120))
    purpose: Mapped[str] = mapped_column(Text())
    provider: Mapped[str] = mapped_column(String(16))
    git_url: Mapped[str] = mapped_column(String(2048))
    default_branch: Mapped[str] = mapped_column(String(120))
    technology: Mapped[str] = mapped_column(String(500))
    development_notes: Mapped[str] = mapped_column(Text())
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer())
    credential_ciphertext: Mapped[str | None] = mapped_column(Text(), nullable=True)
    credential_username: Mapped[str] = mapped_column(String(200), default="")
    connection_version: Mapped[int] = mapped_column(Integer(), default=1)
    connection_check: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)
    # 代码源负责 MR 身份，仓库仍使用自己的 Git 凭据；空值兼容旧部署连接。
    code_source_id: Mapped[UUID | None] = mapped_column(ForeignKey("code_sources.id"), nullable=True)
    platform_repository: Mapped[str] = mapped_column(String(20), default="")
    build_config: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CodeSourceModel(Base):
    """版本化的工作空间代码托管身份，不存 Git 密码，不以加密值充当 API 返回值。"""

    __tablename__ = "code_sources"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(16))
    api_origin: Mapped[str] = mapped_column(String(200))
    organization: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean())
    credential_ciphertext: Mapped[str | None] = mapped_column(Text(), nullable=True)
    version: Mapped[int] = mapped_column(Integer())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunRepositoryModel(Base):
    """首次解析提交后固定；克隆目录仅以不可猜的缓存 ID 关联，不返回宿主路径。"""

    __tablename__ = "project_run_repositories"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("project_repositories.id"), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    connection_version: Mapped[int] = mapped_column(Integer())
    revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    cache_id: Mapped[UUID | None] = mapped_column(nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RepositorySyncModel(Base):
    """后台同步的领取状态和最后发布的本地提交，按连接版本隔离。"""

    __tablename__ = "project_repository_syncs"
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("project_repositories.id"), primary_key=True)
    connection_version: Mapped[int] = mapped_column(Integer(), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    cache_id: Mapped[UUID] = mapped_column(unique=True)
    revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_sync_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_id: Mapped[UUID | None] = mapped_column(nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
