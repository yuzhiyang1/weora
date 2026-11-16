"""实际 HTTP 错误穿过模型代理与原生终态，原因明确且不泄漏供应商正文。"""

import asyncio
import json
from base64 import b64decode
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from weora.modules.runs.application.execution import RuntimeFailure
from weora.modules.runs.application.harness import PI_RPC_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.infrastructure.native_model import NativeModelProxy
from weora.modules.runs.infrastructure.native_rpc import NativeCodingRuntime, NativeRpcConnection


@pytest.mark.anyio
@pytest.mark.parametrize("status,code", [
    (401, "MODEL_AUTHENTICATION_FAILED"), (402, "MODEL_PAYMENT_REQUIRED"),
    (403, "MODEL_ACCESS_DENIED"), (429, "MODEL_RATE_LIMITED"), (503, "MODEL_PROVIDER_UNAVAILABLE"),
])
async def test_provider_rejection_reaches_native_failure_without_sensitive_body(status: int, code: str) -> None:
    """同一次实际 HTTP 拒绝既给原生 SDK 保留状态，又给 Run 保留脱敏原因。"""
    responses = [status, 200]

    async def upstream(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer test-provider-secret"
        current = responses.pop(0)
        return web.json_response(
            {"error": {"message": "sensitive-account-and-request", "account": "test-account"}}
            if current != 200 else {"ok": True}, status=current,
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    profile = ExecutionProfile(PI_RPC_RUNTIME, "deepseek", "deepseek-flash", None, 5)
    proxy = NativeModelProxy(profile, "test-provider-secret", test_origin=f"http://127.0.0.1:{runner.addresses[0][1]}")
    frames: list[dict[str, object]] = []

    async def send(frame: dict[str, object]) -> None:
        frames.append(frame)

    frame: dict[str, object] = {
        "id": "request", "path": "/v1/chat/completions", "body": {"model": "deepseek-flash", "messages": []},
    }
    try:
        async with aiohttp.ClientSession() as client:
            await proxy.stream(client, frame, send)
            assert frames[0]["status"] == status
            wire_body = b"".join(
                b64decode(str(item["data"])) for item in frames if item["channel"] == "http_chunk"
            )
            assert json.loads(wire_body)["error"]["code"] == code
            assert b"sensitive-account" not in wire_body and b"test-provider-secret" not in wire_body
            conn = NativeRpcConnection(
                cast(asyncio.subprocess.Process, SimpleNamespace()), omp=False, model=proxy, http=client,
                call=AsyncMock(), progress=AsyncMock(), diff=AsyncMock(), tools={}, repositories={},
            )
            conn._message_end({"role": "assistant", "stopReason": "error", "content": []})
            with pytest.raises(RuntimeFailure, match=code):
                NativeCodingRuntime._output(conn)
            # SDK 自己决定是否重试；后续真实成功不能被旧失败码污染。
            frames.clear()
            await proxy.stream(client, frame, send)
            conn._message_end({"role": "assistant", "stopReason": "stop", "content": "已恢复"})
            assert NativeCodingRuntime._output(conn) == "已恢复"
            conn._message_end({"role": "assistant", "stopReason": "error", "content": []})
            with pytest.raises(RuntimeFailure, match="^MODEL_FAILED$"):
                NativeCodingRuntime._output(conn)
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_long_native_session_has_no_hidden_model_request_count_limit() -> None:
    """超过旧 160 请求阈值仍转发冻结模型；模型白名单和显式输出预算继续生效。"""
    calls: list[dict[str, object]] = []

    async def upstream(request: web.Request) -> web.Response:
        payload = await request.json()
        assert request.headers["Authorization"] == "Bearer test-long-session-key"
        assert payload["model"] == "deepseek-flash" and payload["max_tokens"] == 64
        calls.append(payload)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    profile = ExecutionProfile(PI_RPC_RUNTIME, "deepseek", "deepseek-flash", 64, 5)
    proxy = NativeModelProxy(profile, "test-long-session-key", test_origin=f"http://127.0.0.1:{runner.addresses[0][1]}")
    frames: list[dict[str, object]] = []

    async def send(frame: dict[str, object]) -> None:
        frames.append(frame)

    try:
        async with aiohttp.ClientSession() as client:
            for index in range(161):
                frames.clear()
                await proxy.stream(client, {
                    "id": str(index), "path": "/v1/chat/completions",
                    "body": {"model": "deepseek-flash", "messages": [], "max_tokens": 1024},
                }, send)
                assert frames[0]["status"] == 200, f"第 {index + 1} 次合法请求被隐式裁断"
                assert frames[-1]["channel"] == "http_end"
            assert len(calls) == 161 and proxy.failure_code is None
            for path, model in (("/v1/chat/completions", "unselected-model"), ("/v1/files", "deepseek-flash")):
                frames.clear()
                await proxy.stream(client, {"id": "denied", "path": path, "body": {"model": model}}, send)
                assert frames[0]["status"] == 403
                assert len(calls) == 161
    finally:
        await runner.cleanup()
