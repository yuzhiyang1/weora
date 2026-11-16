"""隔离会话的模型流代理。只允许访问本轮冻结的供应商和模型。"""

import json
from base64 import b64encode
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import aiohttp

from weora.modules.runs.application.execution import RuntimeFailure
from weora.modules.runs.application.ports import ExecutionProfile

Json = dict[str, object]
WriteFrame = Callable[[Json], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ModelRoute:
    origin: str
    base: str
    api: str
    paths: tuple[str, ...]


MODEL_ROUTES = {
    "deepseek": ModelRoute("https://api.deepseek.com", "/v1", "openai-completions", ("/v1/chat/completions",)),
    "moonshotai-cn": ModelRoute("https://api.moonshot.cn", "/v1", "openai-completions", ("/v1/chat/completions",)),
    "openai": ModelRoute(
        "https://api.openai.com", "/v1", "openai-responses", ("/v1/responses", "/v1/chat/completions")
    ),
    "anthropic": ModelRoute("https://api.anthropic.com", "", "anthropic-messages", ("/v1/messages",)),
    "google": ModelRoute("https://generativelanguage.googleapis.com", "", "google-generative-ai", ()),
}


class NativeModelProxy:
    def __init__(self, profile: ExecutionProfile, key: str, *, test_origin: str | None = None) -> None:
        route = MODEL_ROUTES.get(profile.provider)
        if route is None:
            raise RuntimeFailure("MODEL_NOT_SUPPORTED")
        self.route = route
        self._profile, self._key = profile, key
        self._origin = test_origin or route.origin
        # 只保留脱敏状态码；原生 SDK 处理重试，最终失败时供 Run 区分付费、鉴权与限流。
        self.failure_code: str | None = None

    def _endpoint(self, frame: Json) -> tuple[str, Json, dict[str, str]] | None:
        request_target, body = frame.get("path"), frame.get("body")
        if not isinstance(request_target, str) or not isinstance(body, dict) or len(request_target) > 2048:
            return None
        url = urlsplit(request_target)
        if url.netloc or url.scheme or url.fragment:
            return None
        provider, model = self._profile.provider, self._profile.model_id
        headers = {"Authorization": f"Bearer {self._key}"}
        query = ""
        if provider == "google":
            paths = {f"/v1beta/models/{model}:streamGenerateContent", f"/v1beta/models/{model}:generateContent"}
            if url.path not in paths:
                return None
            if parse_qs(url.query).get("alt") == ["sse"]:
                query = "?alt=sse"
            headers = {"x-goog-api-key": self._key}
        elif url.path not in self.route.paths or body.get("model") != model:
            return None
        if provider == "openai":
            # 复用团队密钥的沙箱只能发起当前模型推理，不能借 Responses 调用远端存储或内置服务。
            tools = body.get("tools", [])
            if not isinstance(tools, list) or any(
                not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools
            ) or any(field in body for field in ("previous_response_id", "conversation", "background")):
                return None
            body = body | {"store": False}
        if provider == "anthropic":
            headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01"}
            beta = frame.get("anthropicBeta")
            if isinstance(beta, str) and len(beta) < 512 and all(32 <= ord(char) < 127 for char in beta):
                headers["anthropic-beta"] = beta
        if self._profile.max_tokens:
            field = "max_output_tokens" if url.path == "/v1/responses" else "max_tokens"
            if provider != "google":
                requested = body.get(field)
                body = body | {
                    field: min(requested, self._profile.max_tokens)
                    if isinstance(requested, int) and requested > 0
                    else self._profile.max_tokens
                }
        return self._origin + url.path + query, body, headers

    async def stream(self, client: aiohttp.ClientSession, frame: Json, send: WriteFrame) -> None:
        request_id = frame["id"]
        endpoint = self._endpoint(frame)
        if endpoint is None:
            self.failure_code = None
            await send({"channel": "http_start", "id": request_id, "status": 403, "content_type": "text/plain"})
            await send({"channel": "http_end", "id": request_id})
            return
        # 原生循环不受隐藏请求次数裁剪；总执行时限及本次模型/输出预算由冻结配置约束。
        url, body, headers = endpoint
        started = False
        try:
            timeout = aiohttp.ClientTimeout(total=self._profile.timeout_seconds)
            async with client.post(url, json=body, headers=headers, allow_redirects=False, timeout=timeout) as response:
                # 供应商正文可能带账号或请求内容；错误只转发本地生成的代码，保留 HTTP 重试语义。
                status = response.status if not 300 <= response.status < 400 else 502
                successful = 200 <= status < 300
                self.failure_code = None if successful else {
                    401: "MODEL_AUTHENTICATION_FAILED", 402: "MODEL_PAYMENT_REQUIRED",
                    403: "MODEL_ACCESS_DENIED", 429: "MODEL_RATE_LIMITED",
                }.get(status, "MODEL_PROVIDER_UNAVAILABLE" if status >= 500 else "MODEL_FAILED")
                await send({
                    "channel": "http_start", "id": request_id, "status": status,
                    "content_type": response.content_type if successful else "application/json",
                })
                started = True
                total = 0
                if successful:
                    async for chunk in response.content.iter_chunked(32768):
                        total += len(chunk)
                        if total > 32 * 1024 * 1024:
                            raise RuntimeFailure("MODEL_OUTPUT_LIMIT")
                        await send({"channel": "http_chunk", "id": request_id, "data": b64encode(chunk).decode()})
                else:
                    # 不解析、拼接或记录上游错误正文，也不猜测具体余额；402 只表示付费/额度拒绝。
                    safe = json.dumps({"error": {"code": self.failure_code, "message": self.failure_code}}).encode()
                    await send({"channel": "http_chunk", "id": request_id, "data": b64encode(safe).decode()})
        except (TimeoutError, aiohttp.ClientError) as error:
            self.failure_code = "MODEL_TIMEOUT" if isinstance(error, TimeoutError) else "MODEL_FAILED"
            if started:
                await send({"channel": "http_error", "id": request_id})
                return
            await send({"channel": "http_start", "id": request_id, "status": 502, "content_type": "text/plain"})
        await send({"channel": "http_end", "id": request_id})
