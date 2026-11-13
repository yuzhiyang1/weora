"""从公开入口验证浏览器确认与 CLI 凭据兑换、撤销。"""

import os
from asyncio import gather
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Cookies
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from uvicorn import Config

from tests.support.database import enable_sqlite_foreign_keys
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.application import create_app
from weora.modules.collaboration.infrastructure.agent_mail import CliLoginRequest
from weora.modules.collaboration.infrastructure.agent_sessions import _digest
from weora.shared.infrastructure.database import Base


@pytest.fixture
async def cli_server() -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    dsn = os.environ.get("WEORA_CLI_TEST_DATABASE_URL")
    if dsn:
        # 单连接池能直接暴露事务内二次取连接。
        engine = create_async_engine(dsn, pool_size=1, max_overflow=0, pool_timeout=1)
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        enable_sqlite_foreign_keys(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(
        IsolatedAppSettings(environment="test", workspace_invite_key=SecretStr("cli-test-invite-key" * 3)),
        session_factory=sessions,
    )
    transport = ASGITransport(app=app, client=(f"2001:db8::{uuid4().hex[:4]}:{uuid4().hex[:4]}", 8000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sessions
    await engine.dispose()


async def begin(client: AsyncClient) -> tuple[str, str]:
    secret = token_urlsafe(32)
    response = await client.post(
        "/api/v1/cli-auth/requests",
        json={
            "name": "开发机",
            "verifier_hash": sha256(secret.encode()).hexdigest(),
        },
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    assert secret not in response.text
    return "/api/v1/cli-auth/requests/" + response.json()["id"], secret


async def signup(client: AsyncClient) -> tuple[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "登录验收",
            "email": f"{uuid4().hex}@example.com",
            "password": "test password for CLI flow",
            "workspace_name": "CLI 测试空间",
        },
    )
    assert response.status_code == 201, response.text
    principal = response.json()
    return str(principal["user"]["id"]), str(principal["workspace"]["id"])


def csrf(client: AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["weora_csrf"]}


@pytest.mark.anyio
async def test_browser_approval_binds_account_and_logout_revokes_device(
    cli_server: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, _ = cli_server
    path, secret = await begin(client)
    payload = {"verifier": secret}
    assert (await client.get(path)).status_code == 401
    assert (await client.post(path + "/poll", json=payload)).json()["status"] == "pending"
    assert (await client.post(path + "/poll", json=payload)).json()["status"] == "slow_down"
    user_id, workspace = await signup(client)
    preview = (await client.get(path)).json()
    assert preview["device_name"] == "开发机" and preview["user_id"] == user_id
    decision = {"decision": "approve", "workspace_id": workspace, "user_id": user_id}
    assert (await client.post(path + "/decision", json=decision)).status_code == 403
    changed = await client.post(
        path + "/decision",
        headers=csrf(client),
        json=decision | {"workspace_id": str(uuid4())},
    )
    assert changed.status_code == 409
    assert (await client.post(path + "/decision", headers=csrf(client), json=decision)).json() == {"status": "approved"}
    assert (await client.post(path + "/decision", headers=csrf(client), json=decision)).status_code == 404
    assert (await client.post(path + "/poll", json={"verifier": token_urlsafe(32)})).status_code == 404
    accepted = (await client.post(path + "/poll", json=payload)).json()
    assert accepted["status"] == "authorized"
    assert accepted["workspace_id"] == workspace
    assert accepted["token"] != secret
    repeated = (await client.post(path + "/poll", json=payload)).json()
    assert repeated["id"] == accepted["id"] and repeated["token"] == accepted["token"]
    auth = {"Authorization": "Bearer " + accepted["token"]}
    me = f"/api/v1/workspaces/{workspace}/agent-collaboration/device-self"
    verified = await client.get(me, headers=auth)
    assert verified.json()["user_id"] == user_id
    assert "token" not in verified.json()
    assert (await client.delete(me, headers=auth)).status_code == 204
    assert (await client.delete(me, headers=auth)).status_code == 204
    assert (await client.get(me, headers=auth)).status_code == 401
    assert (await client.post(path + "/poll", json=payload)).json() == {"status": "denied"}


@pytest.mark.anyio
async def test_expired_request_cannot_issue_device(
    cli_server: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, sessions = cli_server
    path, secret = await begin(client)
    async with sessions.begin() as db:
        row = await db.get(CliLoginRequest, UUID(path.rsplit("/", 1)[-1]))
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await client.post(path + "/poll", json={"verifier": secret})).json() == {"status": "expired"}


@pytest.mark.anyio
async def test_switching_accounts_in_same_workspace_invalidates_displayed_decision(
    cli_server: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    owner, _ = cli_server
    owner_id, workspace = await signup(owner)
    path, secret = await begin(owner)
    preview = (await owner.get(path)).json()
    invite = await owner.post(f"/api/v1/auth/workspaces/{workspace}/invitations", headers=csrf(owner), json={})
    assert invite.status_code == 201, invite.text

    owner_cookies = Cookies(owner.cookies)
    owner.cookies.clear()
    joined = await owner.post(
        "/api/v1/auth/invitations/register",
        json={
            "token": invite.json()["token"],
            "display_name": "另一位成员",
            "email": f"{uuid4().hex}@example.com",
            "password": "test password for CLI flow",
        },
    )
    assert joined.status_code == 201, joined.text
    assert joined.json()["workspace"]["id"] == workspace
    assert joined.json()["user"]["id"] != owner_id

    # 模拟旧标签页提交 A 的确认内容，但浏览器带的是 B 的有效会话和 CSRF。
    for action in ("approve", "deny"):
        decision = {"decision": action, "user_id": preview["user_id"], "workspace_id": preview["workspace_id"]}
        stale = await owner.post(path + "/decision", headers=csrf(owner), json=decision)
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "CLI_ACCOUNT_CHANGED"

    owner.cookies.clear()
    owner.cookies.update(owner_cookies)
    assert (await owner.post(path + "/poll", json={"verifier": secret})).json()["status"] == "pending"
    decision = {"decision": "approve", "user_id": owner_id, "workspace_id": workspace}
    assert (await owner.post(path + "/decision", headers=csrf(owner), json=decision)).status_code == 200


@pytest.mark.anyio
async def test_concurrent_redemptions_reuse_one_connection(
    cli_server: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    if not os.environ.get("WEORA_CLI_TEST_DATABASE_URL"):
        pytest.skip("并发兑换需要 PostgreSQL 行锁与连接池")
    client, _ = cli_server
    user_id, workspace = await signup(client)
    decision = {"decision": "approve", "user_id": user_id, "workspace_id": workspace}
    approved = []
    for _ in range(15):
        path, secret = await begin(client)
        assert (await client.post(path + "/decision", headers=csrf(client), json=decision)).status_code == 200
        approved.append((path, secret))
    results = await gather(*(client.post(path + "/poll", json={"verifier": secret}) for path, secret in approved))
    assert {result.json()["status"] for result in results} == {"authorized"}
    assert len({result.json()["id"] for result in results}) == len(approved)


@pytest.mark.anyio
async def test_login_limits_by_forwarded_client_only_from_trusted_proxy(
    cli_server: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = cli_server
    proxy = "172.31.247.2"
    config = Config(
        create_app(IsolatedAppSettings(environment="test"), session_factory=sessions),
        forwarded_allow_ips=f"127.0.0.1,{proxy}",
    )
    config.load()
    app = config.loaded_app
    prefix = f"2001:db8:{uuid4().hex[:4]}:{uuid4().hex[:4]}"
    async with AsyncClient(transport=ASGITransport(app=app, client=(proxy, 8000)), base_url="http://test") as proxied:
        for index in range(1, 32):
            proxied.headers["X-Forwarded-For"] = f"{prefix}::{index}"
            await begin(proxied)
        proxied.headers["X-Forwarded-For"] = f"{prefix}::1"
        for _ in range(29):
            await begin(proxied)
        limited = await proxied.post(
            "/api/v1/cli-auth/requests", json={"name": "开发机", "verifier_hash": _digest(token_urlsafe(32))}
        )
        assert limited.status_code == 429

    direct_ip = f"{prefix}::ff"
    direct = ASGITransport(app=app, client=(direct_ip, 8000))
    async with AsyncClient(transport=direct, base_url="http://test") as client:
        client.headers["X-Forwarded-For"] = f"{prefix}::fe"
        path, _ = await begin(client)
    async with sessions() as db:
        row = await db.get(CliLoginRequest, UUID(path.rsplit("/", 1)[-1]))
        assert row is not None and row.source_key == _digest(direct_ip)
