"""Skill 分发设备入口与控制台查询。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.collaboration.application.agent_mail import AgentMailError
from weora.modules.collaboration.infrastructure import agent_sessions
from weora.modules.collaboration.infrastructure.agent_mail import AgentDevice
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.skills.api.routes import PackageUpload, _actor, _raise_http_error, _service, read_skill_upload
from weora.modules.skills.application.service import SkillActor
from weora.modules.skills.domain.errors import SkillRegistryError
from weora.modules.skills.infrastructure import distribution
from weora.modules.skills.infrastructure.models import SkillDeviceGrant
from weora.shared.api.request_id import get_request_id
from weora.shared.infrastructure.database import SessionFactory

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["Skill distribution"])
ReadPrincipal = Annotated[PrincipalRecord, Depends(current_principal)]
WritePrincipal = Annotated[PrincipalRecord, Depends(write_principal)]
Bearer = Annotated[str | None, Header(alias="Authorization")]
Runtime = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class DeviceGrantInput(BaseModel):
    usage_enabled: bool = False


class ReceiptInput(BaseModel):
    operation_id: UUID
    slot_id: UUID
    previous_id: UUID | None = None
    installation_id: UUID | None = None
    version_id: UUID | None = None
    runtime: Runtime
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def complete_version(self) -> "ReceiptInput":
        if (self.installation_id is None) != (self.version_id is None):
            raise ValueError("安装代次与版本必须同时提供")
        return self


class ObservationInput(BaseModel):
    installation_id: UUID
    runtime: Runtime
    profile_id: Fingerprint
    session_key: Fingerprint
    turn_key: Fingerprint
    kind: Literal["mentioned", "loaded"]
    occurred_at: AwareDatetime


class BatchInput(BaseModel):
    events: list[ObservationInput] = Field(min_length=1, max_length=100)


def factory(request: Request) -> SessionFactory:
    return cast(SessionFactory, request.app.state.db_session_factory)


def error_response(error: distribution.DistributionError) -> HTTPException:
    messages = {
        "SKILL_DEVICE_NOT_AUTHORIZED": "请先在控制台授权此设备同步 Skill。",
        "SKILL_USAGE_DISABLED": "此设备的 Skill 使用统计已关闭。",
        "SKILL_NOT_FOUND": "Skill 或版本不可用。",
    }
    return HTTPException(
        error.status,
        detail={"code": error.code, "message": messages.get(error.code, "Skill 分发请求无法处理。")},
    )


@asynccontextmanager
async def device(
    request: Request,
    workspace_id: UUID,
    authorization: str | None,
) -> AsyncIterator[tuple[AsyncSession, distribution.DeviceActor]]:
    async with factory(request)() as db, db.begin():
        try:
            bound, principal = await agent_sessions.authenticate(db, workspace_id, authorization)
            actor = distribution.DeviceActor(workspace_id, bound.id, principal.user_id, principal.display_name)
            await distribution.lock_grant(db, actor)
            yield db, actor
        except AgentMailError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
        except distribution.DistributionError as exc:
            raise error_response(exc) from exc
        except SkillRegistryError as exc:
            _raise_http_error(exc)


@router.post("/skill-distribution/contributions", status_code=201)
async def upload_contribution(
    workspace_id: UUID, request: Request, package: PackageUpload, authorization: Bearer = None,
) -> dict[str, object]:
    """已授权设备可代表在职成员提交完整包，身份始终由服务端验证。"""
    async with factory(request)() as db:
        try:
            bound, principal = await agent_sessions.authenticate(db, workspace_id, authorization)
            await distribution.lock_grant(
                db, distribution.DeviceActor(workspace_id, bound.id, principal.user_id, principal.display_name)
            )
        except AgentMailError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
        except distribution.DistributionError as exc:
            raise error_response(exc) from exc
    package_name, archive = await read_skill_upload(package)
    try:
        skill_id, slug, contribution_id = await _service(request).contribute(
            _actor(principal), workspace_id, package_name, archive, get_request_id() or "unknown",
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return {
        "skill_id": skill_id, "slug": slug, "contribution_id": contribution_id,
        "status": "pending_review" if contribution_id else "draft",
    }


@router.put("/skill-distribution/devices/{device_id}/grant")
async def grant_device(
    workspace_id: UUID,
    device_id: UUID,
    payload: DeviceGrantInput,
    request: Request,
    principal: WritePrincipal,
) -> dict[str, object]:
    if principal.workspace_id != workspace_id:
        raise HTTPException(404)
    async with factory(request)() as db, db.begin():
        bound = await db.get(AgentDevice, device_id, with_for_update=True)
        if not bound or bound.workspace_id != workspace_id or bound.user_id != principal.user_id:
            raise HTTPException(404)
        if not agent_sessions.device_active(bound):
            raise HTTPException(409, detail={"code": "SKILL_DEVICE_INACTIVE", "message": "请先配对设备。"})
        grant = await db.get(SkillDeviceGrant, device_id, with_for_update=True)
        if grant is None:
            db.add(
                SkillDeviceGrant(
                    device_id=device_id,
                    workspace_id=workspace_id,
                    usage_enabled=payload.usage_enabled,
                )
            )
        else:
            grant.usage_enabled = payload.usage_enabled
    return {"device_id": device_id, "usage_enabled": payload.usage_enabled}


@router.get("/skill-distribution/grants")
async def list_grants(workspace_id: UUID, request: Request, principal: ReadPrincipal) -> dict[str, object]:
    if principal.workspace_id != workspace_id:
        raise HTTPException(404)
    async with factory(request)() as db:
        grants = await db.scalars(
            select(SkillDeviceGrant)
            .join(AgentDevice, AgentDevice.id == SkillDeviceGrant.device_id)
            .where(
                SkillDeviceGrant.workspace_id == workspace_id,
                AgentDevice.user_id == principal.user_id,
            )
        )
        return {"items": [{"device_id": grant.device_id, "usage_enabled": grant.usage_enabled} for grant in grants]}


@router.delete("/skill-distribution/devices/{device_id}/grant", status_code=204)
async def revoke_grant(workspace_id: UUID, device_id: UUID, request: Request, principal: WritePrincipal) -> None:
    if principal.workspace_id != workspace_id:
        raise HTTPException(404)
    async with factory(request)() as db, db.begin():
        bound = await db.get(AgentDevice, device_id)
        if not bound or bound.workspace_id != workspace_id or bound.user_id != principal.user_id:
            raise HTTPException(404)
        grant = await db.get(SkillDeviceGrant, device_id, with_for_update=True)
        if grant:
            await db.delete(grant)


@router.get("/skill-distribution/manifest")
async def list_manifest(
    workspace_id: UUID,
    request: Request,
    runtime: Runtime,
    scope: Literal["project", "user"] = "project",
    authorization: Bearer = None,
) -> dict[str, object]:
    async with device(request, workspace_id, authorization) as (db, actor):
        grant = await distribution.lock_grant(db, actor)
        return {
            "items": await distribution.manifest(db, actor),
            "complete": True,
            "device_id": actor.device_id,
            "runtime": runtime,
            "scope": scope,
            "usage_enabled": grant.usage_enabled,
        }


@router.get("/skill-distribution/resolve")
async def resolve_version(
    workspace_id: UUID,
    request: Request,
    slug: Annotated[str, Query(min_length=1, max_length=64)],
    version: Annotated[str | None, Query(max_length=64)] = None,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with device(request, workspace_id, authorization) as (db, actor):
        name, release = await distribution.find_release(db, actor, slug=slug, version=version)
        return distribution.descriptor(name, release) | {
            "size": release.package_size,
            "files": release.manifest,
        }


@router.get("/skill-distribution/versions/{version_id}/package")
async def download_package(
    workspace_id: UUID,
    version_id: UUID,
    request: Request,
    authorization: Bearer = None,
) -> Response:
    async with device(request, workspace_id, authorization) as (db, actor):
        record = await distribution.archive_version(db, actor, version_id)
        download = await _service(request).download_record(
            SkillActor(actor.user_id, workspace_id, "member", actor.display_name),
            record,
            get_request_id() or "unknown",
        )
    return Response(
        download.archive,
        media_type="application/zip",
        headers={"X-Content-SHA256": download.package_sha256, "X-Content-Type-Options": "nosniff"},
    )


@router.post("/skill-distribution/installation-receipts")
async def installation_receipt(
    workspace_id: UUID,
    payload: ReceiptInput,
    request: Request,
    authorization: Bearer = None,
) -> dict[str, str]:
    async with device(request, workspace_id, authorization) as (db, actor):
        change = distribution.InstallationChange(
            **(payload.model_dump() | {"occurred_at": payload.occurred_at.astimezone(UTC)}),
        )
        created = await distribution.record_installation(db, actor, change)
        return {"status": "accepted" if created else "duplicate"}


@router.post("/skill-distribution/usage-events/batch")
async def upload_usage(
    workspace_id: UUID,
    payload: BatchInput,
    request: Request,
    authorization: Bearer = None,
) -> dict[str, object]:
    observations = [
        distribution.SkillObservation(**(item.model_dump() | {"occurred_at": item.occurred_at.astimezone(UTC)}))
        for item in payload.events
    ]
    async with device(request, workspace_id, authorization) as (db, actor):
        return {"items": await distribution.record_usage(db, actor, observations)}


@router.get("/skills/{skill_id}/usage")
async def stats(
    workspace_id: UUID,
    skill_id: UUID,
    request: Request,
    principal: ReadPrincipal,
    start: Annotated[AwareDatetime, Query(alias="from")],
    end: Annotated[AwareDatetime, Query(alias="to")],
    timezone: Annotated[str, Query(max_length=64)] = "UTC",
    group_by: Literal["day", "user", "version"] = "day",
    user_id: UUID | None = None,
    version_id: UUID | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, object]:
    if principal.workspace_id != workspace_id:
        raise HTTPException(404)
    if principal.role not in {"owner", "admin"}:
        if user_id and user_id != principal.user_id:
            raise HTTPException(403)
        user_id = principal.user_id
    if end <= start or end - start > timedelta(days=93):
        raise HTTPException(422, detail={"code": "SKILL_TIME_INVALID", "message": "统计范围须在 93 天内。"})
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(422, detail={"code": "SKILL_TIMEZONE_INVALID", "message": "时区无效。"}) from exc
    async with factory(request)() as db:
        try:
            return await distribution.usage_summary(
                db,
                workspace_id,
                skill_id,
                start.astimezone(UTC),
                end.astimezone(UTC),
                zone,
                group_by,
                user_id,
                version_id,
                offset,
                limit,
            )
        except distribution.DistributionError as exc:
            raise error_response(exc) from exc
