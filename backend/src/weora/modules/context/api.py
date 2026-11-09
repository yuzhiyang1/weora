"""上下文管理与私有 Run 预览 HTTP 边界。"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextActor, ContextError, ContextRunScope, Json
from weora.modules.context.service import ContextService
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.runs.application.ports import ProjectContextCatalog, RunActor
from weora.modules.runs.domain.errors import RunProjectSelectionError

router = APIRouter(prefix="/workspaces/{workspace_id}/context", tags=["Context"])
ReadPrincipal = Annotated[PrincipalRecord, Depends(current_principal)]
WritePrincipal = Annotated[PrincipalRecord, Depends(write_principal)]
ScopeFactory = Callable[[ContextActor, UUID], Awaitable[ContextRunScope]]


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(min_length=5, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    secret: str | None = Field(default=None, min_length=1, max_length=1024, exclude=True)
    connection_id: UUID | None = None
    enabled: bool = True
    verification_token: str | None = Field(default=None, min_length=1, max_length=1024, exclude=True)


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: UUID
    chat_id: str = Field(min_length=4, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    source_id: UUID | None = None
    enabled: bool = True


class EnabledInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class BindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: UUID
    open_id: str = Field(min_length=4, max_length=128)
    user_id: UUID
    enabled: bool = True


class CollectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    source_ids: list[UUID] = Field(max_length=100)


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    start_time: datetime | None = None
    end_time: datetime | None = None
    cursor: str | None = Field(default=None, max_length=12000)
    limit: int = Field(default=30, ge=1, le=50)
    thread_id: str | None = Field(default=None, max_length=128)
    message_id: str | None = Field(default=None, max_length=128)


class RouteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    agent_id: UUID
    agent_version: str = Field(min_length=1, max_length=128)
    thread_id: str | None = Field(default=None, max_length=128)
    include_nearby: bool = False
    project_id: UUID | None = None
    repository_ids: list[UUID] = Field(default_factory=list, max_length=20)
    share_project_with_group: bool = False


def _service(request: Request) -> ContextService:
    service = getattr(request.app.state, "context_service", None)
    if service is None:
        raise HTTPException(503, detail={"code": "CONTEXT_NOT_CONFIGURED", "message": "上下文连接尚未配置"})
    return cast(ContextService, service)


def _actor(workspace_id: UUID, principal: PrincipalRecord) -> ContextActor:
    if principal.workspace_id != workspace_id:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "工作空间不存在"})
    return ContextActor(principal.user_id, principal.workspace_id)


def _raise(error: ContextError) -> NoReturn:
    code = (
        404
        if error.code == "NOT_FOUND"
        else 403
        if error.code
        in {
            "PERMISSION_DENIED",
            "SOURCE_REVOKED",
        }
        else 409
        if error.code in {"CONFLICT", "CONTEXT_EXPIRED", "CONCURRENT_UPDATE"}
        else 422
    )
    raise HTTPException(code, detail={"code": error.code, "message": error.message}) from error


async def _scope(request: Request, actor: ContextActor, run_id: UUID) -> ContextRunScope:
    factory = getattr(request.app.state, "context_scope_factory", None)
    if factory is None:
        raise HTTPException(503, detail={"code": "CONTEXT_NOT_CONFIGURED", "message": "运行上下文尚未配置"})
    return await cast(ScopeFactory, factory)(actor, run_id)


@router.get("/connections")
async def connections(workspace_id: UUID, request: Request, principal: ReadPrincipal) -> Json:
    try:
        return {"items": await _service(request).connections(_actor(workspace_id, principal))}
    except ContextError as error:
        _raise(error)


@router.post("/connections")
async def save_connection(
    workspace_id: UUID,
    payload: ConnectionInput,
    request: Request,
    principal: WritePrincipal,
) -> Json:
    try:
        return await _service(request).save_connection(
            _actor(workspace_id, principal),
            app_id=payload.app_id,
            name=payload.name,
            secret=payload.secret,
            connection_id=payload.connection_id,
            enabled=payload.enabled,
            verification_token=payload.verification_token,
        )
    except ContextError as error:
        _raise(error)


@router.post("/connections/{connection_id}/probe")
async def probe(
    workspace_id: UUID,
    connection_id: UUID,
    request: Request,
    principal: WritePrincipal,
    source_id: UUID | None = None,
) -> Json:
    try:
        return await _service(request).probe(_actor(workspace_id, principal), connection_id, source_id)
    except ContextError as error:
        _raise(error)


@router.get("/sources")
async def sources(workspace_id: UUID, request: Request, principal: ReadPrincipal, managed: bool = False) -> Json:
    service = _service(request)
    actor = _actor(workspace_id, principal)
    try:
        return {"items": await service.managed_sources(actor) if managed else await service.sources(actor)}
    except ContextError as error:
        _raise(error)


@router.post("/sources")
async def save_source(workspace_id: UUID, payload: SourceInput, request: Request, principal: WritePrincipal) -> Json:
    try:
        return await _service(request).save_source(
            _actor(workspace_id, principal),
            connection_id=payload.connection_id,
            chat_id=payload.chat_id,
            name=payload.name,
            source_id=payload.source_id,
            enabled=payload.enabled,
        )
    except ContextError as error:
        _raise(error)


@router.put("/sources/{source_id}/grants/{user_id}")
async def set_grant(
    workspace_id: UUID,
    source_id: UUID,
    user_id: UUID,
    payload: EnabledInput,
    request: Request,
    principal: WritePrincipal,
) -> Json:
    try:
        return await _service(request).set_grant(_actor(workspace_id, principal), source_id, user_id, payload.enabled)
    except ContextError as error:
        _raise(error)


@router.get("/grants")
async def grants(workspace_id: UUID, request: Request, principal: ReadPrincipal) -> Json:
    try:
        return {"items": await _service(request).grants(_actor(workspace_id, principal))}
    except ContextError as error:
        _raise(error)


@router.get("/bindings")
async def bindings(workspace_id: UUID, request: Request, principal: ReadPrincipal) -> Json:
    try:
        return {"items": await _service(request).bindings(_actor(workspace_id, principal))}
    except ContextError as error:
        _raise(error)


@router.post("/bindings")
async def bind(workspace_id: UUID, payload: BindingInput, request: Request, principal: WritePrincipal) -> Json:
    try:
        return await _service(request).bind_identity(
            _actor(workspace_id, principal), payload.connection_id, payload.open_id, payload.user_id, payload.enabled
        )
    except ContextError as error:
        _raise(error)


@router.get("/collections")
async def collections(workspace_id: UUID, request: Request, principal: ReadPrincipal) -> Json:
    try:
        return {"items": await _service(request).collections(_actor(workspace_id, principal))}
    except ContextError as error:
        _raise(error)


@router.post("/collections")
async def save_collection(
    workspace_id: UUID,
    payload: CollectionInput,
    request: Request,
    principal: WritePrincipal,
) -> Json:
    try:
        return await _service(request).save_collection(
            _actor(workspace_id, principal),
            payload.name,
            payload.source_ids,
        )
    except ContextError as error:
        _raise(error)


@router.post("/runs/{run_id}/messages")
async def read(
    workspace_id: UUID,
    run_id: UUID,
    payload: ReadInput,
    request: Request,
    principal: WritePrincipal,
) -> Json:
    try:
        scope = await _scope(request, _actor(workspace_id, principal), run_id)
        return await _service(request).read(
            scope,
            payload.source_id,
            start_time=payload.start_time,
            end_time=payload.end_time,
            cursor=payload.cursor,
            limit=payload.limit,
            thread_id=payload.thread_id,
            message_id=payload.message_id,
        )
    except ContextError as error:
        _raise(error)


@router.get("/runs/{run_id}/snapshots")
async def snapshots(workspace_id: UUID, run_id: UUID, request: Request, principal: ReadPrincipal) -> Json:
    try:
        scope = await _scope(request, _actor(workspace_id, principal), run_id)
        return {"items": await _service(request).snapshots(scope)}
    except ContextError as error:
        _raise(error)


@router.get("/runs/{run_id}/snapshots/{snapshot_id}")
async def preview(
    workspace_id: UUID,
    run_id: UUID,
    snapshot_id: UUID,
    request: Request,
    principal: ReadPrincipal,
) -> Json:
    try:
        scope = await _scope(request, _actor(workspace_id, principal), run_id)
        return await _service(request).preview(scope, snapshot_id)
    except ContextError as error:
        _raise(error)


@router.get("/channels")
async def channels(workspace_id: UUID, request: Request, principal: ReadPrincipal) -> Json:
    try:
        return await FeishuChannels(_service(request)).status(_actor(workspace_id, principal))
    except ContextError as error:
        _raise(error)


@router.get("/runs/{run_id}/channel-status")
async def run_channel_status(workspace_id: UUID, run_id: UUID, request: Request, principal: ReadPrincipal) -> Json:
    try:
        scope = await _scope(request, _actor(workspace_id, principal), run_id)
        factory = getattr(request.app.state, "channel_status_factory", None)
        if factory:
            return await cast(Callable[[ContextRunScope], Awaitable[Json]], factory)(scope)
        return await FeishuChannels(_service(request)).run_status(scope)
    except ContextError as error:
        _raise(error)


@router.get("/routes")
async def routes(workspace_id: UUID, request: Request, principal: ReadPrincipal) -> Json:
    try:
        service, actor = _service(request), _actor(workspace_id, principal)
        await service.authorize_actor(actor, manage=True)
        return {"items": await service.store.list(workspace_id, "route")}
    except ContextError as error:
        _raise(error)


@router.post("/routes")
async def save_route(workspace_id: UUID, payload: RouteInput, request: Request, principal: WritePrincipal) -> Json:
    try:
        actor = _actor(workspace_id, principal)
        await _service(request).authorize_actor(actor, manage=True)
        if payload.project_id:
            # 通过项目应用端口校验归属和活动状态，不跨模块操作项目表。
            projects = cast(ProjectContextCatalog, request.app.state.context_access.projects)
            try:
                await projects.snapshot(
                    RunActor(actor.user_id, actor.workspace_id), payload.project_id, tuple(payload.repository_ids)
                )
            except RunProjectSelectionError:
                raise ContextError("INVALID_PARAMETERS", "项目或选中仓库不可用") from None
        return await FeishuChannels(_service(request)).save_route(
            actor,
            source_id=payload.source_id,
            agent_id=payload.agent_id,
            agent_version=payload.agent_version,
            thread_id=payload.thread_id,
            include_nearby=payload.include_nearby,
            project_id=payload.project_id,
            repository_ids=tuple(payload.repository_ids),
            share_project_with_group=payload.share_project_with_group,
        )
    except ContextError as error:
        _raise(error)
