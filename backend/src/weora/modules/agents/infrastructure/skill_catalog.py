"""Skills 模块公开查询到 Agents 端口的适配器。"""

from uuid import UUID

from weora.modules.agents.application.ports import AgentSkillBindingRecord
from weora.modules.skills.application.service import SkillRegistryService


class SkillsModuleVersionCatalog:
    """只依赖 Skills 应用 API，不跨模块读取 ORM。"""

    def __init__(self, service: SkillRegistryService) -> None:
        self._service = service

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        version_ids: tuple[UUID, ...],
    ) -> tuple[AgentSkillBindingRecord, ...]:
        records = await self._service.resolve_published_versions(
            workspace_id=workspace_id,
            version_ids=version_ids,
        )
        return tuple(
            AgentSkillBindingRecord(
                skill_version_id=item.version.id,
                skill_id=item.version.skill_id,
                skill_slug=item.skill_slug,
                skill_display_name=item.skill_display_name,
                version=item.version.version,
                storage_key=item.version.storage_key,
                package_size=item.version.package_size,
                package_sha256=item.version.package_sha256,
                content_sha256=item.version.content_sha256,
            )
            for item in records
        )
