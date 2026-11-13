"""Skills SQLAlchemy Repository 与 Unit of Work。"""

from dataclasses import asdict
from datetime import UTC, datetime
from types import TracebackType
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from weora.modules.skills.application.ports import (
    PublishedSkillVersionRecord,
    SkillContribution,
    SkillDraftRecord,
    SkillEventRecord,
    SkillRecord,
    SkillStore,
    SkillVersionRecord,
)
from weora.modules.skills.domain.errors import SkillRegistryError, SkillSlugConflictError, SkillVersionConflictError
from weora.modules.skills.domain.package import SkillCompatibility, SkillFile, SkillInterface, SkillWarning
from weora.modules.skills.infrastructure.models import (
    SkillContributionModel,
    SkillDraftModel,
    SkillEventModel,
    SkillModel,
    SkillPackageModel,
    SkillVersionModel,
)
from weora.shared.infrastructure.database import SessionFactory


def _as_utc(value: datetime) -> datetime:
    """SQLite 测试会丢失时区信息，统一恢复为 UTC 输出契约。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _current_interface(
    version: str | None,
    released: dict[str, str | None] | None,
    draft: dict[str, str | None] | None,
) -> SkillInterface | None:
    """资产列表优先使用已发布版本，待审内容只在对应包内展示。"""
    metadata = released if version else draft
    return SkillInterface(**metadata) if metadata else None


def _matches_interface(
    column: InstrumentedAttribute[dict[str, str | None] | None],
    search: str,
) -> ColumnElement[bool]:
    """支持按包内别名或简介查找 Skill。"""
    return or_(
        column["display_name"].as_string().contains(search, autoescape=True),
        column["short_description"].as_string().contains(search, autoescape=True),
    )


def _draft_from_model(model: SkillDraftModel | None) -> SkillDraftRecord | None:
    """把 JSON 快照恢复为应用层强类型草稿记录。"""
    if model is None:
        return None
    return SkillDraftRecord(
        package_name=model.package_name,
        storage_key=model.storage_key,
        package_size=model.package_size,
        package_sha256=model.package_sha256,
        content_sha256=model.content_sha256,
        files=tuple(SkillFile(**item) for item in model.manifest),
        compatibility=tuple(SkillCompatibility(**item) for item in model.compatibility_report),
        warnings=tuple(SkillWarning(**item) for item in model.warnings),
        updated_at=_as_utc(model.updated_at),
        interface=SkillInterface(**model.interface) if model.interface else None,
        uploader_name=model.uploader_name,
        uploaded_by=model.updated_by,
    )


def version_from_model(model: SkillVersionModel) -> SkillVersionRecord:
    """把不可变版本 ORM 快照转换为应用层记录。"""
    return SkillVersionRecord(
        id=model.id,
        skill_id=model.skill_id,
        workspace_id=model.workspace_id,
        version=model.version,
        changelog=model.changelog,
        storage_key=model.storage_key,
        package_name=model.package_name,
        package_size=model.package_size,
        package_sha256=model.package_sha256,
        content_sha256=model.content_sha256,
        files=tuple(SkillFile(**item) for item in model.manifest),
        compatibility=tuple(SkillCompatibility(**item) for item in model.compatibility_report),
        warnings=tuple(SkillWarning(**item) for item in model.warnings),
        published_by=model.published_by,
        published_at=_as_utc(model.published_at),
        interface=SkillInterface(**model.interface) if model.interface else None,
        uploader_name=model.uploader_name,
    )


def _skill_from_model(
    model: SkillModel,
    *,
    draft: SkillDraftRecord | None = None,
    latest_version: str | None = None,
    interface: SkillInterface | None = None,
) -> SkillRecord:
    """集中恢复 Skill 聚合，避免查询接口产生字段差异。"""
    return SkillRecord(
        id=model.id,
        workspace_id=model.workspace_id,
        slug=model.slug,
        display_name=(interface.display_name or model.display_name)
        if interface and not model.metadata_edited
        else model.display_name,
        description=(interface.short_description or model.description)
        if interface and not model.metadata_edited
        else model.description,
        status=model.status,
        latest_published_version=latest_version,
        created_by=model.created_by,
        created_at=_as_utc(model.created_at),
        updated_at=_as_utc(model.updated_at),
        draft=draft,
        uploader_name=model.uploader_name,
        interface=interface,
    )


def _draft_model(skill_id: UUID, draft: SkillDraftRecord, actor_id: UUID) -> SkillDraftModel:
    """把应用层草稿转换为数据库快照。"""
    return SkillDraftModel(
        skill_id=skill_id,
        package_name=draft.package_name,
        storage_key=draft.storage_key,
        package_size=draft.package_size,
        package_sha256=draft.package_sha256,
        content_sha256=draft.content_sha256,
        manifest=[asdict(item) for item in draft.files],
        compatibility_report=[asdict(item) for item in draft.compatibility],
        warnings=[asdict(item) for item in draft.warnings],
        updated_by=draft.uploaded_by or actor_id,
        updated_at=draft.updated_at,
        interface=asdict(draft.interface) if draft.interface else None,
        uploader_name=draft.uploader_name,
    )


class SqlAlchemySkillStore:
    """把 Skills 持久化端口映射到 SQLAlchemy ORM。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def put_package(self, workspace_id: UUID, digest: str, archive: bytes) -> None:
        """重复上传复用同一包，并发冲突不回滚外层发布事务。"""
        insert = sqlite_insert if self._session.get_bind().dialect.name == "sqlite" else postgres_insert
        statement = insert(SkillPackageModel).values(
            workspace_id=workspace_id,
            package_sha256=digest,
            archive_bytes=archive,
        )
        await self._session.execute(
            statement.on_conflict_do_nothing(
                index_elements=[SkillPackageModel.workspace_id, SkillPackageModel.package_sha256],
            )
        )

    async def read_package(self, workspace_id: UUID, digest: str) -> bytes | None:
        return (
            await self._session.scalars(
                select(SkillPackageModel.archive_bytes).where(
                    SkillPackageModel.workspace_id == workspace_id,
                    SkillPackageModel.package_sha256 == digest,
                )
            )
        ).one_or_none()

    async def lock_skill_by_slug(self, workspace_id: UUID, slug: str) -> UUID | None:
        """串行处理同名包的提交、接受及草稿更新。"""
        return (
            await self._session.scalars(
                select(SkillModel.id)
                .where(SkillModel.workspace_id == workspace_id, SkillModel.slug == slug)
                .with_for_update()
            )
        ).one_or_none()

    async def backfill_interface(
        self,
        workspace_id: UUID,
        record: SkillDraftRecord | SkillVersionRecord,
        interface: SkillInterface,
    ) -> None:
        """仅补缺失的展示快照，草稿替换后不会写到另一包。"""
        if isinstance(record, SkillVersionRecord):
            await self._session.execute(
                update(SkillVersionModel)
                .where(
                    SkillVersionModel.workspace_id == workspace_id,
                    SkillVersionModel.id == record.id,
                    SkillVersionModel.interface.is_(None),
                )
                .values(interface=asdict(interface))
            )
        else:
            workspace_skills = select(SkillModel.id).where(SkillModel.workspace_id == workspace_id)
            await self._session.execute(
                update(SkillDraftModel)
                .where(
                    SkillDraftModel.skill_id.in_(workspace_skills),
                    SkillDraftModel.package_sha256 == record.package_sha256,
                    SkillDraftModel.interface.is_(None),
                )
                .values(interface=asdict(interface))
            )

    async def add_contribution(self, workspace_id: UUID, item: SkillContribution) -> None:
        """保存候选包引用，与上传审计一同提交。"""
        self._session.add(SkillContributionModel(workspace_id=workspace_id, **asdict(item)))

    async def list_contributions(self, workspace_id: UUID, skill_id: UUID) -> list[SkillContribution]:
        """返回当前资产待审包，最新提交优先。"""
        rows = await self._session.scalars(
            select(SkillContributionModel)
            .where(SkillContributionModel.workspace_id == workspace_id, SkillContributionModel.skill_id == skill_id)
            .order_by(SkillContributionModel.created_at.desc(), SkillContributionModel.id.desc())
        )
        return [
            SkillContribution(
                row.id,
                row.skill_id,
                row.uploaded_by,
                row.uploader_name,
                row.package_sha256,
                row.package_name,
                _as_utc(row.created_at),
                row.base_revision,
            )
            for row in rows
        ]

    async def remove_contribution(self, workspace_id: UUID, skill_id: UUID, contribution_id: UUID) -> None:
        """接受或驳回时移出待审队列，结果由活动记录追溯。"""
        await self._session.execute(
            delete(SkillContributionModel).where(
                SkillContributionModel.workspace_id == workspace_id,
                SkillContributionModel.skill_id == skill_id,
                SkillContributionModel.id == contribution_id,
            )
        )

    async def create_skill(
        self,
        *,
        skill: SkillRecord,
        draft: SkillDraftRecord,
        actor_id: UUID,
        request_id: str,
    ) -> None:
        # 先持久化父记录但不提交，避免启用外键时草稿和审计先于 Skill 插入。
        self._session.add(
            SkillModel(
                id=skill.id,
                workspace_id=skill.workspace_id,
                slug=skill.slug,
                display_name=skill.display_name,
                description=skill.description,
                status=skill.status,
                latest_published_version_id=None,
                created_by=skill.created_by,
                uploader_name=skill.uploader_name,
                created_at=skill.created_at,
                updated_at=skill.updated_at,
            )
        )
        await self._session.flush()
        self._session.add_all(
            [
                _draft_model(skill.id, draft, actor_id),
                SkillEventModel(
                    id=uuid4(),
                    workspace_id=skill.workspace_id,
                    skill_id=skill.id,
                    actor_id=actor_id,
                    event_type="SKILL_CREATED",
                    request_id=request_id,
                    event_data={"content_sha256": draft.content_sha256},
                    occurred_at=skill.created_at,
                ),
            ]
        )

    async def list_skills(
        self,
        *,
        workspace_id: UUID,
        limit: int,
        search: str | None,
        status: str | None,
        cursor_updated_at: datetime | None,
        cursor_id: UUID | None,
    ) -> list[SkillRecord]:
        statement = (
            select(SkillModel, SkillVersionModel.version, SkillVersionModel.interface, SkillDraftModel.interface)
            .outerjoin(
                SkillVersionModel,
                SkillVersionModel.id == SkillModel.latest_published_version_id,
            )
            .outerjoin(SkillDraftModel, SkillDraftModel.skill_id == SkillModel.id)
        )
        conditions = [SkillModel.workspace_id == workspace_id]
        if search:
            # 搜索与展示使用同一版本的别名及简介。
            published = SkillModel.latest_published_version_id.is_not(None)
            conditions.append(
                or_(
                    SkillModel.display_name.contains(search, autoescape=True),
                    SkillModel.slug.contains(search, autoescape=True),
                    SkillModel.description.contains(search, autoescape=True),
                    and_(published, _matches_interface(SkillVersionModel.interface, search)),
                    and_(~published, _matches_interface(SkillDraftModel.interface, search)),
                )
            )
        if status:
            conditions.append(SkillModel.status == status)
        if cursor_updated_at is not None and cursor_id is not None:
            conditions.append(
                or_(
                    SkillModel.updated_at < cursor_updated_at,
                    and_(
                        SkillModel.updated_at == cursor_updated_at,
                        SkillModel.id < cursor_id,
                    ),
                )
            )
        statement = (
            statement.where(*conditions).order_by(SkillModel.updated_at.desc(), SkillModel.id.desc()).limit(limit)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            _skill_from_model(model, latest_version=version, interface=_current_interface(version, released, draft))
            for model, version, released, draft in rows
        ]

    async def get_skill(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
        lock: bool = False,
    ) -> SkillRecord | None:
        if lock:
            # 先锁资产行，再读草稿。等待并发替换后需要读到新的快照。
            locked = await self._session.scalar(
                select(SkillModel.id)
                .where(SkillModel.workspace_id == workspace_id, SkillModel.id == skill_id)
                .with_for_update()
            )
            if locked is None:
                return None
        statement = (
            select(SkillModel, SkillDraftModel, SkillVersionModel.version, SkillVersionModel.interface)
            .outerjoin(SkillDraftModel, SkillDraftModel.skill_id == SkillModel.id)
            .outerjoin(SkillVersionModel, SkillVersionModel.id == SkillModel.latest_published_version_id)
            .where(SkillModel.workspace_id == workspace_id, SkillModel.id == skill_id)
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        skill, draft, latest_version, released = row
        return _skill_from_model(
            skill,
            draft=_draft_from_model(draft),
            latest_version=latest_version,
            interface=_current_interface(latest_version, released, draft.interface if draft else None),
        )

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
    ) -> SkillRecord | None:
        skill = await self._session.scalar(
            select(SkillModel).where(
                SkillModel.workspace_id == workspace_id,
                SkillModel.id == skill_id,
            )
        )
        if skill is None:
            return None
        skill.display_name = display_name
        skill.description = description
        skill.metadata_edited = True
        skill.updated_at = occurred_at
        await self.add_event(
            workspace_id=workspace_id,
            skill_id=skill_id,
            event_type="SKILL_METADATA_UPDATED",
            actor_id=actor_id,
            request_id=request_id,
            data={},
            occurred_at=occurred_at,
        )
        current = await self.get_skill(workspace_id=workspace_id, skill_id=skill_id)
        return current

    async def replace_draft(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
        draft: SkillDraftRecord,
        actor_id: UUID,
        request_id: str,
    ) -> None:
        skill = await self._session.scalar(
            select(SkillModel).where(
                SkillModel.workspace_id == workspace_id,
                SkillModel.id == skill_id,
            )
        )
        if skill is None:
            return
        await self._session.execute(delete(SkillDraftModel).where(SkillDraftModel.skill_id == skill_id))
        self._session.add(_draft_model(skill_id, draft, actor_id))
        skill.updated_at = draft.updated_at
        await self.add_event(
            workspace_id=workspace_id,
            skill_id=skill_id,
            event_type="SKILL_DRAFT_REPLACED",
            actor_id=actor_id,
            request_id=request_id,
            data={"content_sha256": draft.content_sha256},
            occurred_at=draft.updated_at,
        )

    async def publish_version(
        self,
        *,
        skill: SkillRecord,
        version: SkillVersionRecord,
        actor_name: str | None,
        request_id: str,
    ) -> None:
        model = await self._session.scalar(
            select(SkillModel).where(
                SkillModel.workspace_id == skill.workspace_id,
                SkillModel.id == skill.id,
            )
        )
        if model is None:
            return
        # 先安排草稿删除，再加入新版本，避免后续 DELETE 触发提前 autoflush，
        # 从而保证唯一键冲突统一在 Unit of Work 的 commit 中映射为领域错误。
        await self._session.execute(delete(SkillDraftModel).where(SkillDraftModel.skill_id == skill.id))
        self._session.add(
            SkillVersionModel(
                id=version.id,
                skill_id=version.skill_id,
                workspace_id=version.workspace_id,
                version=version.version,
                changelog=version.changelog,
                storage_key=version.storage_key,
                package_name=version.package_name,
                package_size=version.package_size,
                package_sha256=version.package_sha256,
                content_sha256=version.content_sha256,
                manifest=[asdict(item) for item in version.files],
                compatibility_report=[asdict(item) for item in version.compatibility],
                warnings=[asdict(item) for item in version.warnings],
                published_by=version.published_by,
                published_at=version.published_at,
                interface=asdict(version.interface) if version.interface else None,
                uploader_name=version.uploader_name,
            )
        )
        model.latest_published_version_id = version.id
        model.updated_at = version.published_at
        await self.add_event(
            workspace_id=skill.workspace_id,
            skill_id=skill.id,
            event_type="SKILL_PUBLISHED",
            actor_id=version.published_by,
            request_id=request_id,
            # 保留发布当时的显示名，成员改名或离开后仍可辨认。
            data={
                "version": version.version,
                "content_sha256": version.content_sha256,
                "actor_name": actor_name,
            },
            occurred_at=version.published_at,
        )

    async def list_versions(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
    ) -> list[SkillVersionRecord]:
        statement = (
            select(SkillVersionModel)
            .where(
                SkillVersionModel.workspace_id == workspace_id,
                SkillVersionModel.skill_id == skill_id,
            )
            .order_by(SkillVersionModel.published_at.desc(), SkillVersionModel.id.desc())
        )
        return [version_from_model(model) for model in (await self._session.scalars(statement)).all()]

    async def get_version(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
        version: str,
    ) -> SkillVersionRecord | None:
        model = await self._session.scalar(
            select(SkillVersionModel).where(
                SkillVersionModel.workspace_id == workspace_id,
                SkillVersionModel.skill_id == skill_id,
                SkillVersionModel.version == version,
            )
        )
        return version_from_model(model) if model is not None else None

    async def list_published_versions_by_ids(
        self,
        *,
        workspace_id: UUID,
        version_ids: tuple[UUID, ...],
    ) -> list[PublishedSkillVersionRecord]:
        """通过稳定应用查询一次解析同工作空间的发布版本。"""
        if not version_ids:
            return []
        statement = (
            select(SkillVersionModel, SkillModel.slug, SkillModel.display_name)
            .join(SkillModel, SkillModel.id == SkillVersionModel.skill_id)
            .where(
                SkillVersionModel.workspace_id == workspace_id,
                SkillVersionModel.id.in_(version_ids),
            )
        )
        rows = (await self._session.execute(statement)).all()
        return [
            PublishedSkillVersionRecord(
                version=version_from_model(model),
                skill_slug=slug,
                skill_display_name=display_name,
            )
            for model, slug, display_name in rows
        ]

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
    ) -> SkillRecord | None:
        model = await self._session.scalar(
            select(SkillModel).where(
                SkillModel.workspace_id == workspace_id,
                SkillModel.id == skill_id,
            )
        )
        if model is None:
            return None
        model.status = new_status
        model.updated_at = occurred_at
        await self.add_event(
            workspace_id=workspace_id,
            skill_id=skill_id,
            event_type=event_type,
            actor_id=actor_id,
            request_id=request_id,
            data={},
            occurred_at=occurred_at,
        )
        return await self.get_skill(workspace_id=workspace_id, skill_id=skill_id)

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
    ) -> None:
        self._session.add(
            SkillEventModel(
                id=uuid4(),
                workspace_id=workspace_id,
                skill_id=skill_id,
                actor_id=actor_id,
                event_type=event_type,
                request_id=request_id,
                event_data=data,
                occurred_at=occurred_at,
            )
        )

    async def list_events(
        self,
        *,
        workspace_id: UUID,
        skill_id: UUID,
    ) -> list[SkillEventRecord]:
        statement = (
            select(SkillEventModel)
            .where(
                SkillEventModel.workspace_id == workspace_id,
                SkillEventModel.skill_id == skill_id,
            )
            .order_by(SkillEventModel.occurred_at.desc(), SkillEventModel.id.desc())
        )
        models = (await self._session.scalars(statement)).all()
        return [
            SkillEventRecord(
                id=model.id,
                event_type=model.event_type,
                actor_id=model.actor_id,
                request_id=model.request_id,
                data=model.event_data,
                occurred_at=_as_utc(model.occurred_at),
            )
            for model in models
        ]


class SqlAlchemySkillsUnitOfWork:
    """为一次 Skill Registry 用例持有独立数据库事务。"""

    store: SkillStore

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> "SqlAlchemySkillsUnitOfWork":
        self._session = self._session_factory()
        self.store = SqlAlchemySkillStore(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is not None:
            await self._session.rollback()
        await self._session.close()
        if isinstance(exc_value, IntegrityError):
            mapped = _integrity_conflict(exc_value)
            if mapped is not None:
                raise mapped from exc_value

    async def commit(self) -> None:
        """提交事务，并把同工作空间 slug 冲突转换为稳定错误。"""
        if self._session is None:
            raise RuntimeError("Unit of Work 尚未进入事务上下文")
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            mapped = _integrity_conflict(exc)
            if mapped is not None:
                raise mapped from exc
            raise


def _integrity_conflict(error: IntegrityError) -> SkillRegistryError | None:
    """flush 与 commit 共用精确约束映射，不把外键失败误报为同名资产。"""
    details = str(error.orig)
    if "uq_skill_versions_skill_version" in details or "skill_versions.skill_id, skill_versions.version" in details:
        return SkillVersionConflictError()
    if "uq_skills_workspace_slug" in details or "skills.workspace_id, skills.slug" in details:
        return SkillSlugConflictError()
    return None
