"""通过公开 HTTP 接口验证注册、登录和会话行为。"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.support.database import enable_sqlite_foreign_keys
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.application import create_app
from weora.shared.infrastructure.database import Base


@pytest.fixture
async def auth_client() -> AsyncIterator[AsyncClient]:
    """验证开发环境默认校验密码，并使用隔离的真实 SQLAlchemy 数据库。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app(
        IsolatedAppSettings(environment="development", session_cookie_secure=False),
        session_factory=session_factory,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await engine.dispose()


@pytest.fixture
async def development_auth_client() -> AsyncIterator[AsyncClient]:
    """提供显式开启开发登录绕过的隔离应用。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app(
        IsolatedAppSettings(
            environment="development",
            development_auth_bypass_enabled=True,
            session_cookie_secure=False,
        ),
        session_factory=session_factory,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await engine.dispose()


@pytest.fixture
async def production_auth_client() -> AsyncIterator[AsyncClient]:
    """验证生产环境即使误配绕过开关也必须保持正式校验。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app(
        IsolatedAppSettings(
            environment="production",
            development_auth_bypass_enabled=True,
            session_cookie_secure=False,
        ),
        session_factory=session_factory,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await engine.dispose()


@pytest.mark.anyio
async def test_register_creates_authenticated_workspace_owner(auth_client: AsyncClient) -> None:
    """注册成功后应立即得到用户、工作空间和 OWNER 身份。"""
    response = await auth_client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "余则成",
            "email": "Owner@Example.com",
            "password": "correct horse battery staple",
            "workspace_name": "Weora 研发组",
        },
    )

    assert response.status_code == 201
    principal = response.json()
    assert principal["user"]["display_name"] == "余则成"
    assert principal["user"]["email"] == "owner@example.com"
    assert principal["workspace"]["name"] == "Weora 研发组"
    assert principal["workspace"]["role"] == "owner"
    assert auth_client.cookies.get("weora_session")
    assert auth_client.cookies.get("weora_csrf")

    me_response = await auth_client.get("/api/v1/auth/me")

    assert me_response.status_code == 200
    assert me_response.json() == principal


@pytest.mark.anyio
async def test_registered_user_can_log_in_again(auth_client: AsyncClient) -> None:
    """已有用户应能通过标准化邮箱重新创建会话。"""
    register_response = await auth_client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "余则成",
            "email": "owner@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "Weora 研发组",
        },
    )
    expected_principal = register_response.json()
    auth_client.cookies.clear()

    login_response = await auth_client.post(
        "/api/v1/auth/login",
        json={
            "email": "OWNER@EXAMPLE.COM",
            "password": "correct horse battery staple",
        },
    )

    assert login_response.status_code == 200
    assert login_response.json() == expected_principal
    assert auth_client.cookies.get("weora_session")
    assert (await auth_client.get("/api/v1/auth/me")).json() == expected_principal


@pytest.mark.anyio
async def test_development_login_accepts_any_non_empty_password(
    development_auth_client: AsyncClient,
) -> None:
    """开发开关启用后，已有用户输入任意非空密码都应能登录。"""
    await development_auth_client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "开发者",
            "email": "developer@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "Weora 开发空间",
        },
    )
    development_auth_client.cookies.clear()

    login_response = await development_auth_client.post(
        "/api/v1/auth/login",
        json={
            "email": "developer@example.com",
            "password": "x",
        },
    )

    assert login_response.status_code == 200
    assert login_response.json()["user"]["email"] == "developer@example.com"
    assert development_auth_client.cookies.get("weora_session")


@pytest.mark.anyio
async def test_production_never_enables_development_password_bypass(
    production_auth_client: AsyncClient,
) -> None:
    """生产环境必须忽略误开的开发绕过配置。"""
    await production_auth_client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "生产用户",
            "email": "production@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "Weora 生产空间",
        },
    )
    production_auth_client.cookies.clear()

    login_response = await production_auth_client.post(
        "/api/v1/auth/login",
        json={
            "email": "production@example.com",
            "password": "x",
        },
    )

    assert login_response.status_code == 401
    assert login_response.json()["detail"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.anyio
async def test_logout_requires_csrf_and_revokes_session(auth_client: AsyncClient) -> None:
    """退出必须验证 CSRF，成功后原会话不能继续访问控制台。"""
    await auth_client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "余则成",
            "email": "owner@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "Weora 研发组",
        },
    )
    csrf_token = auth_client.cookies["weora_csrf"]

    rejected_response = await auth_client.post("/api/v1/auth/logout")

    assert rejected_response.status_code == 403
    assert (await auth_client.get("/api/v1/auth/me")).status_code == 200

    logout_response = await auth_client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert logout_response.status_code == 204
    assert auth_client.cookies.get("weora_session") is None
    assert (await auth_client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.anyio
async def test_duplicate_email_cannot_create_second_account(auth_client: AsyncClient) -> None:
    """邮箱大小写不同也不能创建第二个账户。"""
    payload = {
        "display_name": "余则成",
        "email": "owner@example.com",
        "password": "correct horse battery staple",
        "workspace_name": "Weora 研发组",
    }
    first_response = await auth_client.post("/api/v1/auth/register", json=payload)
    auth_client.cookies.clear()

    duplicate_response = await auth_client.post(
        "/api/v1/auth/register",
        json={**payload, "email": "OWNER@EXAMPLE.COM"},
    )

    assert first_response.status_code == 201
    assert duplicate_response.status_code == 409
    assert duplicate_response.json()["detail"]["code"] == "EMAIL_UNAVAILABLE"


@pytest.mark.anyio
async def test_login_does_not_reveal_whether_email_exists(auth_client: AsyncClient) -> None:
    """未知邮箱和错误密码必须返回相同的公开错误。"""
    await auth_client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "余则成",
            "email": "owner@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "Weora 研发组",
        },
    )
    auth_client.cookies.clear()

    wrong_password_response = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "wrong password"},
    )
    unknown_email_response = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": "unknown@example.com", "password": "wrong password"},
    )

    assert wrong_password_response.status_code == 401
    assert unknown_email_response.status_code == 401
    assert wrong_password_response.json() == unknown_email_response.json()
    assert wrong_password_response.json()["detail"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.anyio
async def test_repeated_login_failures_are_rate_limited(auth_client: AsyncClient) -> None:
    """连续错误凭据必须触发跨请求限流。"""
    await auth_client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "余则成",
            "email": "owner@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "Weora 研发组",
        },
    )
    auth_client.cookies.clear()

    for _ in range(5):
        response = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": "owner@example.com", "password": "wrong password"},
        )
        assert response.status_code == 401

    blocked_response = await auth_client.post(
        "/api/v1/auth/login",
        json={
            "email": "owner@example.com",
            "password": "correct horse battery staple",
        },
    )

    assert blocked_response.status_code == 429
    assert blocked_response.json()["detail"]["code"] == "LOGIN_RATE_LIMITED"
