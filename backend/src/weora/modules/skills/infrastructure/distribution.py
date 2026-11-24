"""设备分发、安装回执和使用查询的 PostgreSQL 实现。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import SQLColumnExpression, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Subquery

from weora.modules.skills.application.ports import SkillVersionRecord
from weora.modules.skills.infrastructure.models import (
    SkillDeviceGrant,
    SkillInstallation,
    SkillInstallationReceipt,
    SkillModel,
    SkillUsageTurn,
    SkillVersionModel,
)
from weora.modules.skills.infrastructure.skill_selection import (
    ranked_skill_sources,
    skill_tree_path,
    visible_skill_directories,
)
from weora.modules.skills.infrastructure.unit_of_work import version_from_model


class DistributionError(Exception):
    """分发状态与客户端操作冲突时返回稳定的业务错误。"""

    def __init__(self, code: str, status: int = 409, *, slug: str | None = None) -> None:
        self.code = code
        self.status = status
        self.slug = slug


@dataclass(frozen=True)
class DeviceActor:
    workspace_id: UUID
    device_id: UUID
    user_id: UUID
    display_name: str
    project_id: UUID | None = None
    skill_directories: tuple[str, ...] | None = None


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


@dataclass(frozen=True, slots=True)
class CapturedInstallation:
    slug: str
    namespace: str
    version_id: UUID


async def captured_installations(
    db: AsyncSession, actor: DeviceActor, runtime: str, observed: dict[UUID, datetime],
    *, usage: tuple[str, str] | None, captured_at: datetime,
) -> tuple[dict[UUID, CapturedInstallation], set[UUID]]:
    """仅核对当前设备和项目可读的历史安装，避免后续同步改写会话证据。"""
    install, version, skill = SkillInstallation, SkillVersionModel, SkillModel
    rows = await db.execute(select(
        install.id, install.activated_at, install.retired_at, skill.slug, skill.namespace, version.id,
    ).select_from(install).join(version, version.id == install.version_id).join(skill, skill.id == version.skill_id)
        .where(install.id.in_(observed), install.workspace_id == actor.workspace_id,
               version.workspace_id == actor.workspace_id, skill.workspace_id == actor.workspace_id,
               install.device_id == actor.device_id, install.user_id == actor.user_id,
               install.runtime == runtime, _visible_skills(actor)))
    found = {
        generation: CapturedInstallation(slug, namespace, version_id)
        for generation, activated, retired, slug, namespace, version_id in rows.tuples()
        if utc(activated) <= observed[generation] and (retired is None or observed[generation] < utc(retired))
    }
    loaded: set[UUID] = set()
    if found and usage is not None:
        turn = SkillUsageTurn
        loaded = set(await db.scalars(select(turn.version_id).where(
            turn.workspace_id == actor.workspace_id, turn.device_id == actor.device_id, turn.user_id == actor.user_id,
            turn.runtime == runtime, turn.profile_id == usage[0], turn.session_key == usage[1],
            turn.loaded_at <= captured_at, turn.version_id.in_({entry.version_id for entry in found.values()}),
        ).distinct()))
    return found, loaded


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _visible_skills(actor: DeviceActor) -> SQLColumnExpression[bool]:
    # 绑定项目可以同时同步共享 Skill 和当前项目 Skill。
    shared = SkillModel.project_id.is_(None)
    return shared if actor.project_id is None else or_(shared, SkillModel.project_id == actor.project_id)


async def manifest(db: AsyncSession, actor: DeviceActor) -> list[dict[str, str]]:
    sources = ranked_skill_sources(actor.workspace_id, actor.project_id, actor.skill_directories)
    rows = await db.execute(
        select(SkillModel.slug, skill_tree_path(), SkillVersionModel, sources.c.namespace_copies)
        .join(sources, sources.c.id == SkillModel.id)
        .join(SkillVersionModel, SkillVersionModel.id == SkillModel.latest_published_version_id)
        .where(sources.c.source_rank == 1, SkillVersionModel.workspace_id == actor.workspace_id)
        .order_by(SkillModel.slug)
    )
    releases: list[dict[str, str]] = []
    for slug, path, version, copies in rows:
        if copies > 1:
            # 整份清单未生成，客户端保留已安装的版本。
            raise DistributionError("SKILL_NAMESPACE_CONFLICT", slug=slug)
        releases.append(descriptor(slug, path, version))
    return releases


def descriptor(slug: str, path: str, version: SkillVersionModel) -> dict[str, str]:
    return {
        "slug": slug,
        "resource_path": path,
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
) -> tuple[str, str, SkillVersionModel]:
    skill, release = SkillModel, SkillVersionModel
    statement = select(skill.slug, skill_tree_path(), release).select_from(skill)
    scope = and_(
        skill.workspace_id == actor.workspace_id, _visible_skills(actor), skill.status == "active",
        visible_skill_directories(actor.skill_directories),
    )
    release_scope = and_(release.workspace_id == actor.workspace_id, release.skill_id == skill.id)
    if version_id is not None:
        by_id = statement.join(release, release_scope).where(scope, release.id == version_id)
        fixed = (await db.execute(by_id)).one_or_none()
        if fixed is None:
            raise DistributionError("SKILL_NOT_FOUND", 404)
        return fixed[0], fixed[1], fixed[2]

    sources = ranked_skill_sources(actor.workspace_id, actor.project_id, actor.skill_directories)
    target = release.id == skill.latest_published_version_id if version is None else release.version == version
    # 先解析交付资产，再在该资产查旧版本，避免回落到同名共享资产。
    row = (await db.execute(
        statement.add_columns(sources.c.namespace_copies).join(sources, sources.c.id == skill.id)
        .outerjoin(release, release_scope & target).where(sources.c.source_rank == 1, sources.c.slug == slug)
    )).one_or_none()
    if row is not None and row[3] > 1:
        raise DistributionError("SKILL_NAMESPACE_CONFLICT", slug=slug)
    if row is None or row[2] is None:
        raise DistributionError("SKILL_NOT_FOUND", 404)
    return row[0], row[1], row[2]


async def archive_version(db: AsyncSession, actor: DeviceActor, version_id: UUID) -> SkillVersionRecord:
    _, _, release = await find_release(db, actor, version_id=version_id)
    return version_from_model(release)


@dataclass(frozen=True, slots=True)
class ContributionOrigin:
    """设备已获授权的固定发布版本及其所属资产目录。"""

    skill_id: UUID
    project_id: UUID | None
    namespace: str
    slug: str
    version: str


async def contribution_origin(
    db: AsyncSession, actor: DeviceActor, version_id: UUID,
) -> ContributionOrigin:
    """按已交付版本查找原资产范围，同名项目副本不会改变提交目标。"""
    slug, path, release = await find_release(db, actor, version_id=version_id)
    row = (await db.execute(select(SkillModel.project_id).where(
        SkillModel.id == release.skill_id, SkillModel.workspace_id == actor.workspace_id,
    ))).one_or_none()
    if row is None:
        raise DistributionError("SKILL_NOT_FOUND", 404)
    namespace = path.removeprefix("skills/").rpartition("/")[0]
    return ContributionOrigin(release.skill_id, row[0], namespace, slug, release.version)


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
        # 已提交的安装及移除仍需验证关联代次。项目 ID 变化时不能重放旧回执。
        generations = {item for item in (change.installation_id, change.previous_id) if item is not None}
        readable = await db.scalars(
            select(SkillInstallation.id)
            .join(SkillVersionModel, SkillVersionModel.id == SkillInstallation.version_id)
            .join(SkillModel, SkillModel.id == SkillVersionModel.skill_id)
            .where(
                _visible_skills(actor),
                SkillInstallation.workspace_id == actor.workspace_id,
                SkillInstallation.device_id == actor.device_id,
                SkillInstallation.id.in_(generations),
            )
        )
        if set(readable) != generations:
            raise DistributionError("SKILL_NOT_FOUND", 404)
        return False
    active = (await db.execute(
        select(SkillInstallation, SkillModel.project_id)
        .join(SkillVersionModel, SkillVersionModel.id == SkillInstallation.version_id)
        .join(SkillModel, SkillModel.id == SkillVersionModel.skill_id)
        .where(
            SkillInstallation.workspace_id == actor.workspace_id,
            SkillInstallation.device_id == actor.device_id,
            SkillInstallation.slot_id == change.slot_id,
            SkillInstallation.retired_at.is_(None),
        )
    )).one_or_none()
    current, current_project = active if active else (None, None)
    if current_project is not None and current_project != actor.project_id:
        raise DistributionError("SKILL_INSTALLATION_OUT_OF_ORDER")
    if change.previous_id != (current.id if current else None):
        raise DistributionError("SKILL_INSTALLATION_OUT_OF_ORDER")
    if current and (current.runtime != change.runtime or utc(current.activated_at) > change.occurred_at):
        raise DistributionError("SKILL_INSTALLATION_OUT_OF_ORDER")
    if change.occurred_at > datetime.now(UTC) + timedelta(minutes=10):
        raise DistributionError("SKILL_TIME_INVALID", 422)
    if change.installation_id is not None and change.version_id is not None:
        # 已落盘回执可以在归档后抵达，下载授权仍由 active 版本查询控制。
        version = await db.scalar(
            select(SkillVersionModel.id)
            .join(SkillModel, SkillModel.id == SkillVersionModel.skill_id)
            .where(
                _visible_skills(actor),
                SkillVersionModel.workspace_id == actor.workspace_id,
                SkillVersionModel.id == change.version_id,
            )
        )
        if version is None:
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
            select(SkillInstallation)
            .join(SkillVersionModel, SkillVersionModel.id == SkillInstallation.version_id)
            .join(SkillModel, SkillModel.id == SkillVersionModel.skill_id)
            .where(
                _visible_skills(actor),
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


def _team_usage_observations(
    workspace_id: UUID, project_id: UUID | None, start: datetime, end: datetime,
) -> Subquery:
    """复用分发记录的轮次去重和 Skill 所属范围。"""
    turn, version, skill = SkillUsageTurn, SkillVersionModel, SkillModel
    return (
        select(turn.user_id, skill.id.label("skill_id"), skill.slug, skill.display_name, turn.used_at)
        .select_from(turn)
        .join(version, version.id == turn.version_id)
        .join(skill, skill.id == version.skill_id)
        .where(
            turn.workspace_id == workspace_id, version.workspace_id == workspace_id,
            skill.workspace_id == workspace_id, skill.project_id == project_id,
            turn.used_at >= start, turn.used_at < end,
        )
        .subquery()
    )


async def team_usage_digest(
    db: AsyncSession, workspace_id: UUID, project_id: UUID | None, start: datetime, end: datetime,
) -> dict[str, object]:
    """供已校验管理员权限的团队周报读取。"""
    observed = _team_usage_observations(workspace_id, project_id, start, end)
    totals = (await db.execute(select(
        func.count(), func.count(func.distinct(observed.c.user_id)), func.count(func.distinct(observed.c.skill_id)),
    ).select_from(observed))).one()
    uses = func.count().label("uses")
    ranked = await db.execute(select(
        observed.c.skill_id, observed.c.slug, observed.c.display_name, uses,
    ).group_by(observed.c.skill_id, observed.c.slug, observed.c.display_name)
        .order_by(uses.desc(), observed.c.slug, observed.c.skill_id).limit(10))
    return {
        "uses": totals[0], "members": totals[1], "skills": totals[2],
        "top": [
            {"id": str(identifier), "slug": slug, "name": title, "uses": count}
            for identifier, slug, title, count in ranked.tuples()
        ],
    }


async def team_skill_adoption(
    db: AsyncSession, workspace_id: UUID, project_id: UUID | None, start: datetime, end: datetime, limit: int,
) -> dict[str, object]:
    """按已发布资产对照窗口内的实际使用记录，包括仍被使用的已归档资产。"""
    skill = SkillModel
    observed = _team_usage_observations(workspace_id, project_id, start, end)
    usage = (
        select(
            observed.c.skill_id, func.count().label("uses"),
            func.count(func.distinct(observed.c.user_id)).label("members"),
            func.max(observed.c.used_at).label("last_used_at"),
        ).group_by(observed.c.skill_id).subquery()
    )
    statement = select(
        skill.id, skill.slug, skill.display_name, skill.status,
        func.coalesce(usage.c.uses, 0), func.coalesce(usage.c.members, 0), usage.c.last_used_at,
    ).outerjoin(usage, usage.c.skill_id == skill.id).where(
        skill.workspace_id == workspace_id, skill.project_id == project_id,
        (skill.latest_published_version_id.is_not(None) & (skill.status == "active"))
        | usage.c.uses.is_not(None),
    ).order_by(usage.c.members.desc().nulls_last(), usage.c.last_used_at.desc().nulls_last(), skill.slug, skill.id)
    groups: dict[str, list[dict[str, object]]] = {"adopted": [], "unused": [], "archived_in_use": []}
    totals = dict.fromkeys(groups, 0)
    rows = await db.stream(statement)
    async for identity, slug, title, status, uses, members, last in rows.tuples():
        group = "archived_in_use" if status == "archived" else "adopted" if uses else "unused"
        totals[group] += 1
        if len(groups[group]) < limit:
            groups[group].append({
                "id": str(identity), "slug": slug, "name": title, "uses": uses, "members": members,
                "last_used_at": utc(last).isoformat() if last is not None else None,
            })
    return {
        "since": start.isoformat(), "until": end.isoformat(),
        **{group: {"total": totals[group], "items": items} for group, items in groups.items()},
    }


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
    *,
    project_id: UUID | None = None,
) -> dict[str, object]:
    """按资产所属范围汇总使用信号，并在每个统计范围重新去重成员。"""
    if not await db.scalar(
        select(SkillModel.id).where(
            SkillModel.workspace_id == workspace_id,
            SkillModel.project_id == project_id,
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
