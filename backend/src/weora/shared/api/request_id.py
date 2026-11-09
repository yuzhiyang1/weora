"""请求追踪标识中间件。"""

import re
from contextvars import ContextVar, Token
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """返回当前请求的追踪标识，供日志和外部调用透传。"""
    return _request_id.get()


class RequestIdMiddleware:
    """为每个 HTTP 请求建立并返回稳定的追踪标识。

    使用纯 ASGI 中间件，避免 BaseHTTPMiddleware 对流式响应和上下文变量传播的限制。
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = self._resolve_request_id(scope)
        token = _request_id.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        finally:
            self._reset_context(token)

    @staticmethod
    def _resolve_request_id(scope: Scope) -> str:
        candidate = Headers(scope=scope).get(REQUEST_ID_HEADER)
        if candidate and _VALID_REQUEST_ID.fullmatch(candidate):
            return candidate
        return str(uuid4())

    @staticmethod
    def _reset_context(token: Token[str | None]) -> None:
        _request_id.reset(token)
