import hashlib
import hmac
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from weora.modules.context.agent_cli import _BotProxy
from weora.modules.context.agent_cli_skills import read_feishu_cli_skill
from weora.modules.runs.application.ports import RunActor
from weora.modules.tools.application.gateway import ToolScope


@pytest.mark.anyio
async def test_agent_can_discover_and_read_versioned_cli_skills_without_host_file_access() -> None:
    scope = ToolScope(uuid4(), RunActor(uuid4(), uuid4()), "worker", 1)
    catalog = await read_feishu_cli_skill(scope, {})
    assert catalog["version"] == "1.0.80"
    assert isinstance(catalog["skills"], list) and len(catalog["skills"]) == 27

    path = "lark-sheets/SKILL.md"
    first = await read_feishu_cli_skill(scope, {"path": path})
    assert first["ok"] and "lark-sheets" in str(first["content"])
    assert isinstance(first["next_offset"], int)
    second = await read_feishu_cli_skill(scope, {"path": path, "offset": first["next_offset"]})
    assert second["ok"] and second["content"] != first["content"]
    ref = await read_feishu_cli_skill(scope, {"path": "lark-sheets/references/lark-sheets-read-data.md"})
    assert ref["ok"] and "+cells-get" in str(ref["content"])

    for invalid in ("lark-sheets/../../README.md", "/etc/passwd", "lark-sheets\\SKILL.md"):
        result = await read_feishu_cli_skill(scope, {"path": invalid})
        assert result == {"ok": False, "error": {"code": "FEISHU_SKILL_PATH_INVALID"}}


@pytest.mark.anyio
async def test_agent_cli_proxy_only_injects_bound_bot_token_for_signed_feishu_request() -> None:
    token = AsyncMock(return_value="bound-bot-token")
    received = []

    async def fake_feishu(request: web.Request) -> web.Response:
        received.append((request.raw_path, request.headers["Authorization"]))
        return web.json_response({"code": 0, "data": {"values": [["marker"]]}}, headers={"X-Tt-Logid": "provider-id"})

    upstream_app = web.Application()
    upstream_app.router.add_get("/{path:.*}", fake_feishu)
    proxy = _BotProxy("cli-call-key", token)
    path = "/open-apis/sheets/v2/spreadsheets/test/values/sheet%21A1%3AA1"
    timestamp = str(int(time.time()))

    def signed(method: str, body: bytes = b"") -> dict[str, str]:
        canonical = "\n".join(
            ("v1", method, "open.feishu.cn", path, hashlib.sha256(body).hexdigest(), timestamp, "bot", "Authorization")
        )
        return {
            "X-Lark-Proxy-Target": "https://open.feishu.cn",
            "X-Lark-Proxy-Identity": "bot",
            "X-Lark-Proxy-Version": "v1",
            "X-Lark-Proxy-Auth-Header": "Authorization",
            "X-Lark-Proxy-Timestamp": timestamp,
            "X-Lark-Proxy-Signature": hmac.new(b"cli-call-key", canonical.encode(), "sha256").hexdigest(),
        }

    headers = signed("GET")
    async with TestServer(upstream_app) as upstream, aiohttp.ClientSession() as session:

        class LocalTransport:
            def request(self, method, url, **kwargs):
                # 保留实际请求路径，只把测试流量转向本机假的飞书 API。
                from yarl import URL

                return session.request(
                    method, URL(str(upstream.make_url("/")).rstrip("/") + url.raw_path_qs, encoded=True), **kwargs
                )

        proxy_app = web.Application()

        async def forward(request: web.Request) -> web.StreamResponse:
            return await proxy.forward(request, LocalTransport())

        proxy_app.router.add_route("*", "/{path:.*}", forward)
        async with TestServer(proxy_app) as server:
            from yarl import URL

            url = URL(str(server.make_url("/")).rstrip("/") + path, encoded=True)
            async with session.get(url, headers=headers) as response:
                assert response.status == 200
                assert (await response.json())["data"]["values"] == [["marker"]]
            for override in (
                {"X-Lark-Proxy-Target": "https://elsewhere.example"},
                {"X-Lark-Proxy-Identity": "user"},
                {"X-Lark-Proxy-Signature": "bad-signature"},
            ):
                async with session.get(url, headers=headers | override) as response:
                    assert response.status == 403
            for method in ("POST", "PATCH", "PUT", "DELETE"):
                body = b'{"text":"write"}'
                async with session.request(method, url, headers=signed(method, body), data=body) as response:
                    assert response.status == 403
                    assert await response.text() == "FEISHU_CLI_WRITE_REQUIRES_APPROVAL"
    assert received == [(path, "Bearer bound-bot-token")]
    assert proxy.log_id == "provider-id"
    token.assert_awaited_once()
