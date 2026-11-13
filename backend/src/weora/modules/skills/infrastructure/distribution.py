"""设备分发、安装回执和使用查询的 PostgreSQL 实现。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import SQLColumnExpression, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.skills.application.ports import SkillVersionRecord
from weora.modules.skills.infrastructure.models import (
    SkillDeviceGrant,
    SkillInstallation,
    SkillInstallationReceipt,
    SkillModel,
    SkillUsageTurn,
    SkillVersionModel,
)
from weora.modules.skills.infrastructure.unit_of_work import version_from_model


class DistributionError(Exception):
    """分发状态与客户端操作冲突时返回稳定的业务错误。"""

    def __init__(self, code: str, status: int = 409) -> None:
        self.code = code
        self.status = status


@dataclass(frozen=True)
class DeviceActor:
    workspace_id: UUID
    device_id: UUID
    user_id: UUID
    display_name: str


@dataclass(frozen=True)
class InstallationChange:
    operation_id: UUID
    slot_id: UUID
    previous_id: UUID | None
    installation_id: UUID | None
    version_id: UUID | None
    runtime: str
    occurred_at: datetime


@dataclass(frozen=True)
class SkillObservation:
    installation_id: UUID
    runtime: str
    profile_id: str
    session_key: str
    turn_key: str
    kind: str
    occurred_at: datetime


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def manifest(db: AsyncSession, actor: DeviceActor) -> list[dict[str, str]]:
    rows = await db.execute(
        select(SkillModel.slug, SkillVersionModel)
        .join(SkillVersionModel, SkillVersionModel.id == SkillModel.latest_published_version_id)
        .where(
            SkillModel.workspace_id == actor.workspace_id,
            SkillModel.status == "active",
            SkillVersionModel.workspace_id == actor.workspace_id,
        )
        .order_by(SkillModel.slug)
    )
    return [descriptor(slug, version) for slug, version in rows]


def descriptor(slug: str, version: SkillVersionModel) -> dict[str, str]:
    return {
        "slug": slug,
        "version": version.version,
        "version_id": str(version.id),
        "package_sha256": version.package_sha256,
        "content_sha256": version.content_sha256,
    }


async def find_release(
    db: AsyncSession,
    actor: DeviceActor,
    *,
    version_id: UUID | None = None,
    slug: str | None = None,
    version: str | None = None,
) -> tuple[str, SkillVersionModel]:
    statement = (
        select(SkillModel.slug, SkillVersionModel)
        .join(SkillModel, SkillModel.id == SkillVersionModel.skill_id)
        .where(
            SkillModel.workspace_id == actor.workspace_id,
            SkillVersionModel.workspace_id == actor.workspace_id,
            SkillModel.status == "active",
        )
    )
    if version_id is not None:
        statement = statement.where(SkillVersionModel.id == version_id)
    else:
        statement = statement.where(SkillModel.slug == slug)
        if version is None:
            statement = statement.where(SkillVersionModel.id == SkillModel.latest_published_version_id)
        else:
            statement = statement.where(SkillVersionModel.version == version)
    row = (await db.execute(statement)).one_or_none()
    if row is None:
        raise DistributionError("SKILL_NOT_FOUND", 404)
    return row[0], row[1]


async def archive_version(db: AsyncSession, actor: DeviceActor, version_id: UUID) -> SkillVersionRecord:
    _, release = await find_release(db, actor, version_id=version_id)
    return version_from_model(release)


async def grant_cli_skill_sync(db: AsyncSession, workspace_id: UUID, device_id: UUID) -> None:
    """浏览器确认时为新设备开启同步和使用统计，后续由设备所有者调整。"""
    db.add(SkillDeviceGrant(device_id=device_id, workspace_id=workspace_id, usage_enabled=True))


async def lock_grant(db: AsyncSession, actor: DeviceActor) -> SkillDeviceGrant:
    grant = await db.scalar(
        select(SkillDeviceGrant)
        .where(
            SkillDeviceGrant.device_id == actor.device_id,
            SkillDeviceGrant.workspace_id == actor.workspace_id,
        )
        .with_for_update()
    )
    if grant is None:
        raise DistributionError("SKILL_DEVICE_NOT_AUTHORIZED", 403)
    return grant


async def record_installation(db: AsyncSession, actor: DeviceActor, change: InstallationChange) -> bool:
    """锁定设备授权行，使安装切换和幂等回执处在同一个事务内。"""
    await lock_grant(db, actor)
    fingerprint = sha256(repr(change).encode()).hexdigest()
    existing = await db.get(SkillInstallationReceipt, (actor.device_id, change.operation_id))
    if existing is not None:
        if existing.fingerprint != fingerprint:
            raise DistributionError("SKILL_RECEIPT_CONFLICT")
        return False
    current = await db.scalar(
        select(SkillInstallation).where(
            SkillInstallation.workspace_id == actor.workspace_id,
            SkillInstallation.device_id == actor.device_id,
            SkillInstallation.slot_id == change.slot_id,
            SkillInstallation.retired_at.is_(None),
        )
    )
    if change.previous_id != (current.id if current else None):
        raise DistributionError("SKILL_INSTALLATION_OUT_OF_ORDER")
    if current and (current.runtime != change.runtime or utc(current.activated_at) > change.occurred_at):
        raise DistributionError("SKILL_INSTALLATION_OUT_OF_ORDER")
    if change.occurred_at > datetime.now(UTC) + timedelta(minutes=10):
        raise DistributionError("SKILL_TIME_INVALID", 422)
    if change.installation_id is not None and change.version_id is not None:
        # 已落盘回执可以在归档后抵达，下载授权仍由 active 版本查询控制。
        version = await db.get(SkillVersionModel, change.version_id)
        if version is None or version.workspace_id != actor.workspace_id:
            raise DistributionError("SKILL_NOT_FOUND", 404)
        if await db.get(SkillInstallation, change.installation_id) is not None:
            raise DistributionError("SKILL_RECEIPT_CONFLICT")
        db.add(
            SkillInstallation(
                id=change.installation_id,
                workspace_id=actor.workspace_id,
                device_id=actor.device_id,
                user_id=actor.user_id,
                slot_id=change.slot_id,
                version_id=change.version_id,
                runtime=change.runtime,
                activated_at=change.occurred_at,
            )
        )
    elif current is None:
        raise DistributionError("SKILL_INSTALLATION_OUT_OF_ORDER")
    if current:
        current.retired_at = change.occurred_at
    db.add(
        SkillInstallationReceipt(
            device_id=actor.device_id,
            operation_id=change.operation_id,
            fingerprint=fingerprint,
            received_at=datetime.now(UTC),
        )
    )
    return True


async def record_usage(
    db: AsyncSession,
    actor: DeviceActor,
    observations: Sequence[SkillObservation],
) -> list[dict[str, str]]:
    """按项确认使用事件，失败项由设备按原因补报或丢弃。"""
    if not (await lock_grant(db, actor)).usage_enabled:
        raise DistributionError("SKILL_USAGE_DISABLED", 403)
    results = []
    now = datetime.now(UTC)
    for event in observations:
        if not now - timedelta(days=90) <= event.occurred_at <= now + timedelta(minutes=10):
            results.append({"status": "rejected", "code": "SKILL_TIME_INVALID"})
            continue
        installation = await db.scalar(
            select(SkillInstallation).where(
                SkillInstallation.id == event.installation_id,
                SkillInstallation.workspace_id == actor.workspace_id,
                SkillInstallation.device_id == actor.device_id,
                SkillInstallation.user_id == actor.user_id,
                SkillInstallation.runtime == event.runtime,
            )
        )
        if installation is None:
            results.append({"status": "retry", "code": "SKILL_INSTALLATION_UNCONFIRMED"})
            continue
        if event.occurred_at < utc(installation.activated_at) or (
            installation.retired_at and event.occurred_at >= utc(installation.retired_at)
        ):
            results.append({"status": "rejected", "code": "SKILL_INSTALLATION_NOT_ACTIVE"})
            continue
        key = {
            "device_id": actor.device_id,
            "runtime": event.runtime,
            "profile_id": event.profile_id,
            "session_key": event.session_key,
            "turn_key": event.turn_key,
            "version_id": installation.version_id,
        }
        row = await db.scalar(select(SkillUsageTurn).filter_by(**key))
        if row is None:
            row = SkillUsageTurn(
                id=uuid4(),
                workspace_id=actor.workspace_id,
                user_id=actor.user_id,
                used_at=event.occurred_at,
                received_at=now,
                **key,
            )
            db.add(row)
        field = "loaded_at" if event.kind == "loaded" else "mentioned_at"
        old = getattr(row, field)
        if old is not None and utc(old) <= event.occurred_at:
            results.append({"status": "duplicate"})
            continue
        setattr(row, field, event.occurred_at)
        row.used_at = min(utc(row.used_at), event.occurred_at)
        results.append({"status": "accepted"})
    return results


async def usage_summary(
    db: AsyncSession,
    workspace_id: UUID,
    skill_id: UUID,
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
    group_by: str,
    user_id: UUID | None,
    version_id: UUID | None,
    offset: int,
    limit: int,
) -> dict[str, object]:
    """将三个信号按各自发生时间归组，使用人数在每个统计范围重新去重。"""
    if not await db.scalar(
        select(SkillModel.id).where(
            SkillModel.workspace_id == workspace_id,
            SkillModel.id == skill_id,
        )
    ):
        raise DistributionError("SKILL_NOT_FOUND", 404)
    turn = SkillUsageTurn
    filters = [
        turn.workspace_id == workspace_id,
        SkillVersionModel.workspace_id == workspace_id,
        SkillVersionModel.skill_id == skill_id,
    ]
    if user_id:
        filters.append(turn.user_id == user_id)
    if version_id:
        if not await db.scalar(
            select(SkillVersionModel.id).where(
                SkillVersionModel.workspace_id == workspace_id,
                SkillVersionModel.skill_id == skill_id,
                SkillVersionModel.id == version_id,
            )
        ):
            raise DistributionError("SKILL_NOT_FOUND", 404)
        filters.append(turn.version_id == version_id)

    groups: dict[str, dict[str, object]] = {}
    totals: dict[str, object] = {"uses": 0, "users": 0, "mentions": 0, "loads": 0, "last_used_at": None}
    for field, metric in ((turn.used_at, "uses"), (turn.mentioned_at, "mentions"), (turn.loaded_at, "loads")):
        key: SQLColumnExpression[UUID] | SQLColumnExpression[str]
        if group_by == "user":
            key = turn.user_id
        elif group_by == "version":
            key = turn.version_id
        else:
            day = start.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
            buckets = []
            while day.astimezone(UTC) < end:
                following = day + timedelta(days=1)
                buckets.append((field < following.astimezone(UTC), day.date().isoformat()))
                day = following
            key = case(*buckets)
        base = (
            select(func.count(), func.count(func.distinct(turn.user_id)), func.max(field))
            .select_from(turn)
            .join(SkillVersionModel, SkillVersionModel.id == turn.version_id)
            .where(*filters, field >= start, field < end)
        )
        count, users, last = (await db.execute(base)).one()
        totals[metric] = count
        if metric == "uses":
            totals.update(users=users, last_used_at=last)
        for count, users, last, group_key in (await db.execute(base.add_columns(key).group_by(key))).tuples():
            group = groups.setdefault(
                str(group_key),
                {
                    "key": str(group_key),
                    "uses": 0,
                    "users": 0,
                    "mentions": 0,
                    "loads": 0,
                    "last_used_at": None,
                },
            )
            group[metric] = count
            if metric == "uses":
                group.update(users=users, last_used_at=last)
    ordered = [groups[key] for key in sorted(groups)]
    return {"totals": totals, "groups": ordered[offset : offset + limit], "total_groups": len(ordered)}
