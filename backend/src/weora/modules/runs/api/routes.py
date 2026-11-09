"""运行 API：认证、CSRF 与私有任务隔离，不在请求中调用模型。"""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Annotated, Literal, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.projects.domain import ProjectActor
from weora.modules.projects.service import ProjectService
from weora.modules.runs.api.schemas import (
    ContinueTaskRequest,
    CreateRunRequest,
    RunDetailResponse,
    RunEventResponse,
    RunEventsResponse,
    RunListResponse,
    RunSummaryResponse,
)
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.domain.errors import RunError, RunNotFoundError, RunVersionNotFoundError
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.tools.application.gateway import ToolScope
from weora.modules.tools.infrastructure.store import SqlAlchemyToolStore
from weora.shared.api.request_id import get_request_id

router = APIRouter(prefix="/workspaces/{workspace_id}/runs", tags=["Runs"])


class RecoverRequest(BaseModel):
    """仅恢复分析能力，不授予重放写操作的权限。"""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["read_only"]


@router.get("/{run_id}/repositories")
async def run_repositories(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> dict[str, object]:
    try:
        await _service(request).get(actor=_actor(principal), workspace_id=workspace_id, run_id=run_id)
    except RunError as error:
        _raise_http(error)
    service = cast(ProjectService, request.app.state.project_service)
    return {"items": await service.run_repositories(ProjectActor(principal.user_id, workspace_id), run_id)}


def _service(request: Request) -> RunService:
    return cast(RunService, request.app.state.run_service)


def _actor(principal: PrincipalRecord) -> RunActor:
    return RunActor(principal.user_id, principal.workspace_id)


def _raise_http(error: RunError) -> NoReturn:
    http_status = 404 if isinstance(error, (RunNotFoundError, RunVersionNotFoundError)) else 409
    raise HTTPException(http_status, detail={"code": error.code, "message": error.message}) from error


@router.post("", response_model=RunDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    workspace_id: UUID,
    payload: CreateRunRequest,
    request: Request,
    response: Response,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ],
) -> RunDetailResponse:
    """首次创建返回 201，内容相同的重试返回原运行和 200。"""
    try:
        detail, created = await _service(request).create(
            actor=_actor(principal),
            workspace_id=workspace_id,
            agent_id=payload.agent_id,
            agent_version=payload.agent_version,
            instruction=payload.instruction,
            idempotency_key=idempotency_key,
            request_id=get_request_id() or "unknown",
            project_id=payload.project_id,
            repository_ids=tuple(payload.repository_ids),
        )
    except RunError as exc:
        _raise_http(exc)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return RunDetailResponse.from_detail(detail)


@router.get("", response_model=RunListResponse)
async def list_runs(
    workspace_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before_id: UUID | None = None,
) -> RunListResponse:
    """只列出当前成员自己发起的运行。"""
    try:
        records = await _service(request).list_runs(
            actor=_actor(principal),
            workspace_id=workspace_id,
            limit=limit + 1,
            before_id=before_id,
        )
    except RunError as exc:
        _raise_http(exc)
    return RunListResponse(
        items=[RunSummaryResponse.model_validate(item) for item in records[:limit]],
        next_before_id=records[limit - 1].id if len(records) > limit else None,
    )


@router.post("/{run_id}/continue", status_code=202)
async def continue_task(
    workspace_id: UUID, run_id: UUID, payload: ContinueTaskRequest, request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
    idempotency_key: Annotated[str, Header(
        alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$",
    )],
) -> dict[str, object]:
    """202 仅代表跟进已持久接收；run_id 为空时仍在等待当前尝试结束。"""
    service = _service(request)
    try:
        await service.get(actor=_actor(principal), workspace_id=workspace_id, run_id=run_id)
        item = await service.continue_task(
            actor=_actor(principal), run_id=run_id, instruction=payload.instruction,
            idempotency_key=idempotency_key, request_id=get_request_id() or "unknown",
        )
    except RunError as error:
        _raise_http(error)
    return {"input_id": str(item.id), "task_id": str(item.task_id), "status": item.status,
            "sequence": item.sequence, "run_id": str(item.run_id) if item.run_id else None}


@router.get("/{run_id}/inputs")
async def task_inputs(
    workspace_id: UUID, run_id: UUID, request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> dict[str, object]:
    """只返回持久跟进回执，不把队列状态当作 Agent 已读或已完成。"""
    try:
        items = await _service(request).task_inputs(_actor(principal), workspace_id, run_id)
    except RunError as error:
        _raise_http(error)
    return {"items": [{"input_id": str(item.id), "task_id": str(item.task_id), "status": item.status,
                       "run_id": str(item.run_id) if item.run_id else None, "sequence": item.sequence}
                      for item in items]}


@router.get("/{run_id}", response_model=RunDetailResponse)
async def get_run(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> RunDetailResponse:
    """运行与任务正文都从数据库回读。"""
    try:
        detail = await _service(request).get(actor=_actor(principal), workspace_id=workspace_id, run_id=run_id)
    except RunError as exc:
        _raise_http(exc)
    return RunDetailResponse.from_detail(detail)


@router.get("/{run_id}/events", response_model=RunEventsResponse)
async def list_run_events(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RunEventsResponse:
    """JSON 分页与 SSE 共用持久序号，断线无需重新运行模型。"""
    try:
        events, latest = await _service(request).events(
            actor=_actor(principal),
            workspace_id=workspace_id,
            run_id=run_id,
            after=after,
            limit=limit,
        )
    except RunError as exc:
        _raise_http(exc)
    next_after = events[-1].sequence if events else after
    return RunEventsResponse(
        items=[RunEventResponse.model_validate(item) for item in events],
        next_after=next_after,
        latest_sequence=latest,
        has_more=next_after < latest,
    )


@router.get("/{run_id}/events/stream")
async def stream_run_events(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """每次读页重新验证 Cookie 与私有访问；到期断流，由客户端按序号续接。"""
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdecimal() or len(last_event_id) > 18:
            raise HTTPException(422, detail={"code": "INVALID_EVENT_CURSOR", "message": "事件序号无效"})
        after = max(after, int(last_event_id))
    try:
        await _service(request).get(actor=_actor(principal), workspace_id=workspace_id, run_id=run_id)
    except RunError as exc:
        _raise_http(exc)

    async def events() -> AsyncIterator[str]:
        cursor = after
        started = time.monotonic()
        while time.monotonic() - started < 55:
            if await request.is_disconnected():
                return
            try:
                # 长连接建立后退出登录、停用成员也必须切断，不能沿用最初 Principal。
                active = await current_principal(request)
                if _actor(active) != _actor(principal):
                    raise HTTPException(401)
                items, latest = await _service(request).events(
                    actor=_actor(active), workspace_id=workspace_id, run_id=run_id, after=cursor, limit=100
                )
                detail = await _service(request).get(actor=_actor(active), workspace_id=workspace_id, run_id=run_id)
            except (HTTPException, RunError):
                yield 'event: access_revoked\ndata: {"code":"ACCESS_REVOKED"}\n\n'
                return
            for item in items:
                cursor = item.sequence
                data = RunEventResponse.model_validate(item).model_dump_json()
                yield f"id: {cursor}\nevent: run_event\ndata: {data}\n\n"
            if cursor < latest:
                continue
            if detail.run.status in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "WAITING_CONTEXT",
                "WAITING_APPROVAL",
                "RECOVERY_REQUIRED",
            }:
                yield "event: stream_end\ndata: {}\n\n"
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)
        yield "event: stream_end\ndata: {}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/{run_id}/cancel", response_model=RunDetailResponse)
async def cancel_run(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> RunDetailResponse:
    """排队任务直接取消；执行中只登记取消请求，等待 Worker 确认。"""
    try:
        detail = await _service(request).cancel(
            actor=_actor(principal),
            workspace_id=workspace_id,
            run_id=run_id,
            request_id=get_request_id() or "unknown",
        )
    except RunError as exc:
        _raise_http(exc)
    return RunDetailResponse.from_detail(detail)


@router.get("/{run_id}/tools")
async def list_run_tools(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> dict[str, object]:
    """工具请求和事实结果独立展示，不把 Agent 解释当作测试证据。"""
    try:
        await _service(request).get(actor=_actor(principal), workspace_id=workspace_id, run_id=run_id)
    except RunError as exc:
        _raise_http(exc)
    store = cast(SqlAlchemyToolStore, request.app.state.tool_store)
    items = await store.list_calls(ToolScope(run_id, _actor(principal), "console-preview", 0))
    return {"items": [asdict(item) for item in items]}


@router.post("/{run_id}/recover", response_model=RunDetailResponse)
async def recover_run(
    workspace_id: UUID,
    run_id: UUID,
    payload: RecoverRequest,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> RunDetailResponse:
    """人类选择只读恢复；冻结预算与已成功工具，不复活终态或绕过撤权。"""
    del payload
    try:
        await _service(request).get(actor=_actor(principal), workspace_id=workspace_id, run_id=run_id)
        execution = cast(SqlAlchemyExecutionStore, request.app.state.execution_store)
        if not await execution.recover_read_only(_actor(principal), run_id):
            raise HTTPException(
                409, detail={"code": "RECOVERY_CONFLICT", "message": "只能只读恢复中断或等待上下文的运行"}
            )
        detail = await _service(request).get(actor=_actor(principal), workspace_id=workspace_id, run_id=run_id)
    except RunError as exc:
        _raise_http(exc)
    return RunDetailResponse.from_detail(detail)
