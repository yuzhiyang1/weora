"""运行 API：认证、CSRF 与私有任务隔离，不在请求中调用模型。"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Annotated, Literal, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from weora.modules.context.domain import ContextError
from weora.modules.coordination.console import FeishuTaskConsole
from weora.modules.coordination.domain import CoordinationError
from weora.modules.coordination.service import CoordinationService
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
from weora.modules.runs.application.ports import RunActor, RunDetail
from weora.modules.runs.application.service import RunService
from weora.modules.runs.domain.errors import RunError, RunNotFoundError, RunVersionNotFoundError
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.watch import RunWatch
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


def _feishu_console(request: Request, actor: RunActor, detail: RunDetail) -> FeishuTaskConsole | None:
    coordinator = cast(CoordinationService | None, getattr(request.app.state, "coordination_service", None))
    metadata = detail.task.origin_metadata or {}
    if (
        coordinator and detail.task.origin == "feishu" and detail.run.collaboration_context is None
        and metadata.get("coordination_scope_id") and metadata.get("coordination_ticket_id")
    ):
        return FeishuTaskConsole(coordinator, actor, detail)
    return None


def _raise_feishu_input(error: CoordinationError | ContextError) -> NoReturn:
    messages = {
        "STEERING_UNAVAILABLE": "本轮已无法接收指令，请刷新后选择后续运行。",
        "EXPLICIT_RETRY_REQUIRED": "本轮已失败或取消，请明确重试。",
        "NOT_RETRYABLE": "当前任务状态无法重试，请刷新后再试。",
        "INPUT_KEY_CONFLICT": "此发送请求已用于另一条指令，请重新发送。",
        "TASK_INPUT_LIMIT": "待处理指令已达上限，请等现有指令处理后再发送。",
        "CHANNEL_RECONCILIATION_REQUIRED": "请先确认飞书原话题的消息投递状态，再重试。",
    }
    raise HTTPException(409, detail={
        "code": error.code, "message": messages.get(error.code, "当前无法操作这条飞书任务，请刷新后检查任务状态。"),
    }) from error


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
        actor = _actor(principal)
        detail = await service.get(actor=actor, workspace_id=workspace_id, run_id=run_id)
        feishu = _feishu_console(request, actor, detail)
        if feishu:
            return await feishu.submit(payload.mode, payload.instruction, idempotency_key)
        if payload.mode == "retry":
            raise HTTPException(409, detail={"code": "NOT_RETRYABLE", "message": "此任务请通过后续运行继续。"})
        item = await service.continue_task(
            actor=_actor(principal), run_id=run_id, instruction=payload.instruction,
            idempotency_key=idempotency_key, request_id=get_request_id() or "unknown",
            steer=payload.mode == "steer",
        )
    except RunError as error:
        _raise_http(error)
    except (CoordinationError, ContextError) as error:
        _raise_feishu_input(error)
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


@router.get("/{run_id}/conversation")
async def run_conversation(
    workspace_id: UUID, run_id: UUID, request: Request, response: Response,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> dict[str, object]:
    """正文只随当前会话权限返回，浏览器和代理均不得缓存。"""
    response.headers["Cache-Control"] = "no-store"
    try:
        actor = _actor(principal)
        service = _service(request)
        detail = await service.get(actor=actor, workspace_id=workspace_id, run_id=run_id)
        feishu = _feishu_console(request, actor, detail)
        return await feishu.conversation() if feishu else await service.conversation(actor, workspace_id, run_id)
    except RunError as error:
        _raise_http(error)
    except (CoordinationError, ContextError) as error:
        _raise_feishu_input(error)


@router.get("/{run_id}", response_model=RunDetailResponse)
async def get_run(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> RunDetailResponse:
    """运行与任务正文都从数据库回读。"""
    actor = _actor(principal)
    try:
        detail = await _service(request).get(actor=actor, workspace_id=workspace_id, run_id=run_id)
    except RunError as exc:
        _raise_http(exc)
    result = RunDetailResponse.from_detail(detail)
    console = _feishu_console(request, actor, detail)
    if console and detail.run.status == "SUCCEEDED":
        try:
            result.task_blocked_attention = await console.blocked_attention()
        except RunNotFoundError:
            # 原生历史仍可读时，协调记录的撤权或清理不应导致详情接口失败。
            pass
        except CoordinationError as error:
            if error.code != "SCOPE_NOT_FOUND":
                raise
    return result


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
        events, latest, _ = await _service(request).events(
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


@router.get("/{run_id}/watch")
async def watch_run(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
) -> StreamingResponse:
    """订阅轻量状态。终态继续复验授权，历史事件和正文由各自接口按需读取。"""
    service = _service(request)
    watch = cast(RunWatch, request.app.state.run_watch)

    async def snapshots() -> AsyncIterator[str]:
        deadline = time.monotonic() + 55
        previous = None
        actor = None
        # 先登记通知，再读快照，避免提交发生在读取与登记之间。
        with watch.subscribe(run_id) as changed:
            while time.monotonic() < deadline and not await request.is_disconnected():
                changed.clear()
                try:
                    active = _actor(await current_principal(request))
                    if actor is not None and active != actor:
                        raise HTTPException(401)
                    actor = active
                    current = await service.watch_state(active, workspace_id, run_id)
                except (HTTPException, RunNotFoundError):
                    # EventSource 读不到 HTTP 错误码；初连与断线续连都用同一撤权事件清理客户端。
                    yield 'event: access_revoked\ndata: {}\n\n'
                    return
                if current != previous:
                    previous = current
                    run_status, sequence, title = current
                    payload = json.dumps({"status": run_status, "last_sequence": sequence, "title": title})
                    yield f"event: run_state\ndata: {payload}\n\n"
                else:
                    yield "event: access_confirmed\ndata: {}\n\n"
                # 五秒复验来源授权，也能补偿通知丢失；突发通知合并为一次读取。
                try:
                    await asyncio.wait_for(changed.wait(), timeout=min(5, max(0, deadline - time.monotonic())))
                except TimeoutError:
                    pass
                else:
                    await asyncio.sleep(0.25)
        yield "event: stream_rotate\ndata: {}\n\n"

    return StreamingResponse(
        snapshots(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
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
                items, latest, run_status = await _service(request).events(
                    actor=_actor(active), workspace_id=workspace_id, run_id=run_id, after=cursor, limit=100
                )
            except (HTTPException, RunError):
                yield 'event: access_revoked\ndata: {"code":"ACCESS_REVOKED"}\n\n'
                return
            for item in items:
                cursor = item.sequence
                data = RunEventResponse.model_validate(item).model_dump_json()
                yield f"id: {cursor}\nevent: run_event\ndata: {data}\n\n"
            if cursor < latest:
                continue
            if run_status in {
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
