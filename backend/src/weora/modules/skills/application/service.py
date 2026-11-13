"""Skill Registry 用例编排。"""

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from weora.modules.skills.application.ports import (
    PublishedSkillVersionRecord,
    SkillDownload,
    SkillDraftRecord,
    SkillEventRecord,
    SkillListPage,
    SkillRecord,
    SkillStore,
    SkillsUnitOfWorkFactory,
    SkillVersionRecord,
)
from weora.modules.skills.domain.errors import (
    SkillArchivedError,
    SkillCursorInvalidError,
    SkillDraftNotReadyError,
    SkillManifestInvalidError,
    SkillNotFoundError,
    SkillObjectMissingError,
    SkillPermissionDeniedError,
    SkillStorageUnavailableError,
    SkillVersionConflictError,
    SkillVersionInvalidError,
    WorkspaceAccessDeniedError,
)
from weora.modules.skills.domain.package import SkillPackageInspector
from weora.shared.object_storage import ObjectKey, ObjectStoragePort
from weora.shared.object_storage.errors import ObjectNotFoundError

_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@dataclass(frozen=True, slots=True)
class SkillActor:
    """执行 Skill 用例所需的最小成员上下文。"""

    user_id: UUID
    workspace_id: UUID
    role: str


class SkillRegistryService:
    """在同一事务中保存 Skill 包与元数据，兼容旧对象存储版本。"""

    _WRITE_ROLES = frozenset({"owner", "admin"})

    def __init__(
        self,
        *,
        uow_factory: SkillsUnitOfWorkFactory,
        object_storage: ObjectStoragePort | None,
        package_inspector: SkillPackageInspector | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._object_storage = object_storage
        self._package_inspector = package_inspector or SkillPackageInspector()

    async def create_skill(
        self,
        *,
        actor: SkillActor,
        workspace_id: UUID,
        package_name: str,
        archive_bytes: bytes,
        request_id: str,
    ) -> SkillRecord:
        """在同一事务保存包、Skill 草稿和审计事件。"""
        self._require_workspace(actor, workspace_id)
        self._require_write_role(actor)
        inspected = self._package_inspector.inspect(archive_bytes)
        now = datetime.now(UTC)
        skill_id = uuid4()
        draft = SkillDraftRecord(
            package_name=package_name,
            storage_key="",
            package_size=len(inspected.archive),
            package_sha256=inspected.package_sha256,
            content_sha256=inspected.content_sha256,
            files=inspected.files,
            compatibility=inspected.compatibility,
            warnings=inspected.warnings,
            updated_at=now,
        )
        skill = SkillRecord(
            id=skill_id,
            workspace_id=workspace_id,
            slug=inspected.slug,
            display_name=inspected.name,
            description=inspected.description,
            status="active",
            latest_published_version=None,
            created_by=actor.user_id,
            created_at=now,
            updated_at=now,
            draft=draft,
        )
        async with self._uow_factory() as uow:
            await uow.store.put_package(workspace_id, inspected.package_sha256, inspected.archive)
            await uow.store.create_skill(
                skill=skill,
                draft=draft,
                actor_id=actor.user_id,
                request_id=request_id,
            )
            await uow.commit()
        return skill

    async def list_skills(
        self,
        *,
        actor: SkillActor,
        workspace_id: UUID,
        limit: int = 50,
        search: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
    ) -> SkillListPage:
        """使用稳定的更新时间与 ID 游标返回工作空间 Skill 摘要。"""
        self._require_workspace(actor, workspace_id)
        cursor_updated_at, cursor_id = self._decode_cursor(cursor)
        async with self._uow_factory() as uow:
            records = await uow.store.list_skills(
                workspace_id=workspace_id,
                limit=limit + 1,
                search=search.strip() if search else None,
                status=status,
                cursor_updated_at=cursor_updated_at,
                cursor_id=cursor_id,
            )
        items = tuple(records[:limit])
        next_cursor = None
        if len(records) > limit and items:
            next_cursor = self._encode_cursor(items[-1])
        return SkillListPage(items=items, next_cursor=next_cursor)

    async def get_skill(
        self,
        *,
        actor: SkillActor,
        workspace_id: UUID,
        skill_id: UUID,
    ) -> SkillRecord:
        """获取带当前草稿与最新版本摘要的 Skill 详情。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            skill = await uow.store.get_skill(workspace_id=workspace_id, skill_id=skill_id)
        if skill is None:
            raise SkillNotFoundError
        return skill

    async def update_metadata(
        self,
        *,
        actor: SkillActor,
        workspace_id: UUID,
        skill_id: UUID,
        display_name: str,
        description: str,
        request_id: str,
    ) -> SkillRecord:
        """更新工作空间展示元数据，不改动包内清单和历史版本。"""
        self._require_workspace(actor, workspace_id)
        self._require_write_role(actor)
        now = datetime.now(UTC)
        async with self._uow_factory() as uow:
            skill = await uow.store.update_metadata(
                workspace_id=workspace_id,
                skill_id=skill_id,
                display_name=display_name.strip(),
                description=description.strip(),
                actor_id=actor.user_id,
                request_id=request_id,
                occurred_at=now,
            )
            if skill is None:
                raise SkillNotFoundError
            await uow.commit()
        return skill

    async def replace_draft(
        self,
        *,
        actor: SkillActor,
        workspace_id: UUID,
        skill_id: UUID,
        package_name: str,
        archive_bytes: bytes,
        request_id: str,
    ) -> SkillDraftRecord:
        """用新标准包原子替换当前草稿，历史发布版本不受影响。"""
        self._require_workspace(actor, workspace_id)
        self._require_write_role(actor)
        inspected = self._package_inspector.inspect(archive_bytes)
        draft = SkillDraftRecord(
            package_name=package_name,
            storage_key="",
            package_size=len(inspected.archive),
            package_sha256=inspected.package_sha256,
            content_sha256=inspected.content_sha256,
            files=inspected.files,
            compatibility=inspected.compatibility,
            warnings=inspected.warnings,
            updated_at=datetime.now(UTC),
        )
        async with self._uow_factory() as uow:
            current = await uow.store.get_skill(workspace_id=workspace_id, skill_id=skill_id, lock=True)
            if current is None:
                raise SkillNotFoundError
            if current.status == "archived":
                raise SkillArchivedError
            if inspected.slug != current.slug:
                raise SkillManifestInvalidError
            await uow.store.put_package(workspace_id, inspected.package_sha256, inspected.archive)
            await uow.store.replace_draft(
                workspace_id=workspace_id,
                skill_id=skill_id,
                draft=draft,
                actor_id=actor.user_id,
                request_id=request_id,
            )
            await uow.commit()
        return draft

    async def publish(
        self,
        *,
        actor: SkillActor,
        workspace_id: UUID,
        skill_id: UUID,
        version: str,
        changelog: str,
        request_id: str,
    ) -> SkillVersionRecord:
        """锁定当前草稿并发布不可变版本。"""
        self._require_workspace(actor, workspace_id)
        self._require_write_role(actor)
        version = version.strip()
        changelog = changelog.strip()
        if not _SEMVER_PATTERN.fullmatch(version) or len(version) > 64 or not changelog:
            raise SkillVersionInvalidError
        async with self._uow_factory() as uow:
            skill = await uow.store.get_skill(workspace_id=workspace_id, skill_id=skill_id, lock=True)
            if skill is None:
                raise SkillNotFoundError
            if skill.status == "archived":
                raise SkillArchivedError
            if await uow.store.get_version(workspace_id=workspace_id, skill_id=skill_id, version=version):
                raise SkillVersionConflictError
            draft = skill.draft
            if draft is None or any(item.blocks_publish for item in draft.warnings):
                raise SkillDraftNotReadyError
            await self._read_archive(uow.store, workspace_id, draft)
            published = SkillVersionRecord(
                id=uuid4(),
                skill_id=skill_id,
                workspace_id=workspace_id,
                version=version,
                changelog=changelog,
                storage_key="",
                package_name=f"{skill.slug}-{version}.skill",
                package_size=draft.package_size,
                package_sha256=draft.package_sha256,
                content_sha256=draft.content_sha256,
                files=draft.files,
                compatibility=draft.compatibility,
                warnings=draft.warnings,
                published_by=actor.user_id,
                published_at=datetime.now(UTC),
            )
            await uow.store.publish_version(
                skill=skill,
                version=published,
                actor_id=actor.user_id,
                request_id=request_id,
            )
            await uow.commit()
        return published

    async def list_versions(
        self,
        *,
        actor: SkillActor,
        workspace_id: UUID,
        skill_id: UUID,
    ) -> list[SkillVersionRecord]:
        """按发布时间倒序返回全部不可变版本。"""
        await self.get_skill(actor=actor, workspace_id=workspace_id, skill_id=skill_id)
        async with self._uow_factory() as uow:
            return await uow.store.list_versions(workspace_id=workspace_id, skill_id=skill_id)

    async def get_version(
        self,
        *,
        actor: SkillActor,
        workspace_id: UUID,
        skill_id: UUID,
        version: str,
    ) -> SkillVersionRecord:
        """获取工作空间内的指定发布版本。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            record = await uow.store.get_version(
                workspace_id=workspace_id,
                skill_id=skill_id,
                version=version,
            )
        if record is None:
            raise SkillNotFoundError
        return record

    async def resolve_published_versions(
        self,
        *,
        workspace_id: UUID,
        version_ids: tuple[UUID, ...],
    ) -> list[PublishedSkillVersionRecord]:
        """向其他业务模块提供限定工作空间的发布版本查询，不暴露 ORM。"""
        async with self._uow_factory() as uow:
            return await uow.store.list_published_versions_by_ids(
                workspace_id=workspace_id,
                version_ids=version_ids,
            )

    async def download_version(
        self,
        *,
        actor: SkillActor,
        workspace_id: UUID,
        skill_id: UUID,
        version: str,
        request_id: str,
    ) -> SkillDownload:
        """校验发布记录和对象摘要后返回受控下载流，并记录审计事件。"""
        record = await self.get_version(
            actor=actor,
            workspace_id=workspace_id,
            skill_id=skill_id,
            version=version,
        )
        return await self.download_record(actor, record, request_id)

    async def download_record(
        self,
        actor: SkillActor,
        record: SkillVersionRecord,
        request_id: str,
    ) -> SkillDownload:
        """CLI 和控制台使用同一套固定版本下载和审计。"""
        self._require_workspace(actor, record.workspace_id)
        async with self._uow_factory() as uow:
            archive = await self._read_archive(uow.store, record.workspace_id, record)
            await uow.store.add_event(
                workspace_id=record.workspace_id,
                skill_id=record.skill_id,
                event_type="SKILL_DOWNLOAD_REQUESTED",
                actor_id=actor.user_id,
                request_id=request_id,
                data={"version": record.version, "content_sha256": record.content_sha256},
                occurred_at=datetime.now(UTC),
            )
            await uow.commit()
        return SkillDownload(
            filename=record.package_name,
            package_sha256=record.package_sha256,
            package_size=record.package_size,
            archive=archive,
        )

    async def read_version_archive(self, workspace_id: UUID, version_id: UUID) -> bytes:
        """为历史 Agent 绑定读取精确版本，并在首次验证后回填旧包。"""
        async with self._uow_factory() as uow:
            versions = await uow.store.list_published_versions_by_ids(
                workspace_id=workspace_id,
                version_ids=(version_id,),
            )
            if not versions:
                raise SkillNotFoundError
            archive = await self._read_archive(uow.store, workspace_id, versions[0].version)
            await uow.commit()
        return archive

    async def backfill_draft_archive(self, workspace_id: UUID, skill_id: UUID) -> None:
        """发布或替换草稿期间，回填只读取加锁后仍存在的草稿。"""
        async with self._uow_factory() as uow:
            skill = await uow.store.get_skill(workspace_id=workspace_id, skill_id=skill_id, lock=True)
            if skill is not None and skill.draft is not None:
                await self._read_archive(uow.store, workspace_id, skill.draft)
            await uow.commit()

    async def _read_archive(
        self,
        store: SkillStore,
        workspace_id: UUID,
        record: SkillDraftRecord | SkillVersionRecord,
    ) -> bytes:
        archive = await store.read_package(workspace_id, record.package_sha256)
        legacy = archive is None and bool(record.storage_key)
        if legacy:
            # 旧版本仍可从原 OSS 键恢复，回填前必须校验原始字节。
            try:
                source = await self._require_storage().read(ObjectKey(record.storage_key))
            except ObjectNotFoundError as error:
                raise SkillObjectMissingError from error
            chunks = bytearray()
            try:
                async for chunk in source:
                    chunks.extend(chunk)
                    if len(chunks) > record.package_size:
                        raise SkillObjectMissingError
                archive = bytes(chunks)
            finally:
                await source.aclose()
        if archive is None or len(archive) != record.package_size:
            raise SkillObjectMissingError
        if hashlib.sha256(archive).hexdigest() != record.package_sha256:
            raise SkillObjectMissingError
        if legacy:
            await store.put_package(workspace_id, record.package_sha256, archive)
        return archive

    async def set_archived(
        self,
        *,
        actor: SkillActor,
        workspace_id: UUID,
        skill_id: UUID,
        archived: bool,
        request_id: str,
    ) -> SkillRecord:
        """归档或恢复 Skill；历史版本始终保留。"""
        self._require_workspace(actor, workspace_id)
        self._require_write_role(actor)
        async with self._uow_factory() as uow:
            skill = await uow.store.set_status(
                workspace_id=workspace_id,
                skill_id=skill_id,
                new_status="archived" if archived else "active",
                event_type="SKILL_ARCHIVED" if archived else "SKILL_RESTORED",
                actor_id=actor.user_id,
                request_id=request_id,
                occurred_at=datetime.now(UTC),
            )
            if skill is None:
                raise SkillNotFoundError
            await uow.commit()
        return skill

    async def list_events(
        self,
        *,
        actor: SkillActor,
        workspace_id: UUID,
        skill_id: UUID,
    ) -> list[SkillEventRecord]:
        """返回指定 Skill 的关键活动记录。"""
        await self.get_skill(actor=actor, workspace_id=workspace_id, skill_id=skill_id)
        async with self._uow_factory() as uow:
            return await uow.store.list_events(workspace_id=workspace_id, skill_id=skill_id)

    def _require_storage(self) -> ObjectStoragePort:
        if self._object_storage is None:
            raise SkillStorageUnavailableError
        return self._object_storage

    @staticmethod
    def _require_workspace(actor: SkillActor, workspace_id: UUID) -> None:
        if actor.workspace_id != workspace_id:
            raise WorkspaceAccessDeniedError

    @classmethod
    def _require_write_role(cls, actor: SkillActor) -> None:
        if actor.role not in cls._WRITE_ROLES:
            raise SkillPermissionDeniedError

    @staticmethod
    def _encode_cursor(skill: SkillRecord) -> str:
        payload = f"{skill.updated_at.astimezone(UTC).isoformat()}|{skill.id}"
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[datetime | None, UUID | None]:
        if cursor is None:
            return None, None
        try:
            padding = "=" * (-len(cursor) % 4)
            decoded = base64.b64decode(
                cursor + padding,
                altchars=b"-_",
                validate=True,
            ).decode("utf-8")
            raw_time, raw_id = decoded.split("|", maxsplit=1)
            parsed_time = datetime.fromisoformat(raw_time)
            if parsed_time.tzinfo is None:
                raise ValueError
            return parsed_time.astimezone(UTC), UUID(raw_id)
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise SkillCursorInvalidError from exc
