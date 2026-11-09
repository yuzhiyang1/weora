"""供业务模块复用的认证与 CSRF HTTP 依赖。"""

from typing import cast

from fastapi import Header, HTTPException, Request, status

from weora.bootstrap.settings import AppSettings
from weora.modules.identity.application.errors import AuthenticationRequiredError, CsrfRejectedError
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.identity.application.service import IdentityService


def _identity_service(request: Request) -> IdentityService:
    return cast(IdentityService, request.app.state.identity_service)


def _settings(request: Request) -> AppSettings:
    return cast(AppSettings, request.app.state.settings)


async def current_principal(request: Request) -> PrincipalRecord:
    """恢复当前会话；业务模块不需要感知 Cookie 名称。"""
    settings = _settings(request)
    try:
        return await _identity_service(request).get_current_principal(request.cookies.get(settings.session_cookie_name))
    except AuthenticationRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTHENTICATION_REQUIRED", "message": "请先登录"},
        ) from exc


async def write_principal(
    request: Request,
    x_csrf_token: str | None = Header(default=None),
) -> PrincipalRecord:
    """恢复当前会话并校验与会话绑定的 CSRF Header。"""
    settings = _settings(request)
    try:
        return await _identity_service(request).authorize_write(
            session_token=request.cookies.get(settings.session_cookie_name),
            csrf_token=x_csrf_token,
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
