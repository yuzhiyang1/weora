"""工作空间成员和一次性邀请的 HTTP 入口。"""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator

from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.api.routes import _identity_service, _set_auth_cookies, _settings
from weora.modules.identity.api.schemas import PrincipalResponse
from weora.modules.identity.application.errors import AuthenticationRequiredError, EmailUnavailableError, IdentityError
from weora.modules.identity.application.invitations import (
    InviteNotConfigured,
    InviteStatus,
    InviteUnavailable,
    WorkspaceAccessDenied,
    WorkspaceInvite,
    WorkspaceMember,
    WorkspaceRecord,
)
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.identity.application.service import RegisterCommand
from weora.shared.api.request_id import get_request_id

router = APIRouter(prefix="/auth", tags=["Workspace membership"])
ReadActor = Annotated[PrincipalRecord, Depends(current_principal)]
WriteActor = Annotated[PrincipalRecord, Depends(write_principal)]


class CreateInviteRequest(BaseModel):
    expires_in_hours: Literal[24, 168] = 168


class InviteTokenRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)


class InviteRegistrationRequest(InviteTokenRequest):
    display_name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=15, max_length=128)

    @field_validator("display_name")
    @classmethod
    def reject_blank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请输入你的称呼")
        return value.strip()


class InvitePreviewResponse(BaseModel):
    workspace_name: str
    inviter_name: str


class InviteRecordResponse(BaseModel):
    id: UUID
    creator_name: str
    created_at: datetime
    expires_at: datetime
    status: InviteStatus
    used_by_name: str | None

    @classmethod
    def from_record(cls, record: WorkspaceInvite) -> "InviteRecordResponse":
        return cls(
            id=record.id,
            creator_name=record.creator_name,
            created_at=record.created_at.astimezone(UTC)
            if record.created_at.tzinfo
            else record.created_at.replace(tzinfo=UTC),
            expires_at=record.expires_at.astimezone(UTC)
            if record.expires_at.tzinfo
            else record.expires_at.replace(tzinfo=UTC),
            status=record.status(datetime.now(UTC)),
            used_by_name=record.used_by_name,
        )


class InviteLinkResponse(BaseModel):
    token: str


class CreatedInviteResponse(InviteLinkResponse):
    invitation: InviteRecordResponse


def _session(request: Request) -> str:
    token = request.cookies.get(_settings(request).session_cookie_name)
    if not token:
        raise AuthenticationRequiredError
    return token


def _http_error(error: IdentityError) -> HTTPException:
    if isinstance(error, WorkspaceAccessDenied):
        return HTTPException(403, detail={"code": "WORKSPACE_ACCESS_DENIED", "message": "没有权限执行此工作空间操作。"})
    if isinstance(error, InviteUnavailable):
        return HTTPException(
            410, detail={"code": "INVITE_UNAVAILABLE", "message": "此邀请链接已失效，请联系邀请人获取新链接。"}
        )
    if isinstance(error, InviteNotConfigured):
        return HTTPException(
            503, detail={"code": "INVITE_NOT_CONFIGURED", "message": "工作空间邀请尚未启用，请联系管理员。"}
        )
    if isinstance(error, EmailUnavailableError):
        return HTTPException(
            409, detail={"code": "EMAIL_UNAVAILABLE", "message": "该邮箱已有账号或不可注册，请登录后加入。"}
        )
    if isinstance(error, AuthenticationRequiredError):
        return HTTPException(401, detail={"code": "AUTHENTICATION_REQUIRED", "message": "请重新登录。"})
    raise error


@router.get("/workspaces")
async def list_workspaces(request: Request, actor: ReadActor) -> list[WorkspaceRecord]:
    return await _identity_service(request).list_workspaces(actor)


@router.post("/workspaces/{workspace_id}/activate")
async def activate_workspace(workspace_id: UUID, request: Request, actor: WriteActor) -> PrincipalResponse:
    try:
        principal = await _identity_service(request).switch_workspace(actor, workspace_id, _session(request))
    except IdentityError as error:
        raise _http_error(error) from error
    return PrincipalResponse.from_record(principal)


@router.get("/workspaces/{workspace_id}/members")
async def list_members(workspace_id: UUID, request: Request, actor: ReadActor) -> list[WorkspaceMember]:
    try:
        return await _identity_service(request).list_members(actor, workspace_id)
    except IdentityError as error:
        raise _http_error(error) from error


@router.delete("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
async def remove_member(workspace_id: UUID, user_id: UUID, request: Request, actor: WriteActor) -> None:
    try:
        await _identity_service(request).remove_member(actor, workspace_id, user_id, get_request_id() or "unknown")
    except IdentityError as error:
        raise _http_error(error) from error


@router.get("/workspaces/{workspace_id}/invitations")
async def list_invitations(workspace_id: UUID, request: Request, actor: ReadActor) -> list[InviteRecordResponse]:
    try:
        records = await _identity_service(request).list_invites(actor, workspace_id)
    except IdentityError as error:
        raise _http_error(error) from error
    return [InviteRecordResponse.from_record(record) for record in records]


@router.post("/workspaces/{workspace_id}/invitations", status_code=201)
async def create_invitation(
    workspace_id: UUID,
    payload: CreateInviteRequest,
    request: Request,
    response: Response,
    actor: WriteActor,
) -> CreatedInviteResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        record, token = await _identity_service(request).create_invite(
            actor,
            workspace_id,
            payload.expires_in_hours,
            get_request_id() or "unknown",
        )
    except IdentityError as error:
        raise _http_error(error) from error
    return CreatedInviteResponse(invitation=InviteRecordResponse.from_record(record), token=token)


@router.post("/workspaces/{workspace_id}/invitations/{invite_id}/copy")
async def copy_invitation(
    workspace_id: UUID,
    invite_id: UUID,
    request: Request,
    response: Response,
    actor: WriteActor,
) -> InviteLinkResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        return InviteLinkResponse(token=await _identity_service(request).copy_invite(actor, workspace_id, invite_id))
    except IdentityError as error:
        raise _http_error(error) from error


@router.delete("/workspaces/{workspace_id}/invitations/{invite_id}", status_code=204)
async def revoke_invitation(workspace_id: UUID, invite_id: UUID, request: Request, actor: WriteActor) -> None:
    try:
        await _identity_service(request).revoke_invite(actor, workspace_id, invite_id, get_request_id() or "unknown")
    except IdentityError as error:
        raise _http_error(error) from error


@router.post("/invitations/preview")
async def preview_invitation(
    payload: InviteTokenRequest, request: Request, response: Response
) -> InvitePreviewResponse:
    # 令牌放在 POST 正文中，避免出现在代理访问日志、Referer 和可缓存 URL 中。
    response.headers["Cache-Control"] = "no-store"
    try:
        invite = await _identity_service(request).preview_invite(payload.token)
    except IdentityError as error:
        raise _http_error(error) from error
    return InvitePreviewResponse(workspace_name=invite.workspace_name, inviter_name=invite.creator_name)


@router.post("/invitations/accept")
async def accept_invitation(
    payload: InviteTokenRequest,
    request: Request,
    actor: WriteActor,
) -> PrincipalResponse:
    try:
        principal = await _identity_service(request).accept_invite(
            actor,
            payload.token,
            _session(request),
            get_request_id() or "unknown",
        )
    except IdentityError as error:
        raise _http_error(error) from error
    return PrincipalResponse.from_record(principal)


@router.post("/invitations/register", status_code=201)
async def register_from_invitation(
    payload: InviteRegistrationRequest,
    request: Request,
    response: Response,
) -> PrincipalResponse:
    try:
        result = await _identity_service(request).register(
            RegisterCommand(
                display_name=payload.display_name,
                email=str(payload.email),
                password=payload.password,
                workspace_name="",
                request_id=get_request_id() or "unknown",
                invite_token=payload.token,
            )
        )
    except IdentityError as error:
        raise _http_error(error) from error
    _set_auth_cookies(
        response,
        settings=_settings(request),
        session_token=result.session_token,
        csrf_token=result.csrf_token,
    )
    return PrincipalResponse.from_record(result.principal)
