"""Skills 应用层依赖的持久化端口。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol
from uuid import UUID

from weora.modules.skills.domain.package import SkillCompatibility, SkillFile, SkillInterface, SkillWarning


@dataclass(frozen=True, slots=True)
class SkillDraftRecord:
    """当前可变草稿的包元数据快照。"""

    package_name: str
    storage_key: str
    package_size: int
    package_sha256: str
    content_sha256: str
    files: tuple[SkillFile, ...]
    compatibility: tuple[SkillCompatibility, ...]
    warnings: tuple[SkillWarning, ...]
    updated_at: datetime
    interface: SkillInterface | None = None
    uploader_name: str | None = None
    uploaded_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """应用层使用的 Skill 与可选草稿数据。"""

    id: UUID
    workspace_id: UUID
    slug: str
    display_name: str
    description: str
    status: str
    latest_published_version: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    draft: SkillDraftRecord | None = None
    uploader_name: str | None = None
    interface: SkillInterface | None = None


@dataclass(frozen=True, slots=True)
class SkillVersionRecord:
    """发布后不可变的 Skill 版本快照。"""

    id: UUID
    skill_id: UUID
    workspace_id: UUID
    version: str
    changelog: str
    storage_key: str
    package_name: str
    package_size: int
    package_sha256: str
    content_sha256: str
    files: tuple[SkillFile, ...]
    compatibility: tuple[SkillCompatibility, ...]
    warnings: tuple[SkillWarning, ...]
    published_by: UUID
    published_at: datetime
    interface: SkillInterface | None = None
    uploader_name: str | None = None


@dataclass(frozen=True, slots=True)
class SkillContribution:
    """同名包的待审提案，记录提交时的发布版本和草稿修订。"""

    id: UUID
    skill_id: UUID
    uploaded_by: UUID
    uploader_name: str | None
    package_sha256: str
    package_name: str
    created_at: datetime
    base_revision: str


@dataclass(frozen=True, slots=True)
class PublishedSkillVersionRecord:
    """供其他模块绑定的已发布 SkillVersion 稳定查询结果。"""

    version: SkillVersionRecord
    skill_slug: str
    skill_display_name: str


@dataclass(frozen=True, slots=True)
class SkillEventRecord:
    """供详情活动流展示的关键审计事件。"""

    id: UUID
    event_type: str
    actor_id: UUID
    request_id: str
    data: dict[str, object]
    occurred_at: datetime


@dataclass(slots=True)
class SkillDownload:
    """通过权限和摘要校验后的发布包下载流。"""

    filename: str
    package_sha256: str
    package_size: int
    archive: bytes


@dataclass(frozen=True, slots=True)
class SkillListPage:
    """包含不透明下一页游标的 Skill 列表结果。"""

    items: tuple[SkillRecord, ...]
    next_cursor: str | None


class SkillStore(Protocol):
    """一次 Skills 事务内需要的聚合读写操作。"""

    async def put_package(self, workspace_id: UUID, digest: str, archive: bytes) -> None: ...

    async def read_package(self, workspace_id: UUID, digest: str) -> bytes | None: ...

    async def backfill_interface(
        self,
        workspace_id: UUID,
        record: SkillDraftRecord | SkillVersionRecord,
        interface: SkillInterface,
    ) -> None: ...

    async def lock_skill_by_slug(self, workspace_id: UUID, slug: str) -> UUID | None: ...

    async def add_contribution(self, workspace_id: UUID, item: SkillContribution) -> None: ...

    async def list_contributions(self, workspace_id: UUID, skill_id: UUID) -> list[SkillContribution]: ...

    async def remove_contribution(self, workspace_id: UUID, skill_id: UUID, contribution_id: UUID) -> None: ...

    async def create_skill(
        self,
        *,
        skill: SkillRecord,
        draft: SkillDraftRecord,
        actor_id: UUID,
        request_id: str,
    ) -> None: ...

    async def list_skills(
        self,
        *,
        workspace_id: UUID,
        limit: int,
        search: str | None,
        status: str | None,
        cursor_updated_at: datetime | None,
        cursor_id: UUID | None,
    ) -> list[SkillRecord]: ...

    async def get_skill(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
        lock: bool = False,
    ) -> SkillRecord | None: ...

    async def update_metadata(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
        display_name: str,
        description: str,
        actor_id: UUID,
        request_id: str,
        occurred_at: datetime,
    ) -> SkillRecord | None: ...

    async def replace_draft(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
        draft: SkillDraftRecord,
        actor_id: UUID,
        request_id: str,
    ) -> None: ...

    async def publish_version(
        self,
        *,
        skill: SkillRecord,
        version: SkillVersionRecord,
        actor_name: str | None,
        request_id: str,
    ) -> None: ...

    async def list_versions(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
    ) -> list[SkillVersionRecord]: ...

    async def get_version(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
        version: str,
    ) -> SkillVersionRecord | None: ...

    async def list_published_versions_by_ids(
        self,
        *,
        workspace_id: UUID,
        version_ids: tuple[UUID, ...],
    ) -> list[PublishedSkillVersionRecord]: ...

    async def set_status(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
        new_status: str,
        event_type: str,
        actor_id: UUID,
        request_id: str,
        occurred_at: datetime,
    ) -> SkillRecord | None: ...

    async def add_event(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
        event_type: str,
        actor_id: UUID,
        request_id: str,
        data: dict[str, object],
        occurred_at: datetime,
    ) -> None: ...

    async def list_events(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
    ) -> list[SkillEventRecord]: ...


class SkillsUnitOfWork(Protocol):
    """保证 Skill 元数据、草稿和审计事件原子写入。"""

    store: SkillStore

    async def __aenter__(self) -> "SkillsUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


SkillsUnitOfWorkFactory = Callable[[], SkillsUnitOfWork]
