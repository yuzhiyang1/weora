"""Identity HTTP 路由。"""

import base64
import binascii
import struct
from datetime import timedelta
from typing import Annotated, cast

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from weora.bootstrap.settings import AppSettings
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.api.schemas import (
    ErrorResponse,
    LoginRequest,
    PrincipalResponse,
    RegisterRequest,
)
from weora.modules.identity.application.errors import (
    AuthenticationRequiredError,
    CsrfRejectedError,
    EmailUnavailableError,
    InvalidCredentialsError,
    LoginRateLimitedError,
    RegistrationDisabledError,
)
from weora.modules.identity.application.invitations import InviteNotConfigured, InviteUnavailable, NoActiveWorkspace
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.identity.application.service import IdentityService, LoginCommand, RegisterCommand
from weora.shared.api.request_id import get_request_id

router = APIRouter(prefix="/auth", tags=["Identity"])


class ProfileInput(BaseModel):
    """头像由浏览器缩放并重新编码，服务端仍独立限制格式和尺寸。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    display_name: str = Field(min_length=1, max_length=80)
    avatar: str | None = Field(default=None, max_length=350000)

    @field_validator("display_name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("名称不能为空")
        return value.strip()

    @field_validator("avatar")
    @classmethod
    def png_only(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            if not value.startswith("data:image/png;base64,"):
                raise ValueError
            raw = base64.b64decode(value.split(",", 1)[1], validate=True)
            if len(raw) > 256000 or raw[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
                raise ValueError
            width, height = struct.unpack(">II", raw[16:24])
            if not (1 <= width <= 512 and 1 <= height <= 512):
                raise ValueError
        except (ValueError, binascii.Error, struct.error):
            raise ValueError("头像必须是 512px 以内、250KB 以内的 PNG") from None
        return value


@router.put("/profile", response_model=PrincipalResponse)
async def update_profile(
    payload: ProfileInput, request: Request, principal: Annotated[PrincipalRecord, Depends(write_principal)]
) -> PrincipalResponse:
    """更新本人资料，不能修改邮箱、团队角色或其他用户。"""
    record = await _identity_service(request).update_profile(
        principal,
        payload.display_name,
        payload.avatar,
        "avatar" in payload.model_fields_set,
        get_request_id() or "unknown",
    )
    return PrincipalResponse.from_record(record)


@router.get("/avatar")
async def get_avatar(request: Request, principal: Annotated[PrincipalRecord, Depends(current_principal)]) -> Response:
    """私人头像需要有效会话，不提供任意用户枚举或公开缓存。"""
    value = await _identity_service(request).avatar(principal.user_id)
    if value is None:
        raise HTTPException(404, "未设置头像")
    return Response(
        base64.b64decode(value.split(",", 1)[1]),
        media_type="image/png",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


def _identity_service(request: Request) -> IdentityService:
    return cast(IdentityService, request.app.state.identity_service)


def _settings(request: Request) -> AppSettings:
    return cast(AppSettings, request.app.state.settings)


def _set_auth_cookies(
    response: Response,
    *,
    settings: AppSettings,
    session_token: str,
    csrf_token: str,
) -> None:
    """集中设置 Cookie 属性，避免不同认证入口出现安全差异。"""
    max_age = int(timedelta(days=settings.session_ttl_days).total_seconds())
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_token,
        max_age=max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post(
    "/register",
    response_model=PrincipalResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
async def register(payload: RegisterRequest, request: Request, response: Response) -> PrincipalResponse:
    """创建账户、首个工作空间和浏览器会话。"""
    try:
        result = await _identity_service(request).register(
            RegisterCommand(
                display_name=payload.display_name,
                email=str(payload.email),
                password=payload.password,
                workspace_name=payload.workspace_name,
                request_id=get_request_id() or "unknown",
            )
        )
    except RegistrationDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "REGISTRATION_DISABLED", "message": "当前部署未开放自主注册"},
        ) from exc
    except EmailUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "EMAIL_UNAVAILABLE", "message": "该邮箱不可用于注册"},
        ) from exc

    _set_auth_cookies(
        response,
        settings=_settings(request),
        session_token=result.session_token,
        csrf_token=result.csrf_token,
    )
    return PrincipalResponse.from_record(result.principal)


@router.post(
    "/login",
    response_model=PrincipalResponse,
    responses={status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}},
)
async def login(payload: LoginRequest, request: Request, response: Response) -> PrincipalResponse:
    """使用邮箱和密码创建新会话。"""
    try:
        result = await _identity_service(request).login(
            LoginCommand(
                email=str(payload.email),
                password=payload.password,
                request_id=get_request_id() or "unknown",
                invite_token=payload.invite_token,
            )
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CREDENTIALS", "message": "邮箱或密码不正确"},
        ) from exc
    except LoginRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "LOGIN_RATE_LIMITED", "message": "登录尝试过多，请稍后再试"},
        ) from exc
    except NoActiveWorkspace as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "NO_ACTIVE_WORKSPACE", "message": "账户没有可访问的工作空间，请通过新邀请加入。"},
        ) from exc
    except (InviteUnavailable, InviteNotConfigured) as exc:
        raise HTTPException(
            status_code=410,
            detail={"code": "INVITE_UNAVAILABLE", "message": "此邀请链接已失效，请联系邀请人获取新链接。"},
        ) from exc

    _set_auth_cookies(
        response,
        settings=_settings(request),
        session_token=result.session_token,
        csrf_token=result.csrf_token,
    )
    return PrincipalResponse.from_record(result.principal)


@router.get(
    "/me",
    response_model=PrincipalResponse,
    responses={status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}},
)
async def get_current_user(
    request: Request,
    weora_session: str | None = Cookie(default=None),
) -> PrincipalResponse:
    """恢复当前浏览器会话。"""
    settings = _settings(request)
    session_token = request.cookies.get(settings.session_cookie_name, weora_session)
    try:
        principal = await _identity_service(request).get_current_principal(session_token)
    except AuthenticationRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTHENTICATION_REQUIRED", "message": "请先登录"},
        ) from exc
    return PrincipalResponse.from_record(principal)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
        status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
    },
)
async def logout(
    request: Request,
    response: Response,
    x_csrf_token: str | None = Header(default=None),
) -> None:
    """撤销当前会话并清理浏览器 Cookie。"""
    settings = _settings(request)
    try:
        await _identity_service(request).logout(
            session_token=request.cookies.get(settings.session_cookie_name),
            csrf_token=x_csrf_token,
            request_id=get_request_id() or "unknown",
        )
    except AuthenticationRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTHENTICATION_REQUIRED", "message": "请先登录"},
        ) from exc
    except CsrfRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "CSRF_REJECTED", "message": "安全校验失败，请刷新后重试"},
        ) from exc

    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
