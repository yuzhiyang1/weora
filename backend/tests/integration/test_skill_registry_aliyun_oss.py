"""使用真实阿里云 OSS 验证 Skill Registry 端到端对象链路。"""

import hashlib
import os
from contextlib import suppress

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.api.test_skills import build_skill_package
from weora.bootstrap.application import create_app
from weora.bootstrap.object_storage import create_object_storage
from weora.bootstrap.settings import AppSettings
from weora.shared.infrastructure.database import Base
from weora.shared.object_storage import ObjectKey

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("WEORA_RUN_ALIYUN_OSS_INTEGRATION") != "1",
        reason="未显式启用阿里云 OSS 真实联调",
    ),
]


@pytest.mark.anyio
async def test_skill_upload_publish_and_download_against_real_bucket() -> None:
    """真实执行草稿上传、版本复制、草稿清理和发布包下载。"""
    settings = AppSettings()
    storage = create_object_storage(settings)
    assert storage is not None
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app(
        settings,
        session_factory=session_factory,
        object_storage_factory=lambda _: storage,
    )
    version_key: ObjectKey | None = None

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            register = await client.post(
                "/api/v1/auth/register",
                json={
                    "display_name": "OSS 联调用户",
                    "email": "skill-oss-integration@example.com",
                    "password": "integration password",
                    "workspace_name": "OSS 联调工作空间",
                },
            )
            assert register.status_code == 201
            workspace_id = register.json()["workspace"]["id"]
            csrf_token = client.cookies[settings.csrf_cookie_name]
            base_path = f"/api/v1/workspaces/{workspace_id}/skills"

            created = await client.post(
                base_path,
                files={
                    "package": (
                        "meeting-summary.skill",
                        build_skill_package(),
                        "application/zip",
                    )
                },
                headers={"X-CSRF-Token": csrf_token},
            )
            assert created.status_code == 201, created.text
            skill_id = created.json()["id"]
            draft = created.json()["draft"]
            draft_key = ObjectKey(
                f"skills/{workspace_id}/{skill_id}/draft/{draft['content_sha256']}.skill"
            )
            assert await storage.stat(draft_key) is not None

            published = await client.post(
                f"{base_path}/{skill_id}/publish",
                json={"version": "0.1.0", "changelog": "真实 OSS 首次联调"},
                headers={"X-CSRF-Token": csrf_token},
            )
            assert published.status_code == 201, published.text
            version = published.json()
            version_key = ObjectKey(
                f"skills/{workspace_id}/{skill_id}/versions/0.1.0/{version['content_sha256']}.skill"
            )
            assert await storage.stat(draft_key) is None
            assert await storage.stat(version_key) is not None

            downloaded = await client.get(
                f"{base_path}/{skill_id}/versions/0.1.0/download"
            )
            assert downloaded.status_code == 200
            assert hashlib.sha256(downloaded.content).hexdigest() == version["package_sha256"]
    finally:
        if version_key is not None:
            with suppress(Exception):
                await storage.delete(version_key)
        await storage.aclose()
        await engine.dispose()
