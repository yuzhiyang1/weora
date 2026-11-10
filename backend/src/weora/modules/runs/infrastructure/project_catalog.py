"""运行通过项目应用接口冻结资料，不能直接读项目表或加载仓库凭据。"""

from uuid import UUID

from weora.modules.projects.domain import ProjectActor, ProjectError
from weora.modules.projects.service import ProjectService
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.domain.errors import RunProjectSelectionError


class ProjectsModuleCatalog:
    def __init__(self, projects: ProjectService) -> None:
        self.projects = projects

    async def snapshot(self, actor: RunActor, project_id: UUID, repository_ids: tuple[UUID, ...]) -> dict[str, object]:
        try:
            return await self.projects.snapshot(
                ProjectActor(actor.user_id, actor.workspace_id), project_id, repository_ids
            )
        except ProjectError:
            raise RunProjectSelectionError from None

    async def authorize(self, actor: RunActor, snapshot: dict[str, object]) -> bool:
        return await self.projects.authorize_snapshot(ProjectActor(actor.user_id, actor.workspace_id), snapshot)
