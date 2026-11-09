"""通过公开 HTTP 接口验证 Weora API 的存活状态。"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.application import create_app
from weora.shared.object_storage.testing import InMemoryObjectStorage


@pytest.mark.anyio
async def test_live_health_reports_api_identity() -> None:
    """存活接口应提供稳定、可供部署探针识别的响应。"""
    transport = ASGITransport(app=create_app(IsolatedAppSettings()))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "weora-api",
        "version": "0.1.0",
    }


@pytest.mark.anyio
async def test_request_id_is_returned_to_the_caller() -> None:
    """调用方提供的追踪标识应贯穿当前 HTTP 请求。"""
    transport = ASGITransport(app=create_app(IsolatedAppSettings()))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/health/live",
            headers={"X-Request-ID": "req-skeleton-001"},
        )

    assert response.headers["X-Request-ID"] == "req-skeleton-001"


@pytest.mark.anyio
async def test_application_factory_uses_explicit_api_settings() -> None:
    """部署环境应能调整 API 元数据和统一前缀，而不修改业务路由。"""
    settings = IsolatedAppSettings(
        app_name="Weora Test API",
        api_prefix="/internal/v1",
    )
    transport = ASGITransport(app=create_app(settings))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health_response = await client.get("/internal/v1/health/live")
        openapi_response = await client.get("/openapi.json")

    assert health_response.status_code == 200
    assert openapi_response.json()["info"]["title"] == "Weora Test API"


@pytest.mark.anyio
async def test_ready_health_includes_enabled_object_storage() -> None:
    """实例只有在数据库和已启用对象存储都可用时才应进入就绪状态。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    storage = InMemoryObjectStorage()
    settings = IsolatedAppSettings(
        environment="test",
        object_storage_provider="aliyun_oss",
        object_storage_bucket="weora-test",
        object_storage_region="cn-hangzhou",
        object_storage_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
    )
    app = create_app(
        settings,
        session_factory=session_factory,
        object_storage_factory=lambda _: storage,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    await engine.dispose()
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "ok",
        "object_storage": "ok",
    }
