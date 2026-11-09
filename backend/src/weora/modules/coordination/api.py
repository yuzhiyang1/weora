"""主 Agent 配置和运行观测；沿用工作空间管理员权限、会话认证和 CSRF。"""

from dataclasses import asdict
from typing import Annotated, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from weora.modules.context.domain import ContextActor, ContextError, Json
from weora.modules.coordination.domain import Config, CoordinationError, RepositorySource, Rule
from weora.modules.coordination.engine import key
from weora.modules.coordination.service import CoordinationService
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.projects.domain import ProjectError

router = APIRouter(prefix="/workspaces/{workspace_id}/coordination", tags=["Super-agent"])
ReadPrincipal = Annotated[PrincipalRecord, Depends(current_principal)]
WritePrincipal = Annotated[PrincipalRecord, Depends(write_principal)]


class RuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=4000)
    enabled: bool = True


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    main_agent_id: UUID
    main_agent_version: str = Field(min_length=1, max_length=128)
    worker_agent_id: UUID
    worker_agent_version: str = Field(min_length=1, max_length=128)
    allowed_actors: list[str] = Field(min_length=1, max_length=100)
    allowed_bots: list[str] = Field(default_factory=list, max_length=100)
    rules: list[RuleInput] = Field(max_length=50)
    expected_revision: int = Field(ge=0)
    mode: Literal["off", "observe", "auto"] = "observe"
    enabled: bool = True
    paused: bool = False
    observe_unmentioned: bool = True
    max_workers: int = Field(default=3, ge=1, le=10)
    eval_timeout_seconds: int = Field(default=120, ge=30, le=600)
    debounce_ms: int = Field(default=250, ge=0, le=5000)
    main_model_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    worker_model_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    listed_unaddressed: Literal["inferred", "ignore"] = "inferred"
    unlisted_addressed: Literal["explicit", "inferred", "ignore"] = "ignore"
    unlisted_unaddressed: Literal["inferred", "ignore"] = "ignore"


class ConfigInput(PolicyInput):
    source_id: UUID
    route_key: str = Field(min_length=1, max_length=256)


class CodeConfigInput(PolicyInput):
    """复用连接中心的仓库标识，不接收 API URL、个人令牌或模型生成的身份绑定。"""

    repository_id: str = Field(min_length=1, max_length=128)
    remote_id: str = Field(min_length=1, max_length=128)
    project_id: UUID | None = None
    agent_login: str = Field(min_length=1, max_length=256)
    actors: dict[str, UUID] = Field(min_length=1, max_length=100)
    observe_issues: bool = False
    observe_pulls: bool = False
    observe_comments: bool = False
    observe_ci: bool = False


def service(request: Request) -> CoordinationService:
    result = getattr(request.app.state, "coordination_service", None)
    if result is None:
        raise HTTPException(503, detail={"code": "COORDINATION_NOT_CONFIGURED"})
    return cast(CoordinationService, result)


def actor(workspace_id: UUID, principal: PrincipalRecord) -> ContextActor:
    if principal.workspace_id != workspace_id:
        raise HTTPException(404, detail={"code": "NOT_FOUND"})
    return ContextActor(principal.user_id, workspace_id)


@router.put("/config")
async def save_config(workspace_id: UUID, payload: ConfigInput, request: Request, principal: WritePrincipal) -> Json:
    caller = actor(workspace_id, principal)
    values = payload.model_dump(
        exclude={
            "expected_revision",
            "rules",
            "source_id",
            "main_agent_id",
            "worker_agent_id",
            "allowed_actors",
            "allowed_bots",
        }
    )
    try:
        config = Config(
            id=key(str(workspace_id), str(payload.source_id), payload.route_key),
            workspace_id=str(workspace_id),
            created_by=str(caller.user_id),
            source_id=str(payload.source_id),
            main_agent_id=str(payload.main_agent_id),
            worker_agent_id=str(payload.worker_agent_id),
            rules=tuple(Rule(**item.model_dump()) for item in payload.rules),
            allowed_actors=tuple(payload.allowed_actors),
            allowed_bots=tuple(payload.allowed_bots),
            **values,
        )
        saved = await service(request).save(caller, config, expected_revision=payload.expected_revision)
        return {"config": asdict(saved)}
    except (CoordinationError, ContextError) as error:
        raise HTTPException(
            409 if error.code.endswith("CONFLICT") else 404 if error.code == "AGENT_VERSION_NOT_FOUND" else 403,
            detail={"code": error.code},
        ) from None


@router.get("")
async def list_scopes(workspace_id: UUID, request: Request, principal: ReadPrincipal) -> Json:
    caller, coordinator = actor(workspace_id, principal), service(request)
    try:
        return {"items": await coordinator.list_scopes(caller)}
    except ContextError as error:
        raise HTTPException(403, detail={"code": error.code}) from None


@router.get("/setup-options")
async def setup_options(workspace_id: UUID, request: Request, principal: ReadPrincipal) -> Json:
    caller = actor(workspace_id, principal)
    try:
        return await service(request).setup_options(caller)
    except (ContextError, CoordinationError, ProjectError) as error:
        raise HTTPException(403, detail={"code": error.code}) from None


@router.put("/code-config")
async def save_code_config(
    workspace_id: UUID, payload: CodeConfigInput, request: Request, principal: WritePrincipal
) -> Json:
    """管理员显式启用仓库观察；默认 observe，不导入启用之前的存量讨论。"""
    caller = actor(workspace_id, principal)
    source_id = uuid5(
        NAMESPACE_URL,
        f"weora:repository:{workspace_id}:{payload.project_id}:{payload.repository_id}:{payload.remote_id}",
    )
    values = payload.model_dump(
        exclude={
            "expected_revision",
            "rules",
            "main_agent_id",
            "worker_agent_id",
            "allowed_actors",
            "allowed_bots",
            "repository_id",
            "remote_id",
            "project_id",
            "agent_login",
            "actors",
            "observe_issues",
            "observe_pulls",
            "observe_comments",
            "observe_ci",
        }
    )
    config = Config(
        id=key(str(workspace_id), str(source_id)),
        workspace_id=str(workspace_id),
        created_by=str(caller.user_id),
        source_id=str(source_id),
        source_kind="code_hosting",
        route_key="repository:" + str(source_id),
        main_agent_id=str(payload.main_agent_id),
        worker_agent_id=str(payload.worker_agent_id),
        allowed_actors=tuple(payload.allowed_actors),
        allowed_bots=tuple(payload.allowed_bots),
        rules=tuple(Rule(**item.model_dump()) for item in payload.rules),
        project_id=str(payload.project_id) if payload.project_id else None,
        repository_source=RepositorySource(
            payload.repository_id,
            payload.remote_id,
            payload.agent_login,
            {login: str(user) for login, user in payload.actors.items()},
            observe_issues=payload.observe_issues,
            observe_pulls=payload.observe_pulls,
            observe_comments=payload.observe_comments,
            observe_ci=payload.observe_ci,
        ),
        **values,
    )
    try:
        saved = await service(request).save(caller, config, expected_revision=payload.expected_revision)
        return {"config": asdict(saved)}
    except (CoordinationError, ContextError, ProjectError, DeliveryError) as error:
        raise HTTPException(409 if error.code.endswith("CONFLICT") else 403, detail={"code": error.code}) from None


@router.get("/{scope_id}/records")
async def records_page(
    workspace_id: UUID,
    scope_id: str,
    request: Request,
    principal: ReadPrincipal,
    resource: Literal["threads", "decisions", "activities", "effects", "sessions", "notices"] = "threads",
    offset: int = Query(default=0, ge=0, le=10000),
) -> Json:
    """管理员按页查看已授权协调事实；日志正文仍经过来源撤权与 TTL 清理。"""
    caller, coordinator = actor(workspace_id, principal), service(request)
    try:
        return await coordinator.read_records(caller, scope_id, resource, offset)
    except (ContextError, CoordinationError) as error:
        raise HTTPException(403, detail={"code": error.code}) from None
