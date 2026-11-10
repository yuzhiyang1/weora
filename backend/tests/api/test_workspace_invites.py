"""通过独立会话验证邀请链接及成员关系的权威状态。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import anyio
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.support.database import enable_sqlite_foreign_keys
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.application import create_app
from weora.modules.identity.infrastructure.models import WorkspaceInviteModel
from weora.shared.infrastructure.database import Base

AUTH = "/api/v1/auth"
PASSWORD = "isolated fixture long password"


@pytest.fixture
async def invite_app(tmp_path: Path) -> AsyncIterator[tuple[ASGITransport, async_sessionmaker[AsyncSession]]]:
    # 独立连接才能复现两个浏览器并发兑换；共享内存连接会串用事务。
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'invites.sqlite3'}")
    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(
        IsolatedAppSettings(environment="test", workspace_invite_key=SecretStr("i" * 32)),
        session_factory=sessions,
    )
    try:
        yield ASGITransport(app=app), sessions
    finally:
        await engine.dispose()


def browser(transport: ASGITransport) -> AsyncClient:
    return AsyncClient(transport=transport, base_url="http://test")


def csrf(client: AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["weora_csrf"]}


async def signup(client: AsyncClient, name: str) -> str:
    result = await client.post(
        f"{AUTH}/register",
        json={
            "display_name": name,
            "email": f"{name}@example.com",
            "password": PASSWORD,
            "workspace_name": f"{name} 空间",
        },
    )
    assert result.status_code == 201, result.text
    return str(result.json()["workspace"]["id"])


async def issue(client: AsyncClient, path: str) -> tuple[str, str]:
    result = await client.post(path, headers=csrf(client), json={})
    assert result.status_code == 201, result.text
    assert result.headers["Cache-Control"] == "no-store"
    return str(result.json()["invitation"]["id"]), str(result.json()["token"])


@pytest.mark.anyio
async def test_login_accept_switch_and_member_permissions(
    invite_app: tuple[ASGITransport, async_sessionmaker[AsyncSession]],
) -> None:
    transport, _ = invite_app
    async with browser(transport) as admin, browser(transport) as member:
        workspace = await signup(admin, "admin")
        original = await signup(member, "existing")
        invites = f"{AUTH}/workspaces/{workspace}/invitations"
        invite_id, token = await issue(admin, invites)
        copied = await admin.post(f"{invites}/{invite_id}/copy", headers=csrf(admin))
        assert copied.json() == {"token": token}
        assert token not in (await admin.get(invites)).text
        assert (await member.post(f"{AUTH}/logout", headers=csrf(member))).status_code == 204
        assert (await member.post(f"{AUTH}/invitations/preview", json={"token": token})).json() == {
            "workspace_name": "admin 空间",
            "inviter_name": "admin",
        }
        signed_in = await member.post(f"{AUTH}/login", json={"email": "existing@example.com", "password": PASSWORD})
        assert signed_in.json()["workspace"]["id"] == original
        accept = f"{AUTH}/invitations/accept"
        assert (await member.post(accept, json={"token": token})).status_code == 403
        assert (await member.post(accept, headers=csrf(member), json={"token": token})).json()["workspace"] == {
            "id": workspace,
            "name": "admin 空间",
            "role": "member",
        }
        assert (await member.get(f"{AUTH}/me")).json()["workspace"]["id"] == workspace
        assert (await member.post(accept, headers=csrf(member), json={"token": token})).status_code == 200
        assert (await member.get(invites)).status_code == 403
        assert (await member.post(invites, headers=csrf(member), json={})).status_code == 403
        assert (await admin.post(f"{AUTH}/workspaces/{original}/activate", headers=csrf(admin))).status_code == 403
        assert (await member.post(f"{AUTH}/workspaces/{original}/activate", headers=csrf(member))).status_code == 200
        assert (await member.get(f"{AUTH}/me")).json()["workspace"]["id"] == original
        assert (await admin.get(invites)).json()[0]["status"] == "used"


@pytest.mark.anyio
async def test_invited_registration_revocation_expiry_and_rejoin(
    invite_app: tuple[ASGITransport, async_sessionmaker[AsyncSession]],
) -> None:
    transport, sessions = invite_app
    closed_registration = create_app(
        IsolatedAppSettings(environment="test", registration_enabled=False, workspace_invite_key=SecretStr("i" * 32)),
        session_factory=sessions,
    )
    async with browser(transport) as admin, browser(ASGITransport(app=closed_registration)) as guest:
        workspace = await signup(admin, "owner")
        invites = f"{AUTH}/workspaces/{workspace}/invitations"
        revoked_id, revoked = await issue(admin, invites)
        for _ in range(2):
            assert (await admin.delete(f"{invites}/{revoked_id}", headers=csrf(admin))).status_code == 204
        expired_id, expired = await issue(admin, invites)
        async with sessions.begin() as session:
            await session.execute(
                update(WorkspaceInviteModel)
                .where(
                    WorkspaceInviteModel.id == UUID(expired_id),
                )
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        register = f"{AUTH}/invitations/register"
        account = {"display_name": "新成员", "email": "new@example.com", "password": PASSWORD}
        public_signup = await guest.post(f"{AUTH}/register", json={**account, "workspace_name": "新空间"})
        assert public_signup.status_code == 403
        for token in (revoked, expired, expired[:-1] + ("0" if expired[-1] != "0" else "1")):
            assert (await guest.post(f"{AUTH}/invitations/preview", json={"token": token})).status_code == 410
            assert (await guest.post(register, json={**account, "token": token})).status_code == 410
        _, valid = await issue(admin, invites)
        joined = await guest.post(register, json={**account, "token": valid})
        assert joined.status_code == 201, joined.text
        user = joined.json()["user"]["id"]
        assert joined.json()["workspace"]["role"] == "member"
        assert len((await guest.get(f"{AUTH}/workspaces")).json()) == 1
        assert {record["status"] for record in (await admin.get(invites)).json()} == {"revoked", "expired", "used"}
        old_cookie = guest.cookies["weora_session"]
        removal = f"{AUTH}/workspaces/{workspace}/members/{user}"
        assert (await admin.delete(removal, headers=csrf(admin))).status_code == 204
        assert (await guest.get(f"{AUTH}/me")).status_code == 401
        login = {"email": account["email"], "password": PASSWORD}
        assert (await guest.post(f"{AUTH}/login", json=login)).json()["detail"]["code"] == "NO_ACTIVE_WORKSPACE"
        _, fresh = await issue(admin, invites)
        assert (await guest.post(f"{AUTH}/login", json={**login, "invite_token": fresh})).status_code == 200
        assert (await guest.get(f"{AUTH}/me")).json()["workspace"]["id"] == workspace
        async with browser(transport) as former_session:
            former_session.cookies.set("weora_session", old_cookie)
            assert (await former_session.get(f"{AUTH}/me")).status_code == 401


@pytest.mark.anyio
async def test_only_one_concurrent_registration_consumes_invite(
    invite_app: tuple[ASGITransport, async_sessionmaker[AsyncSession]],
) -> None:
    transport, _ = invite_app
    async with browser(transport) as admin:
        workspace = await signup(admin, "team")
        _, token = await issue(admin, f"{AUTH}/workspaces/{workspace}/invitations")
        statuses: list[int] = []

        async def accept(name: str) -> None:
            async with browser(transport) as client:
                result = await client.post(
                    f"{AUTH}/invitations/register",
                    json={
                        "token": token,
                        "display_name": name,
                        "email": f"{name}@example.com",
                        "password": PASSWORD,
                    },
                )
                statuses.append(result.status_code)

        async with anyio.create_task_group() as group:
            group.start_soon(accept, "first")
            group.start_soon(accept, "second")
        assert sorted(statuses) == [201, 410]
        assert len((await admin.get(f"{AUTH}/workspaces/{workspace}/members")).json()) == 2
