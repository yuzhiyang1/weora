"""部署配置到项目应用端口的桥：只接入显式登记目标，不暴露文件路径或读取任意秘密。"""

from urllib.parse import urlsplit
from uuid import UUID

from weora.bootstrap.settings import AppSettings
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.delivery.infrastructure.git import ConfiguredCredentials
from weora.modules.projects.code_sources import CodeSourceService, LegacySourceBinding, codeup_origin
from weora.modules.projects.domain import ProjectActor, ProjectError
from weora.modules.projects.service import ProjectService


class LegacyCodeSources:
    def __init__(self, settings: AppSettings, sources: CodeSourceService, projects: ProjectService) -> None:
        self.settings, self.sources, self.projects = settings, sources, projects

    def _bindings(self, actor: ProjectActor) -> list[tuple[LegacySourceBinding, str]]:
        results = []
        for configured in self.settings.hosting_connections:
            if configured.workspace_id != actor.workspace_id or not configured.organization:
                continue
            try:
                codeup_origin(configured.api_origin)
                repository_id = UUID(configured.repository_id)
            except (ValueError, ProjectError):
                continue
            clone = urlsplit(configured.clone_url)
            if clone.hostname != "codeup.aliyun.com" or not clone.path.startswith("/" + configured.organization + "/"):
                continue
            results.append(
                (
                    LegacySourceBinding(
                        configured.connection().fingerprint,
                        repository_id,
                        configured.clone_url,
                        configured.target_branches,
                        configured.api_origin,
                        configured.organization,
                        configured.platform_repository,
                    ),
                    configured.credential_ref,
                )
            )
        return results

    async def list(self, actor: ProjectActor) -> dict[str, object]:
        # 普通成员不能枚举部署级身份；也不为 GET 读取令牌文件。
        if await self.sources.role(actor) not in {"owner", "admin"}:
            return {"items": []}
        overview = await self.projects.workspace_repositories(actor)
        repos = overview["items"]
        assert isinstance(repos, list)
        return {
            "items": [
                {
                    "fingerprint": binding.fingerprint,
                    "repository_id": row["id"],
                    "repository_name": row["name"],
                    "repository_version": row["version"],
                    "organization": binding.organization,
                }
                for binding, _reference in self._bindings(actor)
                for row in repos
                if not row["code_source_id"]
                and row["status"] == "active"
                and row["project_status"] == "active"
                and binding.matches(row)
            ]
        }

    async def adopt(
        self, actor: ProjectActor, repository_id: UUID, version: int, fingerprint: str
    ) -> dict[str, object]:
        await self.sources.require_import_admin(actor)
        match = next(
            (
                (binding, reference)
                for binding, reference in self._bindings(actor)
                if binding.repository_id == repository_id and binding.fingerprint == fingerprint
            ),
            None,
        )
        if not match:
            raise ProjectError("PROJECT_CHANGED", "部署连接已变化或未登记，不接受客户端自定目标")
        binding, reference = match
        try:
            token = (
                ConfiguredCredentials(self.settings.hosting_credentials, self.settings.hosting_api_token_files)
                .get(reference)
                .api_token
            )
        except DeliveryError:
            raise ProjectError("PROJECT_SECRETS_UNAVAILABLE", "已登记的 API 身份不可用") from None
        if not token:
            raise ProjectError("PROJECT_SECRETS_UNAVAILABLE", "已登记身份不包含 MR API 令牌")
        return await self.sources.adopt_repository(actor, binding, version, token)
