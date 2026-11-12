"""以原生会话为单位领取，事务内记录幂等结果和回执。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.collaboration.application.agent_mail import (
    CLAIM_TTL,
    LOOP_LIMIT,
    LOOP_WINDOW,
    PENDING_TTL,
    WAKE_TTL,
    AgentMailError,
    SendRequest,
    conflict,
    unavailable,
)
from weora.modules.collaboration.infrastructure.agent_mail import (
    AgentContactGrant,
    AgentDevice,
    AgentEvidence,
    AgentMail,
    AgentSession,
    AgentTaskSession,
)
from weora.modules.collaboration.infrastructure.agent_sessions import device_active, task_access
from weora.modules.collaboration.infrastructure.tasks import TaskModel, TaskParticipantModel
from weora.modules.identity.application.ports import PrincipalRecord

# 领取超时后，原领取者仍可用原键恢复。确认或关闭前，会话继续为这条消息保留。
_SESSION_OCCUPYING_CLAIM_STATES = ("claimed", "needs_confirmation")


def _time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def receipt(row: AgentMail, *, body: bool = False) -> dict[str, object]:
    now = datetime.now(UTC)
    state, wake_state = row.state, row.wake_state
    if state == "claimed" and row.claimed_at and row.claimed_at.replace(tzinfo=UTC) <= now - CLAIM_TTL:
        state = "needs_confirmation"
    if state == "pending" and row.created_at.replace(tzinfo=UTC) <= now - PENDING_TTL:
        state = "expired"
    if wake_state == "attempting" and row.wake_at and row.wake_at.replace(tzinfo=UTC) <= now - WAKE_TTL:
        wake_state = "unknown"
    item: dict[str, object] = {
        "id": row.id,
        "sender_id": row.sender_id,
        "recipient_id": row.recipient_id,
        "task_id": row.task_id,
        "reply_to": row.reply_to,
        "expects_reply": row.expects_reply,
        "state": state,
        "created_at": _time(row.created_at),
        "claimed_at": _time(row.claimed_at),
        "resolved_at": _time(row.resolved_at),
        "wake_state": wake_state,
        "wake_at": _time(row.wake_at),
    }
    if body:
        item["body"] = row.body
    return item


async def local_session(db: AsyncSession, device: AgentDevice, session_id: UUID, *, lock: bool = False) -> AgentSession:
    query = select(AgentSession).where(AgentSession.id == session_id, AgentSession.device_id == device.id)
    row = await db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise unavailable()
    return row


async def change_task_binding(
    db: AsyncSession,
    device: AgentDevice,
    owner: PrincipalRecord,
    session_id: UUID,
    task_id: UUID,
    *,
    join: bool,
) -> None:
    await local_session(db, device, session_id, lock=True)
    if join:
        await task_access(db, owner, task_id, write=True)
    row = await db.get(AgentTaskSession, (task_id, session_id))
    if join and row is None:
        db.add(AgentTaskSession(task_id=task_id, session_id=session_id))
    if not join and row is not None:
        await db.delete(row)
        await db.execute(
            update(AgentMail)
            .where(
                AgentMail.task_id == task_id,
                AgentMail.state.in_(("pending", "claimed", "needs_confirmation")),
                (AgentMail.sender_id == session_id) | (AgentMail.recipient_id == session_id),
            )
            .values(state="cancelled", resolved_at=datetime.now(UTC))
        )


async def _can_deliver(
    db: AsyncSession,
    workspace_id: UUID,
    members: set[UUID],
    sender_id: UUID,
    recipient_id: UUID,
    task_id: UUID | None,
    *,
    reverse: bool = False,
    lock_task: bool = False,
) -> bool:
    ids = {sender_id, recipient_id}
    devices = dict(
        (
            await db.execute(
                select(AgentSession.id, AgentDevice)
                .join(AgentDevice)
                .where(
                    AgentSession.id.in_(ids),
                    AgentDevice.workspace_id == workspace_id,
                )
            )
        )
        .tuples()
        .all()
    )
    if set(devices) != ids or not all(device_active(d) and d.user_id in members for d in devices.values()):
        return False
    if task_id is None:
        origin, destination = (recipient_id, sender_id) if reverse else (sender_id, recipient_id)
        return bool(await db.get(AgentContactGrant, (destination, devices[origin].user_id)))
    query = select(TaskModel).where(
        TaskModel.id == task_id,
        TaskModel.workspace_id == workspace_id,
        TaskModel.collaboration_title.is_not(None),
        TaskModel.status.not_in(("done", "cancelled")),
    )
    task = await db.scalar(query.with_for_update() if lock_task else query)
    if task is None:
        return False
    for session_id, device in devices.items():
        if not await db.get(AgentTaskSession, (task_id, session_id)):
            return False
        if device.user_id != task.created_by and not await db.get(
            TaskParticipantModel,
            (task_id, workspace_id, device.user_id),
        ):
            return False
    return True


async def send(
    db: AsyncSession,
    device: AgentDevice,
    active_members: set[UUID],
    spec: SendRequest,
    request_id: str,
) -> dict[str, object]:
    # 固定加锁顺序。授权撤销、发送重试和回复同时发生时都由同一会话行串行化。
    locked = list(
        await db.scalars(
            select(AgentSession)
            .join(AgentDevice)
            .where(
                AgentSession.id.in_((spec.sender_id, spec.recipient_id)),
                AgentDevice.workspace_id == device.workspace_id,
            )
            .order_by(AgentSession.id)
            .with_for_update(of=AgentSession)
        )
    )
    if len(locked) != 2 or not any(s.id == spec.sender_id and s.device_id == device.id for s in locked):
        raise unavailable()
    is_reply = spec.reply_to is not None
    if not await _can_deliver(
        db,
        device.workspace_id,
        active_members,
        spec.sender_id,
        spec.recipient_id,
        spec.task_id,
        reverse=is_reply,
        lock_task=True,
    ):
        raise unavailable()
    existing = await db.scalar(
        select(AgentMail).where(
            AgentMail.sender_id == spec.sender_id,
            AgentMail.send_key == spec.key,
        )
    )
    now = datetime.now(UTC)
    expects_reply = spec.expects_reply and not is_reply
    if existing:
        if (
            existing.recipient_id,
            existing.body,
            existing.task_id,
            existing.reply_to,
            existing.expects_reply,
            existing.wake,
        ) != (spec.recipient_id, spec.body, spec.task_id, spec.reply_to, expects_reply, spec.wake):
            raise conflict()
        return receipt(existing)
    # 对同一对会话的往返设上限。两边的发送都持有这两条会话锁。
    participants = (spec.sender_id, spec.recipient_id)
    count = await db.scalar(
        select(func.count(AgentMail.id)).where(
            AgentMail.workspace_id == device.workspace_id,
            AgentMail.sender_id.in_(participants),
            AgentMail.recipient_id.in_(participants),
            AgentMail.created_at >= now - LOOP_WINDOW,
        )
    )
    if (count or 0) >= LOOP_LIMIT:
        raise AgentMailError(429, "AGENT_CONVERSATION_RATE_LIMIT", "这两条会话近期消息过多，请稍后继续")
    if spec.reply_to is not None:
        parent = await db.get(AgentMail, spec.reply_to)
        if (
            parent is None
            or parent.workspace_id != device.workspace_id
            or parent.sender_id != spec.recipient_id
            or parent.recipient_id != spec.sender_id
            or parent.task_id != spec.task_id
            or not parent.expects_reply
            or parent.state not in {"claimed", "needs_confirmation"}
        ):
            raise conflict()
        parent.state, parent.resolved_at = "replied", now
    row = AgentMail(
        id=uuid4(),
        workspace_id=device.workspace_id,
        task_id=spec.task_id,
        sender_id=spec.sender_id,
        recipient_id=spec.recipient_id,
        send_key=spec.key,
        reply_to=spec.reply_to,
        body=spec.body,
        wake=spec.wake,
        expects_reply=expects_reply,
        state="pending",
        created_at=now,
        request_id=request_id,
    )
    db.add(row)
    return receipt(row)


async def expire(db: AsyncSession, session_id: UUID) -> None:
    now = datetime.now(UTC)
    await db.execute(
        update(AgentMail)
        .where(
            AgentMail.recipient_id == session_id,
            AgentMail.state == "claimed",
            AgentMail.claimed_at <= now - CLAIM_TTL,
        )
        .values(state="needs_confirmation")
    )
    await db.execute(
        update(AgentMail)
        .where(
            AgentMail.recipient_id == session_id,
            AgentMail.state == "pending",
            AgentMail.created_at <= now - PENDING_TTL,
        )
        .values(state="expired", resolved_at=now)
    )


async def _authorized_mail(
    db: AsyncSession,
    row: AgentMail,
    members: set[UUID],
    *,
    lock_task: bool = False,
) -> bool:
    return await _can_deliver(
        db,
        row.workspace_id,
        members,
        row.sender_id,
        row.recipient_id,
        row.task_id,
        reverse=row.reply_to is not None,
        lock_task=lock_task,
    )


async def pending(
    db: AsyncSession,
    device: AgentDevice,
    members: set[UUID],
) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    row, session = AgentMail, AgentSession
    statement = (
        select(row, session)
        .join(session, row.recipient_id == session.id)
        .where(
            row.workspace_id == device.workspace_id,
            session.device_id == device.id,
            row.state == "pending",
            row.created_at > now - PENDING_TTL,
        )
    )
    found: list[dict[str, object]] = []
    for mail, target in (await db.execute(statement.order_by(row.created_at, row.id).limit(100))).tuples():
        if await _authorized_mail(db, mail, members):
            found.append(
                {
                    "message_id": mail.id,
                    "session_id": target.id,
                    "native_id": target.native_id,
                    "runtime": target.runtime,
                    "profile": target.profile,
                    "delivery": target.delivery,
                    "wake": mail.wake and mail.wake_state is None,
                }
            )
    return found


async def reserve_wake(
    db: AsyncSession,
    device: AgentDevice,
    members: set[UUID],
    session_id: UUID,
    message_id: UUID,
) -> bool:
    """锁住收件会话并检查消息授权，空闲时登记一次唤醒尝试。

    返回 True 表示本事务取得发起唤醒的资格，客户端仍需回报实际唤醒结果。
    """
    session = await local_session(db, device, session_id, lock=True)
    await expire(db, session_id)
    row = await db.get(AgentMail, message_id)
    if (
        row is None
        or row.recipient_id != session_id
        or row.state != "pending"
        or not row.wake
        or row.wake_state is not None
        or session.delivery == "manual"
        or not await _authorized_mail(db, row, members, lock_task=True)
    ):
        return False

    # 未完成的领取和仍有效的唤醒尝试都占用会话，避免向原生会话重复注入通知。
    occupied = await db.scalar(
        select(AgentMail.id)
        .where(
            AgentMail.recipient_id == session_id,
            AgentMail.state.in_(_SESSION_OCCUPYING_CLAIM_STATES)
            | (
                (AgentMail.state == "pending")
                & AgentMail.wake_state.in_(("attempting", "accepted"))
                & (AgentMail.wake_at > datetime.now(UTC) - WAKE_TTL)
            ),
        )
        .limit(1)
    )
    if occupied:
        return False
    row.wake_state, row.wake_at = "attempting", datetime.now(UTC)
    return True


async def finish_wake(
    db: AsyncSession,
    device: AgentDevice,
    session_id: UUID,
    message_id: UUID,
    state: str,
) -> None:
    await local_session(db, device, session_id, lock=True)
    row = await db.get(AgentMail, message_id)
    if row is None or row.recipient_id != session_id:
        raise unavailable()
    if row.wake_state == "attempting":
        row.wake_state = state
    elif row.wake_state != state:
        raise conflict()


async def claim(
    db: AsyncSession,
    device: AgentDevice,
    members: set[UUID],
    session_id: UUID,
    key: str,
    message_id: UUID | None,
) -> dict[str, object] | None:
    """在收件会话锁内恢复原领取，或为当前会话登记一条新领取。

    重试沿用原回执并重新核对权限。新领取按入队顺序查找可访问消息，
    返回含正文的领取回执。无可领取消息时返回 None，会话已有未完成领取时返回冲突。
    """
    await local_session(db, device, session_id, lock=True)
    await expire(db, session_id)
    scope = (AgentMail.workspace_id == device.workspace_id, AgentMail.recipient_id == session_id)

    # 先恢复原领取，避免把同键重试当成抢占会话的新请求。
    prior_claim = await db.scalar(select(AgentMail).where(*scope, AgentMail.claim_key == key))
    if prior_claim is not None:
        if message_id and prior_claim.id != message_id:
            raise conflict()
        if prior_claim.state in _SESSION_OCCUPYING_CLAIM_STATES and await _authorized_mail(
            db, prior_claim, members, lock_task=True
        ):
            return receipt(prior_claim, body=True)
        return receipt(prior_claim) if await _authorized_mail(db, prior_claim, members) else None

    if await db.scalar(
        select(AgentMail.id).where(*scope, AgentMail.state.in_(_SESSION_OCCUPYING_CLAIM_STATES)).limit(1)
    ):
        raise AgentMailError(409, "AGENT_SESSION_HAS_ACTIVE_CLAIM", "请先处理当前会话已领取的消息")

    # 排队期间权限可能变化。取消已撤权的消息，再选择本次领取的消息。
    query = select(AgentMail).where(*scope, AgentMail.state == "pending")
    if message_id:
        query = query.where(AgentMail.id == message_id)
    for row in await db.scalars(query.order_by(AgentMail.created_at, AgentMail.id).limit(100)):
        if not await _authorized_mail(db, row, members, lock_task=True):
            row.state, row.resolved_at = "cancelled", datetime.now(UTC)
            continue
        row.state, row.claim_key, row.claimed_at = "claimed", key, datetime.now(UTC)
        return receipt(row, body=True)
    return None


async def acknowledge(
    db: AsyncSession,
    device: AgentDevice,
    members: set[UUID],
    session_id: UUID,
    message_id: UUID,
) -> dict[str, object]:
    await local_session(db, device, session_id, lock=True)
    row = await db.get(AgentMail, message_id)
    if row is None or row.recipient_id != session_id or not await _authorized_mail(db, row, members, lock_task=True):
        raise unavailable()
    if row.expects_reply or row.state not in {"claimed", "needs_confirmation", "acknowledged"}:
        raise conflict()
    if row.state != "acknowledged":
        row.state, row.resolved_at = "acknowledged", datetime.now(UTC)
    return receipt(row)


async def message_status(
    db: AsyncSession,
    device: AgentDevice,
    members: set[UUID],
    message_id: UUID,
) -> dict[str, object]:
    row = await db.get(AgentMail, message_id)
    if row is None or row.workspace_id != device.workspace_id:
        raise unavailable()
    local = await db.scalar(
        select(AgentSession.id)
        .where(
            AgentSession.device_id == device.id,
            AgentSession.id.in_((row.sender_id, row.recipient_id)),
        )
        .limit(1)
    )
    if not local or not await _authorized_mail(db, row, members):
        raise unavailable()
    return receipt(row)


async def cancel(
    db: AsyncSession,
    device: AgentDevice,
    session_id: UUID,
    message_id: UUID,
) -> dict[str, object]:
    # 领取和取消都锁住收件会话，结果不明的请求只能由发件端显式结束。
    row = await db.get(AgentMail, message_id)
    if row is None or row.sender_id != session_id or row.workspace_id != device.workspace_id:
        raise unavailable()
    await local_session(db, device, session_id)
    await db.scalar(select(AgentSession.id).where(AgentSession.id == row.recipient_id).with_for_update())
    await expire(db, row.recipient_id)
    await db.refresh(row)
    if row.state == "cancelled":
        return receipt(row)
    if row.state not in {"pending", "needs_confirmation"}:
        raise conflict()
    row.state, row.resolved_at = "cancelled", datetime.now(UTC)
    return receipt(row)


def evidence_view(row: AgentEvidence) -> dict[str, object]:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "title": row.title,
        "content": row.content,
        "created_at": row.created_at,
    }


async def publish(
    db: AsyncSession,
    device: AgentDevice,
    owner: PrincipalRecord,
    session_id: UUID,
    task_id: UUID,
    key: str,
    title: str,
    content: str,
) -> dict[str, object]:
    await local_session(db, device, session_id, lock=True)
    await task_access(db, owner, task_id, write=True)
    if not await db.get(AgentTaskSession, (task_id, session_id)):
        raise unavailable()
    row = await db.scalar(
        select(AgentEvidence).where(
            AgentEvidence.session_id == session_id,
            AgentEvidence.publish_key == key,
        )
    )
    if row:
        if (row.task_id, row.title, row.content) != (task_id, title, content):
            raise conflict()
        return evidence_view(row)
    row = AgentEvidence(
        id=uuid4(),
        task_id=task_id,
        session_id=session_id,
        publish_key=key,
        title=title,
        content=content,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    return evidence_view(row)
