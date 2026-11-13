"""浏览器确认后，为 CLI 发放现有本地 Agent 设备凭据。"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import StringConstraints
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.collaboration.agent_mail_api import Input, Name, Read, Write, _db
from weora.modules.collaboration.application.agent_mail import DEVICE_TTL
from weora.modules.collaboration.infrastructure.agent_mail import AgentDevice, CliLoginRequest
from weora.modules.collaboration.infrastructure.agent_sessions import _digest, _utc, device_active, device_view
from weora.modules.identity.infrastructure.unit_of_work import get_active_workspace_principal_in_session

router = APIRouter(prefix="/cli-auth", tags=["CLI authentication"])
LOGIN_TTL = timedelta(minutes=10)
POLL_SECONDS = 3
Hash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Verifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]


class StartLogin(Input):
    name: Name
    verifier_hash: Hash


class LoginDecision(Input):
    decision: Literal["approve", "deny"]
    user_id: UUID
    workspace_id: UUID


class PollLogin(Input):
    verifier: Verifier


def unavailable() -> HTTPException:
    return HTTPException(404, {"code": "CLI_LOGIN_UNAVAILABLE", "message": "登录请求已失效，请在终端重新发起。"})


def view(row: CliLoginRequest) -> dict[str, object]:
    fingerprint = row.id.hex[:8].upper()
    return {"id": row.id, "device_name": row.name, "code": f"{fingerprint[:4]}-{fingerprint[4:]}"}


@router.post("/requests", status_code=201)
async def begin(payload: StartLogin, request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    source = _digest(request.client.host if request.client else "unknown")
    now = datetime.now(UTC)
    async with _db(request) as db:
        # PostgreSQL 事务锁让同来源并发请求不能越过短期创建限额。
        bind = db.get_bind()
        if bind.dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(source[:15], 16)})
        recent = await db.scalar(
            select(func.count())
            .select_from(CliLoginRequest)
            .where(
                CliLoginRequest.source_key == source,
                CliLoginRequest.created_at > now - timedelta(minutes=1),
            )
        )
        if recent is not None and recent >= 30:
            raise HTTPException(429, {"code": "CLI_LOGIN_RATE_LIMITED", "message": "登录请求过多，请稍后重试。"})
        await db.execute(delete(CliLoginRequest).where(CliLoginRequest.expires_at <= now))
        row = CliLoginRequest(
            id=uuid4(),
            name=payload.name,
            verifier_hash=payload.verifier_hash,
            source_key=source,
            created_at=now,
            expires_at=now + LOGIN_TTL,
            next_poll_at=now,
        )
        db.add(row)
        return view(row) | {"interval": POLL_SECONDS, "expires_in": int(LOGIN_TTL.total_seconds())}


@router.get("/requests/{login_id}")
async def preview(login_id: UUID, request: Request, response: Response, principal: Read) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    async with _db(request) as db:
        row = await db.get(CliLoginRequest, login_id)
        if row is None or _utc(row.expires_at) <= datetime.now(UTC) or row.device_id or row.denied_at:
            raise unavailable()
        return view(row) | {"user_id": principal.user_id, "workspace_id": principal.workspace_id}


@router.post("/requests/{login_id}/decision")
async def decide(
    login_id: UUID,
    payload: LoginDecision,
    request: Request,
    response: Response,
    principal: Write,
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    # 浏览器标签页共享会话 Cookie，提交时必须核对页面显示的账号和工作空间。
    if payload.user_id != principal.user_id:
        raise HTTPException(409, {"code": "CLI_ACCOUNT_CHANGED", "message": "登录账号已改变，请刷新授权页。"})
    if payload.workspace_id != principal.workspace_id:
        raise HTTPException(409, {"code": "CLI_WORKSPACE_CHANGED", "message": "当前工作空间已改变，请重新打开授权页。"})
    now = datetime.now(UTC)
    async with _db(request) as db:
        row = await db.get(CliLoginRequest, login_id, with_for_update=True)
        if row is None or _utc(row.expires_at) <= now or row.device_id or row.denied_at:
            raise unavailable()
        if payload.decision == "deny":
            row.denied_at = now
            return {"status": "denied"}
        # 浏览器仅绑定所有者，设备要等原 CLI 证明持有验证串后才可使用。
        device = AgentDevice(
            id=uuid4(),
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
            name=row.name,
            expires_at=row.expires_at,
        )
        db.add(device)
        await db.flush()
        row.device_id = device.id
        # 同一次浏览器确认授权设备同步。轮询重试不能重新开启已被成员关闭的同步。
        grant_skill_sync = cast(
            Callable[[AsyncSession, UUID, UUID], Awaitable[None]], request.app.state.grant_cli_skill_sync
        )
        await grant_skill_sync(db, principal.workspace_id, device.id)
        return {"status": "approved"}


@router.post("/requests/{login_id}/poll")
async def poll(login_id: UUID, payload: PollLogin, request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    now = datetime.now(UTC)
    async with _db(request) as db:
        row = await db.get(CliLoginRequest, login_id, with_for_update=True)
        if row is None or not compare_digest(row.verifier_hash, _digest(payload.verifier)):
            raise unavailable()
        if _utc(row.expires_at) <= now:
            return {"status": "expired"}
        if row.denied_at is not None:
            return {"status": "denied"}
        if row.device_id is None:
            if _utc(row.next_poll_at) > now:
                return {"status": "slow_down", "interval": POLL_SECONDS}
            row.next_poll_at = now + timedelta(seconds=POLL_SECONDS)
            return {"status": "pending", "interval": POLL_SECONDS}
        device = await db.get(AgentDevice, row.device_id, with_for_update=True)
        if device is None or device.revoked_at:
            return {"status": "denied"}
        # 兑换持有行锁，复用当前连接，避免并发请求各自持有连接又等待连接池。
        principal = await get_active_workspace_principal_in_session(
            db,
            user_id=device.user_id,
            workspace_id=device.workspace_id,
        )
        if principal is None:
            return {"status": "denied"}
        # 同一个验证串可安全重试网络丢包。平台仅持久化两个用途各自的摘要。
        secret = _digest(f"weora-cli-device-v1:{login_id}:{payload.verifier}")
        if device.token_hash is None:
            device.token_hash = _digest(secret)
            device.expires_at = now + DEVICE_TTL
        elif not device_active(device):
            return {"status": "denied"}
        return device_view(device) | {
            "status": "authorized",
            "token": secret,
            "workspace_id": principal.workspace_id,
            "workspace_name": principal.workspace_name,
            "display_name": principal.display_name,
        }
