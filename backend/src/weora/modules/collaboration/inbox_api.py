"""当前成员的收件箱查询和已读、归档操作。"""

import asyncio
import base64
import binascii
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from weora.modules.collaboration.api import Read, Write, _actor
from weora.modules.collaboration.application.inbox import InboxBulkAction, InboxService
from weora.modules.identity.api.dependencies import current_principal
from weora.modules.runs.application.ports import RunActor

router = APIRouter(prefix="/workspaces/{workspace_id}/inbox", tags=["Inbox"])


class InboxUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[UUID] = Field(min_length=1, max_length=100)
    read: bool | None = None
    archived: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "InboxUpdate":
        if self.read is None and self.archived is None:
            raise ValueError("请指定已读或归档状态")
        return self


class InboxGroupArchive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: bool


class InboxBulkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: InboxBulkAction


def _cursor_scope(actor: RunActor, unread_only: bool) -> list[str | bool]:
    return [str(actor.workspace_id), str(actor.user_id), unread_only]


def _decode_cursor(value: str, scope: list[str | bool]) -> tuple[datetime, UUID]:
    try:
        cursor_scope, created_at, item_id = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        timestamp = datetime.fromisoformat(created_at)
        if cursor_scope != scope or timestamp.tzinfo is None:
            raise ValueError
        return timestamp.astimezone(UTC), UUID(item_id)
    except (ValueError, TypeError, AttributeError, OverflowError, binascii.Error) as exc:
        raise HTTPException(422, detail={"code": "INVALID_INBOX_CURSOR", "message": "归档游标无效"}) from exc


@router.get("")
async def inbox(workspace_id: UUID, request: Request, principal: Read, archived: bool = False) -> dict[str, object]:
    service = cast(InboxService, request.app.state.inbox_service)
    return {"items": await service.list_items(_actor(principal, workspace_id), archived)}


@router.get("/archived")
async def archived_inbox(
    workspace_id: UUID,
    request: Request,
    principal: Read,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    unread_only: bool = False,
) -> dict[str, object]:
    actor = _actor(principal, workspace_id)
    service = cast(InboxService, request.app.state.inbox_service)
    scope = _cursor_scope(actor, unread_only)
    before = _decode_cursor(cursor, scope) if cursor is not None else None
    items, next_before = await service.archive_page(actor, before=before, limit=limit, unread_only=unread_only)
    next_cursor = (
        base64.urlsafe_b64encode(json.dumps([scope, next_before[0].isoformat(), str(next_before[1])]).encode()).decode()
        if next_before
        else None
    )
    return {"items": items, "next_cursor": next_cursor, "has_more": next_cursor is not None}


@router.get("/events/stream")
async def stream_inbox(workspace_id: UUID, request: Request, principal: Read) -> StreamingResponse:
    actor = _actor(principal, workspace_id)
    service = cast(InboxService, request.app.state.inbox_service)

    async def events() -> AsyncIterator[str]:
        last_revision = None
        started = time.monotonic()
        while time.monotonic() - started < 55:
            if await request.is_disconnected():
                return
            try:
                # 每次读取都验证会话和成员身份。重新连接时发送当前版本以补偿断线。
                if _actor(await current_principal(request), workspace_id) != actor:
                    raise HTTPException(401)
            except HTTPException:
                yield "event: access_revoked\ndata: {}\n\n"
                return
            revision = await service.revision(actor)
            if revision != last_revision:
                last_revision = revision
                yield f'event: inbox_changed\ndata: {{"revision":{revision}}}\n\n'
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.patch("/groups/{task_id}", status_code=204)
async def set_group_archive(
    workspace_id: UUID,
    task_id: UUID,
    payload: InboxGroupArchive,
    request: Request,
    principal: Write,
) -> None:
    service = cast(InboxService, request.app.state.inbox_service)
    if not await service.set_group_archive(_actor(principal, workspace_id), task_id, payload.archived):
        raise HTTPException(404, detail={"code": "INBOX_NOT_FOUND", "message": "事项不存在或无访问权限"})


@router.post("/bulk", status_code=204)
async def bulk_inbox(workspace_id: UUID, payload: InboxBulkUpdate, request: Request, principal: Write) -> None:
    service = cast(InboxService, request.app.state.inbox_service)
    await service.bulk(_actor(principal, workspace_id), payload.action)


@router.patch("", status_code=204)
async def update_inbox(workspace_id: UUID, payload: InboxUpdate, request: Request, principal: Write) -> None:
    service = cast(InboxService, request.app.state.inbox_service)
    if not await service.update(
        _actor(principal, workspace_id),
        payload.item_ids,
        read=payload.read,
        archived=payload.archived,
    ):
        raise HTTPException(404, detail={"code": "INBOX_NOT_FOUND", "message": "事项不存在或无访问权限"})
