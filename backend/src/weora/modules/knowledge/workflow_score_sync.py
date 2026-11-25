"""从团队报表分支发布固定快照，并在失败时继续提供上次成功的报表。"""

import asyncio
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.identity.infrastructure.unit_of_work import SqlAlchemyIdentityStore
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import WorkflowScoreSyncFile, WorkflowScoreSyncSource
from weora.modules.knowledge.store import KnowledgeStore, Scope
from weora.modules.knowledge.workflow_scores import WorkflowScoreLabel, parse_workflow_scores, score_digest
from weora.modules.projects.domain import ProjectError, ReportRepository, RepositoryBlob, RepositorySource
from weora.modules.projects.repository_sync import utc
from weora.modules.projects.service import ProjectService
from weora.shared.infrastructure.database import SessionFactory

BRANCH = "teamai-reports"
REPORT_FILE = re.compile(r"scores/[^/]+/([0-9]{4}-(?:0[1-9]|1[0-2]))\.jsonl")
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_CHANGED_BYTES = 32 * 1024 * 1024
MAX_FILES = 5000
MAX_FILE_LINES = 20000
SOURCE_CHANGED = "WORKFLOW_SCORE_SYNC_SOURCE_CHANGED"


class ReportsReader(Protocol):
    async def report_revision(self, source: RepositorySource) -> str | None: ...
    async def clone(self, source: RepositorySource, revision: str) -> UUID: ...
    async def index_blobs(
        self, cache_id: UUID, revision: str, paths: tuple[str, ...] = (),
    ) -> list[RepositoryBlob]: ...
    async def index_text(self, cache_id: UUID, blob: RepositoryBlob, *, max_bytes: int) -> str | None: ...
    async def discard(self, cache_id: UUID) -> None: ...


@dataclass(frozen=True, slots=True)
class SyncClaim:
    id: UUID
    workspace: UUID
    version: int
    lease: UUID
    revision: str | None
    project: UUID
    repository: UUID
    connection: int


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    blob: RepositoryBlob
    month: str
    labels: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class Snapshot:
    revision: str
    changed: list[FileSnapshot]
    removed: list[str]


def _scope_key(scope: Scope) -> str:
    return str(scope.project) if scope.project is not None else "shared"


async def _source(store: KnowledgeStore, *, lock: bool = False) -> WorkflowScoreSyncSource | None:
    query = select(WorkflowScoreSyncSource).where(
        WorkflowScoreSyncSource.workspace_id == store.scope.workspace,
        WorkflowScoreSyncSource.scope_key == _scope_key(store.scope),
    )
    # 读取侧持有共享锁，直到全部文件从同一已发布快照读出。
    return (await store.db.scalars(query.with_for_update(read=not lock))).one_or_none()


async def _binding(
    db: AsyncSession, projects: ProjectService, source: WorkflowScoreSyncSource, *, lock: bool = False,
) -> ReportRepository | None:
    return await projects.report_repository(
        db, source.workspace_id, source.source_project_id, source.repository_id,
        connection_version=source.connection_version, lock=lock,
    )


async def sync_status(
    store: KnowledgeStore, projects: ProjectService, *, available: bool,
) -> dict[str, object]:
    source = await _source(store)
    if source is None:
        return {"available": available, "status": "unconfigured", "branch": BRANCH, "version": 0}
    binding = await _binding(store.db, projects, source)
    now = datetime.now(UTC)
    status = "queued"
    if not source.enabled:
        status = "disabled"
    elif binding is None:
        status = "source_changed"
    elif not available:
        status = "unavailable"
    elif source.lease_until is not None and utc(source.lease_until) > now:
        status = "syncing"
    elif source.error_code:
        status = "failed"
    elif source.revision:
        status = "ready"
    return {
        "available": available, "status": status, "branch": BRANCH, "version": source.version,
        "repository_id": source.repository_id, "repository_name": binding.name if binding else None,
        "source_project_id": source.source_project_id,
        "last_synced_at": utc(source.synced_at).isoformat() if binding and source.synced_at else None,
        "revision": source.revision if binding else None, "error_code": source.error_code if binding else None,
    }


async def configure_sync(
    store: KnowledgeStore, projects: ProjectService, *, expected_version: int, enabled: bool,
    project: UUID | None, repository: UUID | None, available: bool,
) -> dict[str, object]:
    store.require_reviewer()
    scope, db = store.scope, store.db
    if scope.device_id is not None:
        raise KnowledgeError("KNOWLEDGE_FORBIDDEN", "自动同步需要管理员本人配置。", 403)
    if enabled and not available:
        raise KnowledgeError("WORKFLOW_SCORE_SYNC_UNAVAILABLE", "当前部署未开启仓库同步 Worker。", 409)
    if not await SqlAlchemyIdentityStore(db).lock_workspace(scope.workspace):
        raise KnowledgeError("WORKFLOW_SCORE_SYNC_SCOPE", "工作空间不可用。", 404)
    existing = await _source(store, lock=True)
    if expected_version != (existing.version if existing else 0):
        raise KnowledgeError("WORKFLOW_SCORE_SYNC_CONFLICT", "设置已变化，请刷新后重试。", 409)
    now = datetime.now(UTC)
    if not enabled:
        if existing is not None and existing.enabled:
            existing.enabled = False
            existing.version += 1
            existing.configured_by, existing.configured_at = scope.user, now
            existing.lease_id, existing.lease_until = None, None
    else:
        if project is None or repository is None or (scope.project is not None and scope.project != project):
            raise KnowledgeError("WORKFLOW_SCORE_SYNC_SCOPE", "请选择当前范围内的已连接仓库。", 422)
        binding = await projects.report_repository(db, scope.workspace, project, repository, lock=True)
        if binding is None:
            raise KnowledgeError("WORKFLOW_SCORE_SYNC_REPOSITORY", "仓库不存在或尚未通过连接检查。", 422)
        target = project, repository, binding.connection_version
        if existing is None:
            db.add(WorkflowScoreSyncSource(
                id=uuid4(), workspace_id=scope.workspace, scope_key=_scope_key(scope),
                source_project_id=project, repository_id=repository, connection_version=binding.connection_version,
                configured_by=scope.user, configured_at=now, version=1, enabled=True, next_sync_at=now,
            ))
        else:
            current = existing.source_project_id, existing.repository_id, existing.connection_version
            if target != current:
                # 变更连接后先隐藏旧快照。同内容的 Git blob 可在新来源成功发布时复用。
                existing.source_project_id, existing.repository_id, existing.connection_version = target
                existing.revision, existing.synced_at = None, None
            if target != current or not existing.enabled:
                existing.version += 1
                existing.enabled = True
                existing.configured_by, existing.configured_at = scope.user, now
                existing.lease_id, existing.lease_until = None, None
                existing.error_code, existing.next_sync_at = None, now
    await db.flush()
    return await sync_status(store, projects, available=available)


async def synced_scores(
    store: KnowledgeStore, projects: ProjectService, since: datetime, now: datetime,
) -> list[tuple[str, WorkflowScoreLabel]]:
    source = await _source(store)
    if source is None or not source.enabled or not source.revision:
        return []
    if not await _binding(store.db, projects, source, lock=True):
        return []
    model = WorkflowScoreSyncFile
    files = await store.db.scalars(select(model.labels).where(
        model.source_id == source.id, model.month.between(since.strftime("%Y-%m"), now.strftime("%Y-%m")),
    ).order_by(model.path))
    scored = []
    for payloads in files:
        for payload in payloads:
            label = WorkflowScoreLabel.model_validate(payload)
            if since <= label.scored_at <= now:
                ident = score_digest("teamai-reports", label.logical_key(), label.scored_at.isoformat())
                scored.append((ident, label))
    return scored


class WorkflowScoreSync:
    def __init__(
        self, sessions: SessionFactory, projects: ProjectService, reader: ReportsReader, *,
        timeout: int, interval: int,
    ) -> None:
        self.sessions, self.projects, self.reader = sessions, projects, reader
        self.timeout, self.interval = timeout, timedelta(seconds=interval)

    async def run_one(self) -> bool:
        claim = await self._claim()
        if claim is None:
            return False
        snapshot, code, cancelled = None, None, False
        try:
            async with asyncio.timeout(self.timeout):
                snapshot = await self._read(claim)
        except TimeoutError:
            code = "WORKFLOW_SCORE_SYNC_TIMEOUT"
        except (KnowledgeError, ProjectError) as error:
            code = error.code
        except OSError:
            code = "WORKFLOW_SCORE_SYNC_STORAGE_FAILED"
        except asyncio.CancelledError:
            code, cancelled = "WORKFLOW_SCORE_SYNC_INTERRUPTED", True
        await self._finish(claim, snapshot, code)
        if cancelled:
            raise asyncio.CancelledError
        return True

    async def _claim(self) -> SyncClaim | None:
        now, lease = datetime.now(UTC), uuid4()
        source = WorkflowScoreSyncSource
        due = (
            source.enabled.is_(True), source.next_sync_at <= now,
            or_(source.lease_until.is_(None), source.lease_until <= now),
        )
        async with self.sessions() as db, db.begin():
            row = await db.scalar(select(source).where(*due).order_by(
                source.next_sync_at, source.id,
            ).limit(1).with_for_update(skip_locked=True))
            if row is None:
                return None
            got = await db.scalar(update(source).where(source.id == row.id, *due).values(
                lease_id=lease, lease_until=now + timedelta(seconds=self.timeout + 60),
            ).execution_options(synchronize_session=False).returning(source.id))
            if got is None:
                return None
            return SyncClaim(
                row.id, row.workspace_id, row.version, lease, row.revision,
                row.source_project_id, row.repository_id, row.connection_version,
            )

    async def _read(self, claim: SyncClaim) -> Snapshot:
        async with self.sessions() as db, db.begin():
            if not await SqlAlchemyIdentityStore(db).lock_workspace(claim.workspace):
                raise ProjectError(SOURCE_CHANGED, "报表来源不可用。")
            binding = await self.projects.report_repository(
                db, claim.workspace, claim.project, claim.repository, connection_version=claim.connection,
                credentials=True,
            )
            if binding is None or binding.source is None:
                raise ProjectError(SOURCE_CHANGED, "报表仓库连接已变化。")
            known = dict((await db.execute(select(WorkflowScoreSyncFile.path, WorkflowScoreSyncFile.oid).where(
                WorkflowScoreSyncFile.source_id == claim.id,
            ))).tuples().all())

        remote = replace(binding.source, branch=BRANCH)
        revision = await self.reader.report_revision(remote)
        if revision is None:
            raise ProjectError("WORKFLOW_SCORE_SYNC_BRANCH_MISSING", "团队报表分支尚未发布。")
        if revision == claim.revision:
            return Snapshot(revision, [], [])
        cache = await self.reader.clone(remote, revision)
        try:
            return await self._files(cache, revision, known)
        finally:
            await self.reader.discard(cache)

    async def _files(self, cache: UUID, revision: str, known: dict[str, str]) -> Snapshot:
        now = datetime.now(UTC)
        oldest = (now - timedelta(days=180)).strftime("%Y-%m")
        current = now.strftime("%Y-%m")
        catalog = [
            (blob, match[1])
            for blob in await self.reader.index_blobs(cache, revision, ("scores",))
            if len(blob.path) <= 280 and (match := REPORT_FILE.fullmatch(blob.path)) and oldest <= match[1] <= current
        ]
        changes = [(blob, month) for blob, month in catalog if known.get(blob.path) != blob.oid]
        if len(catalog) > MAX_FILES or sum(blob.size for blob, _ in changes) > MAX_CHANGED_BYTES:
            raise ProjectError("WORKFLOW_SCORE_SYNC_LIMIT", "本次报表快照超过同步预算。")
        files = []
        for blob, month in changes:
            content = await self.reader.index_text(cache, blob, max_bytes=MAX_FILE_BYTES)
            if content is None:
                raise ProjectError("WORKFLOW_SCORE_SYNC_FILE", "报表文件不是支持大小内的 UTF-8 文本。")
            labels, _ = parse_workflow_scores(
                content, now, max_bytes=MAX_FILE_BYTES, max_lines=MAX_FILE_LINES, allow_empty=True,
            )
            if any(label.scored_at.strftime("%Y-%m") != month for label in labels):
                raise ProjectError("WORKFLOW_SCORE_SYNC_MONTH", "评分时间与报表文件月份不一致。")
            files.append(FileSnapshot(blob, month, [label.model_dump(mode="json") for label in labels]))
        removed = sorted(known.keys() - {blob.path for blob, _ in catalog})
        return Snapshot(revision, files, removed)

    async def _finish(self, claim: SyncClaim, snapshot: Snapshot | None, error: str | None) -> None:
        now = datetime.now(UTC)
        async with self.sessions() as db, db.begin():
            workspace_active = await SqlAlchemyIdentityStore(db).lock_workspace(claim.workspace)
            source = await db.get(WorkflowScoreSyncSource, claim.id, with_for_update=True)
            if (source is None or source.workspace_id != claim.workspace or source.version != claim.version
                    or source.lease_id != claim.lease or source.lease_until is None
                    or utc(source.lease_until) <= now or not source.enabled):
                return
            source.lease_id, source.lease_until = None, None
            source.next_sync_at = now + self.interval
            if not workspace_active or not await _binding(db, self.projects, source, lock=True):
                source.error_code = SOURCE_CHANGED
                return
            source.error_code = error
            if snapshot is None:
                return
            # 仅持有当前租约的 Worker 可一次发布文件变更和分支提交号。
            model = WorkflowScoreSyncFile
            for start in range(0, len(snapshot.removed), 200):
                await db.execute(delete(model).where(
                    model.source_id == source.id, model.path.in_(snapshot.removed[start:start + 200]),
                ))
            insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
            for start in range(0, len(snapshot.changed), 100):
                statement = insert(model).values([{
                    "source_id": source.id, "path": item.blob.path,
                    "oid": item.blob.oid, "month": item.month, "labels": item.labels,
                } for item in snapshot.changed[start:start + 100]])
                await db.execute(statement.on_conflict_do_update(
                    index_elements=[model.source_id, model.path],
                    set_={"oid": statement.excluded.oid, "month": statement.excluded.month,
                          "labels": statement.excluded.labels},
                ))
            source.revision, source.synced_at = snapshot.revision, now
