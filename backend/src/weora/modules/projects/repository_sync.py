"""同步独立于 Run，数据库只发布已经落到服务端磁盘的提交。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from weora.modules.projects.domain import ProjectError, RepositorySource
from weora.modules.projects.persistence import ProjectModel, ProjectRepositoryModel, RepositorySyncModel
from weora.shared.infrastructure.database import SessionFactory


class SyncReader(Protocol):
    async def sync(self, source: RepositorySource, cache_id: UUID) -> str: ...

    async def has_synced_revision(self, cache_id: UUID, revision: str) -> bool: ...

    async def discard(self, cache_id: UUID) -> None: ...


@dataclass(frozen=True, slots=True)
class LocalRevision:
    cache_id: UUID
    revision: str
    synced_at: datetime


def utc(value: datetime) -> datetime:
    # SQLite 的测试事务可能返回没有时区的 UTC，生产 PostgreSQL 保留时区。
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def active_connection() -> Exists:
    return (
        select(ProjectRepositoryModel.id)
        .join(ProjectModel, ProjectModel.id == ProjectRepositoryModel.project_id)
        .where(
            ProjectRepositoryModel.id == RepositorySyncModel.repository_id,
            ProjectRepositoryModel.workspace_id == RepositorySyncModel.workspace_id,
            ProjectModel.workspace_id == RepositorySyncModel.workspace_id,
            ProjectRepositoryModel.connection_version == RepositorySyncModel.connection_version,
            ProjectRepositoryModel.status == "active",
            ProjectModel.status == "active",
        )
        .exists()
    )


class RepositorySync:
    def __init__(
        self,
        sessions: SessionFactory,
        reader: SyncReader,
        source: Callable[[ProjectRepositoryModel], RepositorySource],
        *,
        timeout_seconds: int,
        interval_seconds: int,
        max_age_seconds: int,
    ) -> None:
        self.sessions, self.reader, self.source = sessions, reader, source
        self.timeout_seconds = timeout_seconds
        self.interval = timedelta(seconds=interval_seconds)
        self.max_age = timedelta(seconds=max_age_seconds)

    async def enqueue(self, session: AsyncSession, repo: ProjectRepositoryModel) -> None:
        """连接检查成功后与检查结果同事务登记；正在同步的任务继续复用。"""
        now = datetime.now(UTC)
        state = await session.get(RepositorySyncModel, (repo.id, repo.connection_version), with_for_update=True)
        if state is None:
            session.add(
                RepositorySyncModel(
                    repository_id=repo.id,
                    connection_version=repo.connection_version,
                    workspace_id=repo.workspace_id,
                    cache_id=uuid4(),
                    next_sync_at=now,
                )
            )
        elif state.lease_until is None or utc(state.lease_until) <= now:
            state.next_sync_at = now

    async def enroll_existing(self) -> None:
        """启用时接管已经校验通过的登记；凭据或连接变化后按新版本重新登记。"""
        async with self.sessions() as session:
            repo = await session.scalar(
                select(ProjectRepositoryModel)
                .join(ProjectModel, ProjectModel.id == ProjectRepositoryModel.project_id)
                .where(
                    ProjectModel.status == "active",
                    ProjectModel.workspace_id == ProjectRepositoryModel.workspace_id,
                    ProjectRepositoryModel.status == "active",
                    ProjectRepositoryModel.connection_check["status"].as_string() == "connected",
                    ~select(RepositorySyncModel.repository_id)
                    .where(
                        RepositorySyncModel.repository_id == ProjectRepositoryModel.id,
                        RepositorySyncModel.connection_version == ProjectRepositoryModel.connection_version,
                    )
                    .exists(),
                )
                .order_by(ProjectRepositoryModel.id)
                .limit(1)
            )
            if repo is not None:
                try:
                    await self.enqueue(session, repo)
                    await session.commit()
                except IntegrityError:
                    await session.rollback()  # 与人工校验并发时，联合主键保留先登记者。

    async def run_one(self) -> bool:
        await self.enroll_existing()
        now, lease = datetime.now(UTC), uuid4()
        due = (
            RepositorySyncModel.next_sync_at <= now,
            or_(RepositorySyncModel.lease_until.is_(None), RepositorySyncModel.lease_until <= now),
            active_connection(),
        )
        async with self.sessions() as session:
            selected = await session.scalar(
                select(RepositorySyncModel)
                .where(*due)
                .order_by(RepositorySyncModel.next_sync_at, RepositorySyncModel.repository_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if selected is None:
                return False
            key = (
                RepositorySyncModel.repository_id == selected.repository_id,
                RepositorySyncModel.connection_version == selected.connection_version,
                RepositorySyncModel.workspace_id == selected.workspace_id,
            )
            repo = await session.get(ProjectRepositoryModel, selected.repository_id)
            if repo is None or repo.connection_version != selected.connection_version:
                return False
            cache_id = selected.cache_id
            claimed = await session.execute(
                update(RepositorySyncModel)
                .where(*key, *due)
                .values(
                    lease_id=lease,
                    lease_until=now + timedelta(seconds=self.timeout_seconds + 60),
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                return False
            await session.commit()

        revision = None
        error_code = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                revision = await self.reader.sync(self.source(repo), cache_id)
        except TimeoutError:
            error_code = "REPOSITORY_SYNC_TIMEOUT"
        except ProjectError as error:
            error_code = error.code
        except OSError:
            error_code = "REPOSITORY_SYNC_STORAGE_FAILED"
        finished = datetime.now(UTC)
        values: dict[str, object] = {
            "lease_id": None,
            "lease_until": None,
            "error_code": error_code,
            "next_sync_at": finished + self.interval,
        }
        if revision is not None:
            values |= {"revision": revision, "synced_at": finished}
        async with self.sessions() as session:
            # 撤权、连接修改及租约回收后，旧 Worker 无权发布本次结果。
            await session.execute(
                update(RepositorySyncModel)
                .where(
                    *key,
                    RepositorySyncModel.lease_id == lease,
                    RepositorySyncModel.lease_until > finished,
                    active_connection(),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
        return True

    async def binding(
        self, workspace_id: UUID, repository_id: UUID, version: int, revision: str | None = None
    ) -> LocalRevision:
        async with self.sessions() as session:
            state = await session.get(RepositorySyncModel, (repository_id, version))
        if state is None or state.workspace_id != workspace_id:
            raise ProjectError("REPOSITORY_SYNC_PENDING", "仓库尚未完成首次同步")
        if revision is None and state.error_code:
            raise ProjectError(state.error_code, "仓库最近一次同步失败")
        if not state.revision or not state.synced_at:
            raise ProjectError("REPOSITORY_SYNC_PENDING", "仓库尚未完成首次同步")
        if revision is None and utc(state.synced_at) + self.max_age < datetime.now(UTC):
            raise ProjectError("REPOSITORY_SYNC_STALE", "仓库最近一次成功同步已超过有效期")
        fixed = revision or state.revision
        if not await self.reader.has_synced_revision(state.cache_id, fixed):
            raise ProjectError("REPOSITORY_SYNC_CACHE_MISSING", "本地仓库缺少本次需要的固定提交")
        return LocalRevision(state.cache_id, fixed, utc(state.synced_at))

    async def status(self, workspace_id: UUID, repository_id: UUID, version: int) -> dict[str, object]:
        async with self.sessions() as session:
            row = await session.get(RepositorySyncModel, (repository_id, version))
        if row is None or row.workspace_id != workspace_id:
            return {"status": "NOT_SCHEDULED"}
        if row.lease_until and utc(row.lease_until) > datetime.now(UTC):
            status = "SYNCING"
        elif row.error_code:
            status = "FAILED"
        elif row.synced_at and utc(row.synced_at) + self.max_age < datetime.now(UTC):
            status = "STALE"
        else:
            status = "READY" if row.revision else "QUEUED"
        return {
            "status": status,
            "revision": row.revision,
            "error_code": row.error_code,
            "synced_at": utc(row.synced_at).isoformat() if row.synced_at else None,
        }

    async def purge_retired(self) -> bool:
        """只清理连接版本单向升级后已失去读权限的裸库，留足在途操作的宽限期。"""
        now = datetime.now(UTC)
        async with self.sessions() as session:
            row = await session.scalar(
                select(RepositorySyncModel)
                .join(ProjectRepositoryModel, ProjectRepositoryModel.id == RepositorySyncModel.repository_id)
                .where(
                    ProjectRepositoryModel.workspace_id == RepositorySyncModel.workspace_id,
                    ProjectRepositoryModel.connection_version > RepositorySyncModel.connection_version,
                    ProjectRepositoryModel.updated_at < now - timedelta(days=7),
                    or_(RepositorySyncModel.lease_until.is_(None), RepositorySyncModel.lease_until <= now),
                )
                .order_by(RepositorySyncModel.next_sync_at)
                .limit(1)
                .with_for_update(of=RepositorySyncModel, skip_locked=True)
            )
            if row is None:
                return False
            await self.reader.discard(row.cache_id)
            await session.delete(row)
            await session.commit()
        return True
