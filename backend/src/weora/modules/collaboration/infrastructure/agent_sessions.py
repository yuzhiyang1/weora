"""设备认证和会话目录，运行时只作为目录元数据与本机适配器标识。"""

from datetime import UTC, datetime
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.collaboration.application.agent_mail import (
    DEVICE_TTL,
    PAIRING_TTL,
    PRESENCE_TTL,
    AgentMailError,
    SessionRegistration,
    unavailable,
)
from weora.modules.collaboration.infrastructure.agent_mail import (
    AgentContactGrant,
    AgentDevice,
    AgentMail,
    AgentSession,
    AgentTaskSession,
)
from weora.modules.collaboration.infrastructure.tasks import TaskModel, TaskParticipantModel
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.identity.application.service import IdentityService


def _digest(secret: str) -> str:
    return sha256(secret.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    # PostgreSQL 使用带时区的列；测试用的 SQLite 会返回无时区值。
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def device_active(device: AgentDevice) -> bool:
    return device.token_hash is not None and device.revoked_at is None and _utc(device.expires_at) > datetime.now(UTC)


def device_view(device: AgentDevice) -> dict[str, object]:
    return {
        "id": device.id,
        "name": device.name,
        "paired": device.token_hash is not None,
        "seen_at": device.seen_at,
        "expires_at": device.expires_at,
        "expired": _utc(device.expires_at) <= datetime.now(UTC),
        "revoked_at": device.revoked_at,
    }


async def begin_pairing(db: AsyncSession, owner: PrincipalRecord, name: str) -> dict[str, object]:
    secret = token_urlsafe(32)
    device = AgentDevice(
        id=uuid4(),
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        name=name,
        pairing_hash=_digest(secret),
        expires_at=datetime.now(UTC) + PAIRING_TTL,
    )
    db.add(device)
    return device_view(device) | {"pairing_code": secret}


async def pair(
    db: AsyncSession,
    identity: IdentityService,
    workspace_id: UUID,
    code: str,
) -> dict[str, object]:
    device = await db.scalar(
        select(AgentDevice)
        .where(
            AgentDevice.workspace_id == workspace_id,
            AgentDevice.pairing_hash == _digest(code),
        )
        .with_for_update()
    )
    if (
        device is None
        or device.revoked_at is not None
        or _utc(device.expires_at) <= datetime.now(UTC)
        or not await identity.get_active_workspace_principal(user_id=device.user_id, workspace_id=workspace_id)
    ):
        raise AgentMailError(401, "AGENT_DEVICE_PAIRING_INVALID", "配对码无效或已过期")
    secret = token_urlsafe(32)
    device.pairing_hash = None
    device.token_hash = _digest(secret)
    device.expires_at = datetime.now(UTC) + DEVICE_TTL
    return device_view(device) | {"token": secret}


async def authenticate(
    db: AsyncSession,
    identity: IdentityService,
    workspace_id: UUID,
    authorization: str | None,
    *,
    lock: bool = False,
) -> tuple[AgentDevice, PrincipalRecord]:
    scheme, _, secret = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and 32 <= len(secret) <= 128:
        query = select(AgentDevice).where(
            AgentDevice.workspace_id == workspace_id,
            AgentDevice.token_hash == _digest(secret),
        )
        device = await db.scalar(query.with_for_update() if lock else query)
        if device and device_active(device):
            principal = await identity.get_active_workspace_principal(user_id=device.user_id, workspace_id=workspace_id)
            if principal:
                return device, principal
    raise AgentMailError(401, "AGENT_DEVICE_UNAUTHORIZED", "请重新配对 Weora 设备")


async def devices(db: AsyncSession, owner: PrincipalRecord) -> list[dict[str, object]]:
    rows = await db.scalars(
        select(AgentDevice)
        .where(
            AgentDevice.workspace_id == owner.workspace_id,
            AgentDevice.user_id == owner.user_id,
        )
        .order_by(AgentDevice.name, AgentDevice.id)
    )
    return [device_view(row) for row in rows]


async def revoke(db: AsyncSession, owner: PrincipalRecord, device_id: UUID) -> None:
    device = await db.scalar(
        select(AgentDevice)
        .where(
            AgentDevice.id == device_id,
            AgentDevice.workspace_id == owner.workspace_id,
            AgentDevice.user_id == owner.user_id,
        )
        .with_for_update()
    )
    if device is None:
        raise unavailable()
    if device.revoked_at is None:
        device.revoked_at = datetime.now(UTC)
        device.pairing_hash = None


async def register(db: AsyncSession, device: AgentDevice, spec: SessionRegistration) -> AgentSession:
    # 设备锁序列化重复登记；原生会话恢复后继承同一个地址和未读消息。
    session = await db.scalar(
        select(AgentSession).where(
            AgentSession.device_id == device.id,
            AgentSession.runtime == spec.runtime,
            AgentSession.profile == spec.profile,
            AgentSession.native_id == spec.native_id,
        )
    )
    if session is None:
        session = AgentSession(
            id=uuid4(),
            device_id=device.id,
            runtime=spec.runtime,
            profile=spec.profile,
            native_id=spec.native_id,
            name=spec.name,
        )
        db.add(session)
    if not spec.preserve_name:
        session.name = spec.name
    session.repo = spec.repo
    session.branch = spec.branch
    session.delivery = spec.delivery
    session.seen_at = datetime.now(UTC)
    return session


async def task_access(db: AsyncSession, owner: PrincipalRecord, task_id: UUID, *, write: bool = False) -> TaskModel:
    query = select(TaskModel).where(
        TaskModel.id == task_id,
        TaskModel.workspace_id == owner.workspace_id,
        TaskModel.collaboration_title.is_not(None),
    )
    task = await db.scalar(query.with_for_update() if write else query)
    if task is None or (write and task.status in {"done", "cancelled"}):
        raise unavailable()
    if task.created_by == owner.user_id:
        return task
    participant = await db.get(TaskParticipantModel, (task_id, owner.workspace_id, owner.user_id))
    if participant is not None:
        return task
    raise unavailable()


async def owned_session(
    db: AsyncSession,
    owner: PrincipalRecord,
    session_id: UUID,
    *,
    lock: bool = False,
) -> AgentSession:
    query = (
        select(AgentSession)
        .join(AgentDevice)
        .where(
            AgentSession.id == session_id,
            AgentDevice.workspace_id == owner.workspace_id,
            AgentDevice.user_id == owner.user_id,
            AgentDevice.revoked_at.is_(None),
        )
    )
    row = await db.scalar(query.with_for_update(of=AgentSession) if lock else query)
    if row is None:
        raise unavailable()
    return row


async def update_grants(
    db: AsyncSession,
    owner: PrincipalRecord,
    session_id: UUID,
    members: set[UUID],
    active: set[UUID],
) -> None:
    await owned_session(db, owner, session_id, lock=True)
    if owner.user_id in members or not members <= active:
        raise unavailable()
    old = set(await db.scalars(select(AgentContactGrant.user_id).where(AgentContactGrant.session_id == session_id)))
    removed = old - members
    if removed:
        await db.execute(
            delete(AgentContactGrant).where(
                AgentContactGrant.session_id == session_id,
                AgentContactGrant.user_id.in_(removed),
            )
        )
        sender_ids = (
            select(AgentSession.id)
            .join(AgentDevice)
            .where(
                AgentDevice.workspace_id == owner.workspace_id,
                AgentDevice.user_id.in_(removed),
            )
        )
        # 撤销后重新授权也不能恢复先前未结束的消息及其待领取回复。
        originals = select(AgentMail.id).where(
            AgentMail.task_id.is_(None),
            AgentMail.recipient_id == session_id,
            AgentMail.sender_id.in_(sender_ids),
            AgentMail.reply_to.is_(None),
        )
        pending = ("pending", "claimed", "needs_confirmation")
        await db.execute(
            update(AgentMail)
            .where(
                AgentMail.workspace_id == owner.workspace_id,
                AgentMail.state.in_(pending),
                (AgentMail.id.in_(originals) | AgentMail.reply_to.in_(originals)),
            )
            .values(state="cancelled", resolved_at=datetime.now(UTC))
        )
    db.add_all(AgentContactGrant(session_id=session_id, user_id=member) for member in members - old)


def session_view(session: AgentSession, device: AgentDevice, user_name: str) -> dict[str, object]:
    online = bool(device_active(device) and device.seen_at and _utc(device.seen_at) + PRESENCE_TTL > datetime.now(UTC))
    return {
        "id": session.id,
        "name": session.name,
        "address": f"{user_name}/{session.name}@{device.name}",
        "runtime": session.runtime,
        "owner_id": device.user_id,
        "owner_name": user_name,
        "device": device.name,
        "repo": session.repo,
        "branch": session.branch,
        "online": online,
        "delivery": session.delivery if online else "manual",
    }


async def directory(
    db: AsyncSession,
    owner: PrincipalRecord,
    names: dict[UUID, str],
    *,
    task_id: UUID | None,
    query: str = "",
    own: bool = False,
) -> list[dict[str, object]]:
    statement = (
        select(AgentSession, AgentDevice)
        .join(AgentDevice)
        .where(
            AgentDevice.workspace_id == owner.workspace_id,
            AgentDevice.revoked_at.is_(None),
            AgentDevice.user_id.in_(names),
            AgentDevice.token_hash.is_not(None),
            AgentDevice.expires_at > datetime.now(UTC),
        )
    )
    if own:
        statement = statement.where(AgentDevice.user_id == owner.user_id)
    elif task_id is not None:
        await task_access(db, owner, task_id)
        statement = statement.join(AgentTaskSession).where(AgentTaskSession.task_id == task_id)
    else:
        statement = statement.join(AgentContactGrant).where(AgentContactGrant.user_id == owner.user_id)
    result = []
    for session, device in (await db.execute(statement.order_by(AgentSession.name, AgentSession.id))).tuples():
        if task_id and not own:
            task = await db.get(TaskModel, task_id)
            assert task is not None
            if device.user_id != task.created_by and not await db.get(
                TaskParticipantModel,
                (task_id, owner.workspace_id, device.user_id),
            ):
                continue
        user_name = names[device.user_id]
        haystack = f"{user_name}/{session.name}@{device.name} {session.repo} {session.runtime} {session.branch}"
        if all(word in haystack.casefold() for word in query.casefold().split()):
            item = session_view(session, device, user_name)
            if own:
                item["allowed_members"] = list(
                    await db.scalars(
                        select(AgentContactGrant.user_id).where(
                            AgentContactGrant.session_id == session.id,
                        )
                    )
                )
                item["task_ids"] = list(
                    await db.scalars(
                        select(AgentTaskSession.task_id).where(
                            AgentTaskSession.session_id == session.id,
                        )
                    )
                )
            result.append(item)
    return result
