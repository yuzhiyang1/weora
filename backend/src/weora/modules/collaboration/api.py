"""协作任务 HTTP 接口，仅返回已发布的协作信息。"""

from dataclasses import asdict
from typing import Annotated, Literal, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from weora.modules.collaboration.application.tasks import TaskCollaboration, TaskStatus
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.identity.application.service import IdentityService
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.application.task_collaboration import CollaborationConflict, TaskCollaborationService
from weora.modules.runs.domain.errors import RunError, RunNotFoundError, RunSubmissionRaceError, RunVersionNotFoundError
from weora.shared.api.request_id import get_request_id

router = APIRouter(prefix="/workspaces/{workspace_id}/tasks", tags=["Tasks"])

Read = Annotated[PrincipalRecord, Depends(current_principal)]
Write = Annotated[PrincipalRecord, Depends(write_principal)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class RevisionRequest(BaseModel):
    """使用读取时的版本防止旧页面覆盖新指派。"""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)


class ShareRequest(RevisionRequest):
    """明确填写可共享的目标及接收范围。"""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    participant_ids: list[UUID] = Field(max_length=50)


class AssignRequest(RevisionRequest):
    """指派、启动和活跃运行期间的交接使用同一任务修订号。"""

    kind: Literal["member", "agent"] | None
    assignee_id: UUID | None
    version: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None = None
    mode: Literal["assign", "start"]
    stop_current: bool = False


class StatusRequest(RevisionRequest):
    """任务状态独立于负责人和 Run 结果。"""

    status: TaskStatus
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)] | None = None


def _actor(principal: PrincipalRecord, workspace_id: UUID) -> RunActor:
    if principal.workspace_id != workspace_id:
        _error(RunNotFoundError())
    return RunActor(principal.user_id, workspace_id)


def _service(request: Request) -> TaskCollaborationService:
    return cast(TaskCollaborationService, request.app.state.task_collaboration_service)


def _error(error: Exception) -> NoReturn:
    if isinstance(error, RunNotFoundError):
        raise HTTPException(404, detail={"code": "TASK_NOT_FOUND", "message": "任务不存在或无访问权限"}) from error
    if isinstance(error, RunVersionNotFoundError):
        raise HTTPException(409, detail={"code": error.code, "message": error.message}) from error
    if isinstance(error, RunError):
        raise HTTPException(409, detail={"code": error.code, "message": error.message}) from error
    message = str(error) if isinstance(error, CollaborationConflict) else "任务正在更新，请重试"
    raise HTTPException(409, detail={"code": "TASK_CONFLICT", "message": message}) from error


def _view(state: TaskCollaboration, actor: RunActor) -> dict[str, object]:
    return {
        "id": state.task_id,
        "title": state.title,
        "summary": state.summary,
        "participant_ids": state.participants,
        "assignment": asdict(state.assignment) if state.assignment else None,
        "pending_assignment": asdict(state.pending) if state.pending else None,
        "status": state.status,
        "revision": state.revision,
        "can_share": state.owner_id == actor.user_id,
        "can_assign": state.can_manage(actor.user_id),
    }


@router.get("")
async def list_tasks(workspace_id: UUID, request: Request, principal: Read) -> dict[str, object]:
    actor = _actor(principal, workspace_id)
    return {"items": [_view(state, actor) for state in await _service(request).list_shared(actor)]}


@router.get("/{task_id}")
async def task_detail(workspace_id: UUID, task_id: UUID, request: Request, principal: Read) -> dict[str, object]:
    actor = _actor(principal, workspace_id)
    try:
        state, active, activity = await _service(request).detail(actor, task_id)
    except RunError as error:
        _error(error)
    identity = cast(IdentityService, request.app.state.identity_service)
    names = dict(await identity.workspace_member_names(principal))
    return _view(state, actor) | {
        "participants": [
            {"id": user_id, "name": names[user_id]}
            for user_id in (state.owner_id, *state.participants)
            if user_id in names
        ],
        "activity": [item | {"actor_name": names.get(UUID(str(item["actor_id"])))} for item in activity],
        # 跨人协作只显示占用状态；打开 Run 仍走运行权限。
        "active_run": {
            "status": active.status,
            "id": active.id if active.created_by == actor.user_id else None,
            "stopping": active.cancellation_requested_at is not None,
        }
        if active
        else None,
    }


@router.get("/{task_id}/members")
async def member_options(workspace_id: UUID, task_id: UUID, request: Request, principal: Read) -> dict[str, object]:
    actor = _actor(principal, workspace_id)
    try:
        state, _, _ = await _service(request).detail(actor, task_id)
        if not state.can_manage(actor.user_id):
            raise RunNotFoundError
    except RunError as error:
        _error(error)
    identity = cast(IdentityService, request.app.state.identity_service)
    return {
        "items": [{"id": user_id, "name": name} for user_id, name in await identity.workspace_member_names(principal)]
    }


@router.put("/{task_id}/sharing", status_code=204)
async def share_task(
    workspace_id: UUID, task_id: UUID, payload: ShareRequest, request: Request, principal: Write
) -> None:
    try:
        await _service(request).share(
            _actor(principal, workspace_id),
            task_id,
            revision=payload.revision,
            title=payload.title,
            summary=payload.summary,
            participant_ids=tuple(payload.participant_ids),
        )
    except (RunError, CollaborationConflict) as error:
        _error(error)


@router.post("/{task_id}/assignment")
async def assign_task(
    workspace_id: UUID,
    task_id: UUID,
    payload: AssignRequest,
    request: Request,
    principal: Write,
    key: Key,
) -> dict[str, object]:
    try:
        run_id = await _service(request).assign(
            _actor(principal, workspace_id),
            task_id,
            revision=payload.revision,
            kind=payload.kind,
            assignee_id=payload.assignee_id,
            version=payload.version,
            mode=payload.mode,
            stop_current=payload.stop_current,
            key=key,
            request_id=get_request_id() or "unknown",
        )
    except (RunError, CollaborationConflict, RunSubmissionRaceError) as error:
        _error(error)
    return {"run_id": run_id}


@router.patch("/{task_id}/status", status_code=204)
async def set_task_status(
    workspace_id: UUID,
    task_id: UUID,
    payload: StatusRequest,
    request: Request,
    principal: Write,
) -> None:
    try:
        await _service(request).set_status(
            _actor(principal, workspace_id), task_id, payload.revision, payload.status, payload.note
        )
    except (RunError, CollaborationConflict) as error:
        _error(error)
