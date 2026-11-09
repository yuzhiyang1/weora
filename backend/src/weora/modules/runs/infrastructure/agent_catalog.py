"""通过 Agents 的公开应用接口解析发布版本。"""

from uuid import UUID

from weora.modules.agents.application.service import AgentActor, AgentRegistryService
from weora.modules.agents.domain.errors import AgentNotFoundError, AgentWorkspaceAccessDeniedError
from weora.modules.runs.application.ports import PublishedAgentVersion, RunActor
from weora.modules.runs.domain.errors import RunVersionNotFoundError


class AgentsModuleVersionCatalog:
    """把 Agents 记录转换为运行所需的最小中立快照。"""

    def __init__(self, service: AgentRegistryService) -> None:
        self._service = service

    async def resolve(self, *, actor: RunActor, agent_id: UUID, version: str) -> PublishedAgentVersion:
        """读取发布版本无需管理权限，不将外部 Runtime 类型泄漏至运行。"""
        try:
            record = await self._service.get_version(
                actor=AgentActor(actor.user_id, actor.workspace_id, "member"),
                workspace_id=actor.workspace_id,
                agent_id=agent_id,
                version=version,
            )
        except (AgentNotFoundError, AgentWorkspaceAccessDeniedError) as exc:
            raise RunVersionNotFoundError from exc
        return PublishedAgentVersion(record.id, record.agent_id, record.version, record.display_name)
