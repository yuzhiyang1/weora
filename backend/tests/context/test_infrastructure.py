"""真实 SQLite 持久化与本地 HTTP 适配验证，不调用飞书生产网络。"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import aiohttp
import pytest
from aiohttp import web
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from weora.modules.context.domain import ContextError, Json
from weora.modules.context.feishu import FeishuHttpReader, FernetSecrets
from weora.modules.context.store import SqlContextStore
from weora.shared.infrastructure.database import Base


def test_sql_store_survives_recreation_and_isolates_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = f"sqlite+aiosqlite:///{tmp_path / 'context.db'}"
        workspace, other_workspace = uuid4(), uuid4()
        engine = create_async_engine(database)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        store = SqlContextStore(async_sessionmaker(engine, expire_on_commit=False))
        await store.change(workspace, "budget", "run1", lambda _: {"calls": 1})
        await store.change(workspace, "budget", "run1", lambda old: {"calls": int(str((old or {})["calls"])) + 1})
        assert await store.get(other_workspace, "budget", "run1") is None
        assert await store.list(other_workspace, "budget") == []
        await engine.dispose()
        engine = create_async_engine(database)
        recreated = SqlContextStore(async_sessionmaker(engine, expire_on_commit=False))
        assert await recreated.get(workspace, "budget", "run1") == {"calls": 2}
        assert await recreated.list(workspace, "snapshot") == []
        await engine.dispose()
    asyncio.run(scenario())


def test_sql_store_rolls_back_failed_business_update(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rollback.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        workspace = uuid4()
        store = SqlContextStore(async_sessionmaker(engine, expire_on_commit=False))
        await store.change(workspace, "budget", "run1", lambda _: {"calls": 10})

        def reject(old: Json | None) -> Json:
            raise ContextError("BUDGET_EXCEEDED")

        with pytest.raises(ContextError):
            await store.change(workspace, "budget", "run1", reject)
        assert await store.get(workspace, "budget", "run1") == {"calls": 10}
        await engine.dispose()
    asyncio.run(scenario())


def test_credential_cipher_detects_tampering_and_wrong_key() -> None:
    first, second = FernetSecrets(Fernet.generate_key().decode()), FernetSecrets(Fernet.generate_key().decode())
    ciphertext = first.encrypt("private-app-secret")
    assert first.decrypt(ciphertext) == "private-app-secret"
    tampered = ciphertext[:25] + ("A" if ciphertext[25] != "A" else "B") + ciphertext[26:]
    for value, cipher in ((tampered, first), (ciphertext, second)):
        with pytest.raises(ContextError) as error:
            cipher.decrypt(value)
        assert error.value.code == "CREDENTIALS_UNAVAILABLE"


class TokenReader(FeishuHttpReader):
    def __init__(self, session: aiohttp.ClientSession, cipher: FernetSecrets) -> None:
        super().__init__(session, cipher)
        self.auth_calls = 0
        self.requests: list[Json] = []
        self.expire_first = False
        self.response: Json | None = None

    async def _json(
        self, method: str, path: str, *, token: str | None = None,
        body: Json | None = None, params: dict[str, str] | None = None,
        data: aiohttp.FormData | None = None, timeout_seconds: float = 2.5,
    ) -> Json:
        self.requests.append({"path": path, "method": method, "params": params})
        if path == "/auth/v3/tenant_access_token/internal":
            self.auth_calls += 1
            await asyncio.sleep(0)
            return {"tenant_access_token": f"t-{self.auth_calls}", "expire": 7200}
        if self.expire_first and token == "t-1":
            raise ContextError("TOKEN_EXPIRED")
        return self.response or {"code": 0, "data": {"items": [], "has_more": False}}


def test_token_refresh_uses_singleflight_and_credential_version() -> None:
    async def scenario() -> None:
        cipher = FernetSecrets(Fernet.generate_key().decode())
        connection: Json = {"id": str(uuid4()), "app_id": "cli_test", "version": 1,
                            "secret": cipher.encrypt("secret")}
        async with aiohttp.ClientSession() as session:
            reader = TokenReader(session, cipher)
            reader.expire_first = True
            await asyncio.gather(*(reader._get(connection, "/im/v1/messages") for _ in range(20)))
            assert reader.auth_calls == 2
            await reader._get(dict(connection, version=2), "/im/v1/messages")
            assert reader.auth_calls == 3
    asyncio.run(scenario())


def test_thread_adapter_omits_unsupported_upstream_time_filters() -> None:
    async def scenario() -> None:
        cipher = FernetSecrets(Fernet.generate_key().decode())
        connection: Json = {"id": str(uuid4()), "app_id": "cli_test", "version": 1,
                            "secret": cipher.encrypt("secret")}
        async with aiohttp.ClientSession() as session:
            reader = TokenReader(session, cipher)
            now = datetime.now(UTC)
            await reader.messages(connection, chat_id="oc_project", start=now, end=now,
                                  thread_id="omt_test", page_token="next-token")
            params = cast(dict[str, str], reader.requests[-1]["params"])
            assert params["container_id_type"] == "thread" and params["page_token"] == "next-token"
            assert "start_time" not in params and "end_time" not in params
            assert params["sort_type"] == "ByCreateTimeAsc"
    asyncio.run(scenario())


def test_normalization_keeps_external_authors_and_placeholders() -> None:
    raw: Json = {"message_id": "om_one", "chat_id": "oc_project", "create_time": "1609296809000",
                 "sender": {"id": "ou_not_a_weora_member"}, "msg_type": "text",
                 "body": {"content": json.dumps({"text": "登录设计"})}}
    message = FeishuHttpReader._message(raw)
    assert message.author_id == "ou_not_a_weora_member" and message.text == "登录设计"
    attachment = FeishuHttpReader._message(dict(raw, msg_type="file", body={
        "content": json.dumps({"file_key": "file_test", "file_name": "方案.txt"}),
    }))
    assert "未下载" in attachment.text and attachment.parse_status == "partial"
    assert attachment.resources[0]["file_name"] == "方案.txt"
    deleted = FeishuHttpReader._message(dict(raw, deleted=True))
    assert deleted.content_type == "deleted" and "撤回" in deleted.text


@pytest.mark.parametrize("data", [
    "invalid", {"items": "invalid"}, {"items": [], "has_more": True},
    {"items": [], "has_more": "true"}, {"items": [], "has_more": 0}, {"items": []},
])
def test_malformed_page_never_becomes_a_false_complete_window(data: object) -> None:
    async def scenario() -> None:
        cipher = FernetSecrets(Fernet.generate_key().decode())
        connection: Json = {"id": str(uuid4()), "app_id": "cli_test", "version": 1,
                            "secret": cipher.encrypt("secret")}
        async with aiohttp.ClientSession() as session:
            reader = TokenReader(session, cipher)
            reader.response = {"code": 0, "data": data}
            with pytest.raises(ContextError) as error:
                await reader.messages(connection, chat_id="oc_project", start=datetime.now(UTC),
                                      end=datetime.now(UTC), page_token=None)
            assert error.value.code == "PROVIDER_UNAVAILABLE"
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["redirect", "oversized", "malformed", "permission", "rate"])
def test_http_adapter_rejects_unsafe_responses_without_following_redirects(mode: str) -> None:
    async def scenario() -> None:
        target_called = False

        async def handler(request: web.Request) -> web.Response:
            nonlocal target_called
            if request.path == "/redirected":
                target_called = True
                return web.json_response({"code": 0})
            if mode == "redirect":
                return web.Response(status=302, headers={"Location": "/redirected"})
            if mode == "oversized":
                return web.Response(body=b"x" * 2000001)
            if mode == "malformed":
                return web.Response(text="not-json")
            if mode == "permission":
                return web.json_response({"code": 230002, "msg": "sensitive app details"}, status=400)
            return web.Response(status=429)

        application = web.Application()
        application.router.add_get("/{tail:.*}", handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                reader = FeishuHttpReader(session, FernetSecrets(Fernet.generate_key().decode()))
                # 只有本地测试替换固定地址；业务 API 从不接受 URL 参数。
                reader.BASE = f"http://127.0.0.1:{port}"
                with pytest.raises(ContextError) as error:
                    await reader._json("GET", "/test", token="short-lived-token")
                assert "sensitive" not in str(error.value) and "short-lived" not in str(error.value)
                assert error.value.code == {"permission": "PERMISSION_DENIED", "rate": "RATE_LIMITED"}.get(
                    mode, "PROVIDER_UNAVAILABLE",
                )
                assert target_called is False
        finally:
            await runner.cleanup()
    asyncio.run(scenario())
