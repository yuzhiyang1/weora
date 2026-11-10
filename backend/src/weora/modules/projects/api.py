"""项目 HTTP 边界：活动成员可创建，创建者/管理员管理，密钥只写不读。"""

import re
from typing import Annotated, Literal, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.projects.code_sources import CodeSourceService, codeup_origin
from weora.modules.projects.domain import ProjectActor, ProjectError
from weora.modules.projects.service import ProjectService

router = APIRouter(prefix="/workspaces/{workspace_id}/projects", tags=["Projects"])
source_router = APIRouter(prefix="/workspaces/{workspace_id}/code-sources", tags=["Code sources"])


class BuildInput(BaseModel):
    """构建建议不授予命令执行权；环境只允许选择部署管理员登记的配置。"""

    model_config = ConfigDict(extra="forbid")
    profile: Literal["default", "project"] = "project"
    commands: list[list[str]] = Field(default_factory=list, max_length=8)

    @field_validator("commands")
    @classmethod
    def bounded_commands(cls, value: list[list[str]]) -> list[list[str]]:
        if (
            any(
                not argv or len(argv) > 32 or any(not arg or len(arg) > 2000 or "\x00" in arg for arg in argv)
                for argv in value
            )
            or sum(len(arg) for argv in value for arg in argv) > 8000
        ):
            raise ValueError("请填写最多八条有效的 argv 命令，不使用空参数或超长内容")
        return value


class CodeSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    provider: Literal["codeup"] = "codeup"
    api_origin: str = "https://openapi-rdc.aliyuncs.com"
    organization: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    enabled: bool = True
    api_token: SecretStr | None = None

    @field_validator("api_origin")
    @classmethod
    def official_origin(cls, value: str) -> str:
        try:
            return codeup_origin(value)
        except ProjectError:
            raise ValueError("请选择云效官方 API 地址") from None

    @field_validator("api_token")
    @classmethod
    def bounded_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value and (len(value.get_secret_value()) > 8000 or any(c.isspace() for c in value.get_secret_value())):
            raise ValueError("API 令牌格式错误")
        return value


class CodeSourceEdit(CodeSourceInput):
    version: int = Field(ge=1)


class LegacySourceInput(BaseModel):
    """只选择服务端登记映射；禁止指定秘密、文件或目标地址。"""

    model_config = ConfigDict(extra="forbid")
    repository_id: UUID
    repository_version: int = Field(ge=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class LegacySources(Protocol):
    async def list(self, actor: ProjectActor) -> dict[str, object]: ...
    async def adopt(
        self, actor: ProjectActor, repository_id: UUID, version: int, fingerprint: str
    ) -> dict[str, object]: ...


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=6000)
    architecture: str = Field(default="", max_length=6000)
    conventions: str = Field(default="", max_length=4000)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("项目名称不能为空")
        return value.strip()


class ProjectEdit(ProjectInput):
    version: int = Field(ge=1)
    status: Literal["active", "archived"] = "active"


class RepositoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=120)
    purpose: str = Field(default="", max_length=2000)
    provider: Literal["git", "codeup", "gitlab", "github"] = "git"
    git_url: str = Field(min_length=1, max_length=2048)
    default_branch: str = Field(default="main", min_length=1, max_length=120)
    technology: str = Field(default="", max_length=500)
    development_notes: str = Field(default="", max_length=4000)
    status: Literal["active", "disabled"] = "active"
    # 字段名兼容旧客户端；实际承载 Git 密码或访问令牌。未传/null 保留，空字符串清除，永不回填。
    credential_token: SecretStr | None = None
    credential_username: str = Field(default="", max_length=200, pattern=r"^[^:\r\n]*$")
    code_source_id: UUID | None = None
    platform_repository: str = Field(default="", pattern=r"^(?:[1-9][0-9]{0,19})?$")
    build_config: BuildInput | None = None

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        return ProjectInput.nonblank(value)

    @field_validator("credential_token")
    @classmethod
    def bounded_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value and len(value.get_secret_value()) > 8000:
            raise ValueError("凭据超过长度限制")
        return value

    @field_validator("default_branch")
    @classmethod
    def branch(cls, value: str) -> str:
        if (
            not re.fullmatch(r"[A-Za-z0-9_./-]{1,120}", value)
            or ".." in value
            or "//" in value
            or value.startswith(("/", "-"))
            or value.endswith(("/", ".", ".lock"))
        ):
            raise ValueError("分支格式错误")
        return value


class RepositoryEdit(RepositoryInput):
    version: int = Field(ge=1)


class CheckConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


def _actor(principal: PrincipalRecord, workspace_id: UUID) -> ProjectActor:
    if principal.workspace_id != workspace_id:
        raise HTTPException(404, detail={"code": "PROJECT_NOT_FOUND", "message": "项目不存在或无权访问"})
    return ProjectActor(principal.user_id, workspace_id)


def _service(request: Request) -> ProjectService:
    return cast(ProjectService, request.app.state.project_service)


def _error(error: ProjectError) -> HTTPException:
    code = 404 if error.code == "PROJECT_NOT_FOUND" else 403 if error.code == "PROJECT_FORBIDDEN" else 409
    if error.code in {"INVALID_GIT_URL", "PROJECT_SELECTION_INVALID", "PROJECT_CONTEXT_TOO_LARGE"}:
        code = 422
    return HTTPException(code, detail={"code": error.code, "message": error.message})


@router.get("")
async def list_projects(
    workspace_id: UUID, request: Request, principal: Annotated[PrincipalRecord, Depends(current_principal)]
) -> dict[str, object]:
    try:
        return await _service(request).list_projects(_actor(principal, workspace_id))
    except ProjectError as error:
        raise _error(error) from None


@source_router.get("")
async def list_code_sources(
    workspace_id: UUID, request: Request, principal: Annotated[PrincipalRecord, Depends(current_principal)]
) -> dict[str, object]:
    try:
        service = cast(CodeSourceService, request.app.state.code_source_service)
        return await service.list(_actor(principal, workspace_id))
    except ProjectError as error:
        raise _error(error) from None


@source_router.get("/repositories")
async def workspace_repositories(
    workspace_id: UUID, request: Request, principal: Annotated[PrincipalRecord, Depends(current_principal)]
) -> dict[str, object]:
    try:
        return await _service(request).workspace_repositories(_actor(principal, workspace_id))
    except ProjectError as error:
        raise _error(error) from None


@source_router.get("/legacy")
async def legacy_code_sources(
    workspace_id: UUID, request: Request, principal: Annotated[PrincipalRecord, Depends(current_principal)]
) -> dict[str, object]:
    try:
        return await cast(LegacySources, request.app.state.legacy_code_sources).list(_actor(principal, workspace_id))
    except ProjectError as error:
        raise _error(error) from None


@source_router.post("/legacy/import")
async def import_legacy_code_source(
    workspace_id: UUID,
    request: Request,
    payload: LegacySourceInput,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    try:
        return await cast(LegacySources, request.app.state.legacy_code_sources).adopt(
            _actor(principal, workspace_id), payload.repository_id, payload.repository_version, payload.fingerprint
        )
    except ProjectError as error:
        raise _error(error) from None


@source_router.get("/build-profiles")
async def build_profiles(
    workspace_id: UUID, request: Request, principal: Annotated[PrincipalRecord, Depends(current_principal)]
) -> dict[str, object]:
    try:
        service = _service(request)
        await service._role(_actor(principal, workspace_id))
        return {"items": [{"id": key, "name": name} for key, (name, _image) in service.build_profiles.items()]}
    except ProjectError as error:
        raise _error(error) from None


@source_router.post("", status_code=201)
async def add_code_source(
    workspace_id: UUID,
    request: Request,
    payload: CodeSourceInput,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    try:
        return await cast(CodeSourceService, request.app.state.code_source_service).save(
            _actor(principal, workspace_id),
            payload.model_dump(exclude={"api_token"}),
            token=payload.api_token.get_secret_value() if payload.api_token is not None else None,
        )
    except ProjectError as error:
        raise _error(error) from None


@source_router.put("/{source_id}")
async def edit_code_source(
    workspace_id: UUID,
    source_id: UUID,
    request: Request,
    payload: CodeSourceEdit,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    try:
        return await cast(CodeSourceService, request.app.state.code_source_service).save(
            _actor(principal, workspace_id),
            payload.model_dump(exclude={"api_token", "version"}),
            token=payload.api_token.get_secret_value() if payload.api_token is not None else None,
            source_id=source_id,
            version=payload.version,
        )
    except ProjectError as error:
        raise _error(error) from None


@router.get("/{project_id}")
async def get_project(
    workspace_id: UUID,
    project_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> dict[str, object]:
    try:
        return await _service(request).detail(_actor(principal, workspace_id), project_id)
    except ProjectError as error:
        raise _error(error) from None


@router.post("", status_code=201)
async def create_project(
    workspace_id: UUID,
    request: Request,
    payload: ProjectInput,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    try:
        return await _service(request).create(_actor(principal, workspace_id), payload.model_dump())
    except ProjectError as error:
        raise _error(error) from None


@router.put("/{project_id}")
async def edit_project(
    workspace_id: UUID,
    project_id: UUID,
    request: Request,
    payload: ProjectEdit,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    try:
        return await _service(request).edit(
            _actor(principal, workspace_id), project_id, payload.version, payload.model_dump(exclude={"version"})
        )
    except ProjectError as error:
        raise _error(error) from None


@router.post("/{project_id}/repositories", status_code=201)
async def add_repository(
    workspace_id: UUID,
    project_id: UUID,
    request: Request,
    payload: RepositoryInput,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    try:
        return await _service(request).save_repository(
            _actor(principal, workspace_id),
            project_id,
            payload.model_dump(exclude={"credential_token"}),
            token=payload.credential_token.get_secret_value() if payload.credential_token is not None else None,
        )
    except ProjectError as error:
        raise _error(error) from None


@router.put("/{project_id}/repositories/{repository_id}")
async def edit_repository(
    workspace_id: UUID,
    project_id: UUID,
    repository_id: UUID,
    request: Request,
    payload: RepositoryEdit,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    try:
        return await _service(request).save_repository(
            _actor(principal, workspace_id),
            project_id,
            payload.model_dump(exclude={"credential_token", "version"}),
            repository_id=repository_id,
            version=payload.version,
            token=payload.credential_token.get_secret_value() if payload.credential_token is not None else None,
        )
    except ProjectError as error:
        raise _error(error) from None


@router.post("/{project_id}/repositories/{repository_id}/check")
async def check_repository(
    workspace_id: UUID,
    project_id: UUID,
    repository_id: UUID,
    request: Request,
    payload: CheckConnectionInput,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    try:
        return await _service(request).check_connection(
            _actor(principal, workspace_id), project_id, repository_id, payload.version
        )
    except ProjectError as error:
        raise _error(error) from None


@router.get("/{project_id}/repositories/{repository_id}/sync")
async def repository_sync_status(
    workspace_id: UUID,
    project_id: UUID,
    repository_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> dict[str, object]:
    try:
        return await _service(request).sync_status(_actor(principal, workspace_id), project_id, repository_id)
    except ProjectError as error:
        raise _error(error) from None
