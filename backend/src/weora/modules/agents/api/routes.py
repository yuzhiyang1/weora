"""Agent Registry HTTP 路由。"""

from typing import Annotated, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from weora.modules.agents.api.schemas import (
    AgentDetailResponse,
    AgentDraftResponse,
    AgentListResponse,
    AgentSummaryResponse,
    AgentVersionListResponse,
    AgentVersionResponse,
    CreateAgentRequest,
    ErrorResponse,
    PublishAgentRequest,
    UpdateAgentDraftRequest,
)
from weora.modules.agents.application.service import AgentActor, AgentRegistryService
from weora.modules.agents.domain.errors import (
    AgentNotFoundError,
    AgentPermissionDeniedError,
    AgentRegistryError,
    AgentSkillVersionInvalidError,
    AgentVersionConflictError,
    AgentVersionInvalidError,
    AgentWorkspaceAccessDeniedError,
)
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.shared.api.request_id import get_request_id

router = APIRouter(prefix="/workspaces/{workspace_id}/agents", tags=["Agents"])


def _service(request: Request) -> AgentRegistryService:
    return cast(AgentRegistryService, request.app.state.agent_registry_service)


def _actor(principal: PrincipalRecord) -> AgentActor:
    return AgentActor(
        user_id=principal.user_id,
        workspace_id=principal.workspace_id,
        role=principal.role,
    )


def _raise_http_error(error: AgentRegistryError) -> NoReturn:
    """把稳定领域错误映射为不泄漏内部信息的 HTTP 状态。"""
    if isinstance(error, (AgentWorkspaceAccessDeniedError, AgentNotFoundError)):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(error, AgentPermissionDeniedError):
        http_status = status.HTTP_403_FORBIDDEN
    elif isinstance(error, AgentVersionConflictError):
        http_status = status.HTTP_409_CONFLICT
    elif isinstance(error, (AgentSkillVersionInvalidError, AgentVersionInvalidError)):
        http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        http_status = status.HTTP_400_BAD_REQUEST
    raise HTTPException(
        status_code=http_status,
        detail={"code": error.code, "message": error.message},
    ) from error


@router.post(
    "",
    response_model=AgentDetailResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse}},
)
async def create_agent(
    workspace_id: UUID,
    payload: CreateAgentRequest,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> AgentDetailResponse:
    """创建 Agent 资产，并绑定明确的已发布 SkillVersion。"""
    try:
        agent = await _service(request).create_agent(
            actor=_actor(principal),
            workspace_id=workspace_id,
            display_name=payload.display_name,
            description=payload.description,
            system_prompt=payload.system_prompt,
            skill_version_ids=tuple(payload.skill_version_ids),
            request_id=get_request_id() or "unknown",
        )
    except AgentRegistryError as exc:
        _raise_http_error(exc)
    return AgentDetailResponse.from_record(agent)


@router.get("", response_model=AgentListResponse)
async def list_agents(
    workspace_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> AgentListResponse:
    """列出当前工作空间中的 Agent 资产。"""
    try:
        agents = await _service(request).list_agents(
            actor=_actor(principal),
            workspace_id=workspace_id,
        )
    except AgentRegistryError as exc:
        _raise_http_error(exc)
    return AgentListResponse(items=[AgentSummaryResponse.from_record(item) for item in agents])


@router.get("/{agent_id}", response_model=AgentDetailResponse)
async def get_agent(
    workspace_id: UUID,
    agent_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> AgentDetailResponse:
    """获取 Agent 资产与当前草稿。"""
    try:
        agent = await _service(request).get_agent(
            actor=_actor(principal),
            workspace_id=workspace_id,
            agent_id=agent_id,
        )
    except AgentRegistryError as exc:
        _raise_http_error(exc)
    return AgentDetailResponse.from_record(agent)


@router.patch("/{agent_id}/draft", response_model=AgentDraftResponse)
async def update_agent_draft(
    workspace_id: UUID,
    agent_id: UUID,
    payload: UpdateAgentDraftRequest,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> AgentDraftResponse:
    """替换当前草稿，历史 AgentVersion 不受影响。"""
    try:
        draft = await _service(request).replace_draft(
            actor=_actor(principal),
            workspace_id=workspace_id,
            agent_id=agent_id,
            system_prompt=payload.system_prompt,
            skill_version_ids=tuple(payload.skill_version_ids),
            request_id=get_request_id() or "unknown",
        )
    except AgentRegistryError as exc:
        _raise_http_error(exc)
    return AgentDraftResponse.from_record(draft)


@router.post(
    "/{agent_id}/publish",
    response_model=AgentVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def publish_agent(
    workspace_id: UUID,
    agent_id: UUID,
    payload: PublishAgentRequest,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> AgentVersionResponse:
    """从当前草稿发布不可变 AgentVersion。"""
    try:
        version = await _service(request).publish(
            actor=_actor(principal),
            workspace_id=workspace_id,
            agent_id=agent_id,
            version=payload.version,
            changelog=payload.changelog,
            request_id=get_request_id() or "unknown",
        )
    except AgentRegistryError as exc:
        _raise_http_error(exc)
    return AgentVersionResponse.from_record(version)


@router.get("/{agent_id}/versions", response_model=AgentVersionListResponse)
async def list_agent_versions(
    workspace_id: UUID,
    agent_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> AgentVersionListResponse:
    """列出 Agent 的全部不可变版本。"""
    try:
        versions = await _service(request).list_versions(
            actor=_actor(principal),
            workspace_id=workspace_id,
            agent_id=agent_id,
        )
    except AgentRegistryError as exc:
        _raise_http_error(exc)
    return AgentVersionListResponse(items=[AgentVersionResponse.from_record(item) for item in versions])


@router.get("/{agent_id}/versions/{version}", response_model=AgentVersionResponse)
async def get_agent_version(
    workspace_id: UUID,
    agent_id: UUID,
    version: str,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> AgentVersionResponse:
    """读取一个不可变 AgentVersion。"""
    try:
        record = await _service(request).get_version(
            actor=_actor(principal),
            workspace_id=workspace_id,
            agent_id=agent_id,
            version=version,
        )
    except AgentRegistryError as exc:
        _raise_http_error(exc)
    return AgentVersionResponse.from_record(record)
