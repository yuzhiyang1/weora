"""协作控制台与本地连接器入口。Web Cookie 与设备 Bearer 不混用。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.collaboration.application.agent_mail import AgentMailError, SendRequest, SessionRegistration
from weora.modules.collaboration.infrastructure import agent_delivery as delivery
from weora.modules.collaboration.infrastructure import agent_sessions as sessions
from weora.modules.collaboration.infrastructure.agent_mail import AgentDevice, AgentEvidence, AgentMail
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.identity.application.service import IdentityService
from weora.shared.api.request_id import get_request_id
from weora.shared.infrastructure.database import SessionFactory
from weora.shared.infrastructure.postgres_watch import PostgresWatch

router = APIRouter(prefix="/workspaces/{workspace_id}/agent-collaboration", tags=["Agent collaboration"])
Read = Annotated[PrincipalRecord, Depends(current_principal)]
Write = Annotated[PrincipalRecord, Depends(write_principal)]
Bearer = Annotated[str | None, Header(alias="Authorization")]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairingInput(Input):
    name: Name


class PairInput(Input):
    code: Annotated[str, StringConstraints(min_length=32, max_length=128)]


class SessionInput(Input):
    # 任何运行时均可注册。只有识别到的投递协议会被本地适配器处理。
    runtime: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,15}$")]
    profile: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,64}$")] = "default"
    native_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    repo: str = Field("", max_length=120)
    branch: str = Field("", max_length=120)
    delivery: Literal["manual", "queue", "channel"] = "manual"
    preserve_name: bool = False


class GrantsInput(Input):
    member_ids: set[UUID] = Field(max_length=50)


class SendInput(Input):
    sender_id: UUID
    recipient_id: UUID
    task_id: UUID | None = None
    reply_to: UUID | None = None
    body: Text
    expects_reply: bool = True
    wake: bool = True


class ClaimInput(Input):
    message_id: UUID | None = None


class WakeResult(Input):
    state: Literal["accepted", "failed", "unknown"]


class EvidenceInput(Input):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    content: Text


def _identity(connection: Request | WebSocket) -> IdentityService:
    return cast(IdentityService, connection.app.state.identity_service)


@asynccontextmanager
async def _db(connection: Request | WebSocket) -> AsyncIterator[AsyncSession]:
    factory = cast(SessionFactory, connection.app.state.db_session_factory)
    async with factory.begin() as db:
        try:
            yield db
        except AgentMailError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from error


def _scope(owner: PrincipalRecord, workspace_id: UUID) -> None:
    if owner.workspace_id != workspace_id:
        raise HTTPException(404, {"code": "AGENT_COLLABORATION_UNAVAILABLE", "message": "工作空间不可访问"})


@asynccontextmanager
async def _device(
    connection: Request | WebSocket,
    workspace_id: UUID,
    bearer: str | None,
    *,
    lock: bool = False,
) -> AsyncIterator[tuple[AsyncSession, AgentDevice, PrincipalRecord]]:
    async with _db(connection) as db:
        device, principal = await sessions.authenticate(db, workspace_id, bearer, lock=lock)
        yield db, device, principal


async def _names(connection: Request | WebSocket, owner: PrincipalRecord) -> dict[UUID, str]:
    return dict(await _identity(connection).workspace_member_names(owner))


@router.get("/devices")
async def list_devices(workspace_id: UUID, request: Request, owner: Read) -> dict[str, object]:
    _scope(owner, workspace_id)
    async with _db(request) as db:
        return {"items": await sessions.devices(db, owner)}


@router.get("/device-self")
async def device_identity(
    workspace_id: UUID, request: Request, response: Response, authorization: Bearer = None,
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    async with _device(request, workspace_id, authorization) as (_, device, principal):
        return sessions.device_view(device) | {
            "user_id": principal.user_id,
            "display_name": principal.display_name,
            "workspace_id": principal.workspace_id,
            "workspace_name": principal.workspace_name,
            "role": principal.role,
        }


@router.delete("/device-self", status_code=204)
async def revoke_current_device(workspace_id: UUID, request: Request, authorization: Bearer = None) -> None:
    async with _db(request) as db:
        await sessions.revoke_by_bearer(db, workspace_id, authorization)


@router.post("/devices", status_code=201)
async def create_pairing(
    workspace_id: UUID,
    request: Request,
    response: Response,
    owner: Write,
    payload: PairingInput,
) -> dict[str, object]:
    _scope(owner, workspace_id)
    response.headers["Cache-Control"] = "no-store"
    async with _db(request) as db:
        return await sessions.begin_pairing(db, owner, payload.name)


@router.post("/devices/pair")
async def pair_device(
    workspace_id: UUID, request: Request, response: Response, payload: PairInput
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    async with _db(request) as db:
        return await sessions.pair(db, workspace_id, payload.code)


@router.delete("/devices/{device_id}", status_code=204)
async def revoke_device(workspace_id: UUID, device_id: UUID, request: Request, owner: Write) -> None:
    _scope(owner, workspace_id)
    async with _db(request) as db:
        await sessions.revoke(db, owner, device_id)


@router.get("/my-sessions")
async def my_sessions(workspace_id: UUID, request: Request, owner: Read) -> dict[str, object]:
    _scope(owner, workspace_id)
    names = await _names(request, owner)
    async with _db(request) as db:
        return {"items": await sessions.directory(db, owner, names, task_id=None, own=True)}


@router.get("/members")
async def member_options(workspace_id: UUID, request: Request, owner: Read) -> dict[str, object]:
    _scope(owner, workspace_id)
    return {
        "items": [
            {"id": member, "name": name}
            for member, name in (await _names(request, owner)).items()
            if member != owner.user_id
        ]
    }


@router.put("/my-sessions/{session_id}/contacts", status_code=204)
async def allow_contacts(
    workspace_id: UUID,
    session_id: UUID,
    request: Request,
    owner: Write,
    payload: GrantsInput,
) -> None:
    _scope(owner, workspace_id)
    names = await _names(request, owner)
    async with _db(request) as db:
        await sessions.update_grants(db, owner, session_id, payload.member_ids, set(names))


@router.get("/task/{task_id}")
async def task_mail(workspace_id: UUID, task_id: UUID, request: Request, owner: Read) -> dict[str, object]:
    _scope(owner, workspace_id)
    names = await _names(request, owner)
    async with _db(request) as db:
        await sessions.task_access(db, owner, task_id)
        contacts = await sessions.directory(db, owner, names, task_id=task_id)
        mails = await db.scalars(
            select(AgentMail)
            .where(
                AgentMail.workspace_id == workspace_id,
                AgentMail.task_id == task_id,
            )
            .order_by(AgentMail.created_at.desc(), AgentMail.id.desc())
            .limit(100)
        )
        evidence = await db.scalars(
            select(AgentEvidence)
            .where(AgentEvidence.task_id == task_id)
            .order_by(
                AgentEvidence.created_at.desc(),
                AgentEvidence.id.desc(),
            )
            .limit(50)
        )
        return {
            "sessions": contacts,
            "messages": [delivery.receipt(m, body=True) for m in mails],
            "evidence": [delivery.evidence_view(row) for row in evidence],
        }


@router.put("/sessions")
async def register_session(
    workspace_id: UUID,
    request: Request,
    payload: SessionInput,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with _device(request, workspace_id, authorization, lock=True) as (db, device, owner):
        row = await sessions.register(db, device, SessionRegistration(**payload.model_dump()))
        return sessions.session_view(row, device, owner.display_name)


@router.get("/sessions")
async def find_sessions(
    workspace_id: UUID,
    request: Request,
    authorization: Bearer = None,
    task_id: UUID | None = None,
    q: Annotated[str, Query(max_length=256)] = "",
) -> dict[str, object]:
    async with _device(request, workspace_id, authorization) as (db, _, owner):
        return {"items": await sessions.directory(db, owner, await _names(request, owner), task_id=task_id, query=q)}


@router.put("/sessions/{session_id}/tasks/{task_id}", status_code=204)
@router.delete("/sessions/{session_id}/tasks/{task_id}", status_code=204)
async def task_session(
    workspace_id: UUID,
    session_id: UUID,
    task_id: UUID,
    request: Request,
    authorization: Bearer = None,
) -> None:
    async with _device(request, workspace_id, authorization, lock=True) as (db, device, owner):
        await delivery.change_task_binding(db, device, owner, session_id, task_id, join=request.method == "PUT")


@router.post("/messages", status_code=201)
async def send_mail(
    workspace_id: UUID,
    request: Request,
    payload: SendInput,
    key: Key,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with _device(request, workspace_id, authorization, lock=True) as (db, device, owner):
        spec = SendRequest(key=key, **payload.model_dump())
        return await delivery.send(db, device, set(await _names(request, owner)), spec, get_request_id() or "unknown")


@router.get("/messages/{message_id}")
async def status_mail(
    workspace_id: UUID,
    message_id: UUID,
    request: Request,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with _device(request, workspace_id, authorization) as (db, device, owner):
        return await delivery.message_status(db, device, set(await _names(request, owner)), message_id)


@router.get("/pending")
async def pending_mail(workspace_id: UUID, request: Request, authorization: Bearer = None) -> dict[str, object]:
    async with _device(request, workspace_id, authorization) as (db, device, owner):
        return {"items": await delivery.pending(db, device, set(await _names(request, owner)))}


@router.post("/presence", status_code=204)
async def record_presence(workspace_id: UUID, request: Request, authorization: Bearer = None) -> None:
    async with _device(request, workspace_id, authorization, lock=True) as (_, device, _owner):
        device.seen_at = datetime.now(UTC)


@router.post("/sessions/{session_id}/claims")
async def claim_mail(
    workspace_id: UUID,
    session_id: UUID,
    request: Request,
    payload: ClaimInput,
    key: Key,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with _device(request, workspace_id, authorization, lock=True) as (db, device, owner):
        item = await delivery.claim(db, device, set(await _names(request, owner)), session_id, key, payload.message_id)
        return {"item": item}


@router.post("/sessions/{session_id}/messages/{message_id}/acknowledge")
async def acknowledge_mail(
    workspace_id: UUID,
    session_id: UUID,
    message_id: UUID,
    request: Request,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with _device(request, workspace_id, authorization, lock=True) as (db, device, owner):
        return await delivery.acknowledge(db, device, set(await _names(request, owner)), session_id, message_id)


@router.post("/sessions/{session_id}/messages/{message_id}/cancel")
async def cancel_mail(
    workspace_id: UUID,
    session_id: UUID,
    message_id: UUID,
    request: Request,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with _device(request, workspace_id, authorization, lock=True) as (db, device, _):
        return await delivery.cancel(db, device, session_id, message_id)


@router.post("/sessions/{session_id}/messages/{message_id}/wake")
async def reserve_wake(
    workspace_id: UUID,
    session_id: UUID,
    message_id: UUID,
    request: Request,
    authorization: Bearer = None,
) -> dict[str, bool]:
    async with _device(request, workspace_id, authorization, lock=True) as (db, device, owner):
        accepted = await delivery.reserve_wake(db, device, set(await _names(request, owner)), session_id, message_id)
        return {"reserved": accepted}


@router.put("/sessions/{session_id}/messages/{message_id}/wake", status_code=204)
async def finish_wake(
    workspace_id: UUID,
    session_id: UUID,
    message_id: UUID,
    request: Request,
    payload: WakeResult,
    authorization: Bearer = None,
) -> None:
    async with _device(request, workspace_id, authorization, lock=True) as (db, device, _):
        await delivery.finish_wake(db, device, session_id, message_id, payload.state)


@router.post("/sessions/{session_id}/tasks/{task_id}/evidence", status_code=201)
async def publish_evidence(
    workspace_id: UUID,
    session_id: UUID,
    task_id: UUID,
    request: Request,
    payload: EvidenceInput,
    key: Key,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with _device(request, workspace_id, authorization, lock=True) as (db, device, owner):
        return await delivery.publish(db, device, owner, session_id, task_id, key, payload.title, payload.content)


@router.websocket("/events")
async def wake_events(workspace_id: UUID, socket: WebSocket) -> None:
    """通知用于唤醒查询，重连和超时都从权威收件箱补取。"""
    bearer = socket.headers.get("Authorization")
    try:
        async with _device(socket, workspace_id, bearer) as (_, device, _owner):
            device_id = device.id
        watch = cast(PostgresWatch, socket.app.state.agent_mail_watch)
        await socket.accept()
        with watch.subscribe(device_id) as changed:
            while True:
                changed.clear()
                async with _device(socket, workspace_id, bearer, lock=True) as (db, device, owner):
                    device.seen_at = datetime.now(UTC)
                    items = await delivery.pending(db, device, set(await _names(socket, owner)))
                await socket.send_json({"type": "pending", "message_ids": [str(item["message_id"]) for item in items]})
                disconnected = asyncio.create_task(socket.receive())
                signaled = asyncio.create_task(changed.wait())
                try:
                    await asyncio.wait((disconnected, signaled), timeout=15, return_when=asyncio.FIRST_COMPLETED)
                    if disconnected.done():
                        # 本协议只下行信号。断开和非预期上行都结束订阅。
                        disconnected.result()
                        return
                finally:
                    for task in (disconnected, signaled):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(disconnected, signaled, return_exceptions=True)
    except HTTPException:
        await socket.close(code=1008)
    except WebSocketDisconnect:
        pass
