"""通过公开 HTTP 接口验证 Skill Registry 行为。"""

import hashlib
from collections.abc import AsyncIterator
from io import BytesIO
from typing import cast
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.support.database import enable_sqlite_foreign_keys
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.application import create_app
from weora.shared.infrastructure.database import Base
from weora.shared.object_storage.testing import InMemoryObjectStorage


def build_skill_package(
    *,
    name: str = "meeting-summary",
    description: str = "把会议记录整理为结构化纪要",
    reference_content: str = "# 输出格式\n",
    extra_files: dict[str, str] | None = None,
) -> bytes:
    """构造包含完整 Skill 目录的最小有效测试包。"""
    content = BytesIO()
    with ZipFile(content, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: {description}\n---\n\n# 使用说明\n",
        )
        archive.writestr(f"{name}/references/output.md", reference_content)
        for path, file_content in (extra_files or {}).items():
            archive.writestr(f"{name}/{path}", file_content)
    return content.getvalue()


@pytest.fixture
async def skills_client() -> AsyncIterator[AsyncClient]:
    """提供带真实 SQLAlchemy 事务和内存对象存储的隔离应用。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    storage = InMemoryObjectStorage()
    app = create_app(
        IsolatedAppSettings(environment="test", session_cookie_secure=False),
        session_factory=session_factory,
        object_storage_factory=lambda _: storage,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await engine.dispose()


async def register_workspace_owner(client: AsyncClient) -> dict[str, object]:
    """注册首个工作空间 Owner，并返回公开身份上下文。"""
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "余则成",
            "email": "owner@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "Weora 研发组",
        },
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


@pytest.mark.anyio
async def test_owner_uploads_skill_and_finds_it_in_workspace_list(
    skills_client: AsyncClient,
) -> None:
    """合法包应创建 Skill 草稿，并立即出现在当前工作空间列表中。"""
    principal = await register_workspace_owner(skills_client)
    workspace_id = principal["workspace"]["id"]  # type: ignore[index]
    csrf_token = skills_client.cookies["weora_csrf"]

    create_response = await skills_client.post(
        f"/api/v1/workspaces/{workspace_id}/skills",
        files={
            "package": (
                "meeting-summary.skill",
                build_skill_package(),
                "application/zip",
            )
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    assert create_response.status_code == 201, create_response.text
    created = create_response.json()
    assert created["slug"] == "meeting-summary"
    assert created["display_name"] == "meeting-summary"
    assert created["description"] == "把会议记录整理为结构化纪要"
    assert created["status"] == "active"
    assert created["latest_published_version"] is None
    assert created["draft"]["package_name"] == "meeting-summary.skill"
    assert len(created["draft"]["content_sha256"]) == 64
    assert [item["path"] for item in created["draft"]["files"]] == [
        "SKILL.md",
        "references/output.md",
    ]

    list_response = await skills_client.get(f"/api/v1/workspaces/{workspace_id}/skills")

    assert list_response.status_code == 200
    assert list_response.json() == {
        "items": [
            {
                "id": created["id"],
                "slug": "meeting-summary",
                "display_name": "meeting-summary",
                "description": "把会议记录整理为结构化纪要",
                "status": "active",
                "latest_published_version": None,
                "updated_at": created["updated_at"],
                "uploader_name": "余则成",
            }
        ],
        "next_cursor": None,
    }


@pytest.mark.anyio
async def test_skill_upload_requires_csrf_bound_to_current_session(
    skills_client: AsyncClient,
) -> None:
    """Cookie 会话写接口缺少 CSRF Header 时必须拒绝请求。"""
    principal = await register_workspace_owner(skills_client)
    workspace_id = principal["workspace"]["id"]  # type: ignore[index]

    response = await skills_client.post(
        f"/api/v1/workspaces/{workspace_id}/skills",
        files={"package": ("meeting-summary.skill", build_skill_package(), "application/zip")},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CSRF_REJECTED"


@pytest.mark.anyio
async def test_member_cannot_probe_another_workspace_skill_list(
    skills_client: AsyncClient,
) -> None:
    """工作空间路径与当前成员上下文不一致时统一返回 404。"""
    first = await register_workspace_owner(skills_client)
    first_workspace_id = first["workspace"]["id"]  # type: ignore[index]
    skills_client.cookies.clear()
    second_response = await skills_client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "张三",
            "email": "second@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "第二工作空间",
        },
    )
    assert second_response.status_code == 201

    response = await skills_client.get(f"/api/v1/workspaces/{first_workspace_id}/skills")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "WORKSPACE_ACCESS_DENIED"


@pytest.mark.anyio
async def test_owner_manages_immutable_skill_versions_and_downloads(
    skills_client: AsyncClient,
) -> None:
    """上传、发布、替换草稿、再次发布后，旧版本下载内容必须保持不变。"""
    principal = await register_workspace_owner(skills_client)
    workspace_id = principal["workspace"]["id"]  # type: ignore[index]
    csrf_token = skills_client.cookies["weora_csrf"]
    base_path = f"/api/v1/workspaces/{workspace_id}/skills"

    create_response = await skills_client.post(
        base_path,
        files={"package": ("meeting-summary.skill", build_skill_package(), "application/zip")},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert create_response.status_code == 201, create_response.text
    skill_id = create_response.json()["id"]

    first_publish = await skills_client.post(
        f"{base_path}/{skill_id}/publish",
        json={"version": "0.1.0", "changelog": "完成会议纪要基础模板"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert first_publish.status_code == 201, first_publish.text
    first_version = first_publish.json()
    assert first_version["version"] == "0.1.0"
    assert first_version["changelog"] == "完成会议纪要基础模板"

    first_download = await skills_client.get(f"{base_path}/{skill_id}/versions/0.1.0/download")
    assert first_download.status_code == 200
    assert first_download.headers["content-disposition"].endswith('filename="meeting-summary-0.1.0.skill"')
    assert first_download.headers["x-content-type-options"] == "nosniff"
    assert hashlib.sha256(first_download.content).hexdigest() == first_version["package_sha256"]

    invalid_version = await skills_client.post(
        f"{base_path}/{skill_id}/publish",
        json={"version": "01.0.0", "changelog": "非法版本不应发布"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert invalid_version.status_code == 422
    assert invalid_version.json()["detail"]["code"] == "SKILL_VERSION_INVALID"

    same_draft = await skills_client.post(
        f"{base_path}/{skill_id}/draft",
        files={"package": ("meeting-summary.skill", build_skill_package(), "application/zip")},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert same_draft.status_code == 200
    duplicate_version = await skills_client.post(
        f"{base_path}/{skill_id}/publish",
        json={"version": "0.1.0", "changelog": "重复版本不应覆盖"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert duplicate_version.status_code == 409
    assert duplicate_version.json()["detail"]["code"] == "SKILL_VERSION_CONFLICT"
    preserved_download = await skills_client.get(f"{base_path}/{skill_id}/versions/0.1.0/download")
    assert preserved_download.content == first_download.content

    replace_response = await skills_client.post(
        f"{base_path}/{skill_id}/draft",
        files={
            "package": (
                "meeting-summary-v2.skill",
                build_skill_package(reference_content="# 新输出格式\n- 决策\n- 待办\n"),
                "application/zip",
            )
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert replace_response.status_code == 200, replace_response.text
    assert replace_response.json()["content_sha256"] != first_version["content_sha256"]

    second_publish = await skills_client.post(
        f"{base_path}/{skill_id}/publish",
        json={"version": "0.1.1", "changelog": "补充决策与待办输出"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert second_publish.status_code == 201, second_publish.text

    versions_response = await skills_client.get(f"{base_path}/{skill_id}/versions")
    assert versions_response.status_code == 200
    assert [item["version"] for item in versions_response.json()["items"]] == [
        "0.1.1",
        "0.1.0",
    ]

    repeated_first_download = await skills_client.get(f"{base_path}/{skill_id}/versions/0.1.0/download")
    assert repeated_first_download.content == first_download.content

    detail_response = await skills_client.get(f"{base_path}/{skill_id}")
    assert detail_response.status_code == 200
    assert detail_response.json()["draft"] is None
    assert detail_response.json()["latest_published_version"] == "0.1.1"


@pytest.mark.anyio
async def test_owner_updates_metadata_archives_restores_and_reads_events(
    skills_client: AsyncClient,
) -> None:
    """详情管理操作应更新状态，并产生可追踪的工作空间审计事件。"""
    principal = await register_workspace_owner(skills_client)
    workspace_id = principal["workspace"]["id"]  # type: ignore[index]
    csrf_token = skills_client.cookies["weora_csrf"]
    base_path = f"/api/v1/workspaces/{workspace_id}/skills"
    created = await skills_client.post(
        base_path,
        files={"package": ("meeting-summary.skill", build_skill_package(), "application/zip")},
        headers={"X-CSRF-Token": csrf_token},
    )
    skill_id = created.json()["id"]

    update_response = await skills_client.patch(
        f"{base_path}/{skill_id}",
        json={"display_name": "会议纪要助手", "description": "沉淀团队会议决策与行动项"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert update_response.status_code == 200
    assert update_response.json()["display_name"] == "会议纪要助手"

    archive_response = await skills_client.post(
        f"{base_path}/{skill_id}/archive",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert archive_response.status_code == 200
    assert archive_response.json()["status"] == "archived"

    archived_publish = await skills_client.post(
        f"{base_path}/{skill_id}/publish",
        json={"version": "0.1.0", "changelog": "归档资产不可发布"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert archived_publish.status_code == 409
    assert archived_publish.json()["detail"]["code"] == "SKILL_ARCHIVED"

    restore_response = await skills_client.post(
        f"{base_path}/{skill_id}/restore",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert restore_response.status_code == 200
    assert restore_response.json()["status"] == "active"

    search_response = await skills_client.get(base_path, params={"search": "纪要"})
    assert search_response.status_code == 200
    assert [item["id"] for item in search_response.json()["items"]] == [skill_id]

    events_response = await skills_client.get(f"{base_path}/{skill_id}/events")
    assert events_response.status_code == 200
    assert [item["event_type"] for item in events_response.json()["items"]] == [
        "SKILL_RESTORED",
        "SKILL_ARCHIVED",
        "SKILL_METADATA_UPDATED",
        "SKILL_CREATED",
    ]


@pytest.mark.anyio
async def test_skill_list_uses_opaque_cursor_and_rejects_damaged_cursor(
    skills_client: AsyncClient,
) -> None:
    """列表游标应稳定翻页，且损坏游标不能退化成不可预测查询。"""
    principal = await register_workspace_owner(skills_client)
    workspace_id = principal["workspace"]["id"]  # type: ignore[index]
    csrf_token = skills_client.cookies["weora_csrf"]
    base_path = f"/api/v1/workspaces/{workspace_id}/skills"
    for name in ("skill-alpha", "skill-bravo", "skill-charlie"):
        response = await skills_client.post(
            base_path,
            files={"package": (f"{name}.skill", build_skill_package(name=name), "application/zip")},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 201

    first_page = await skills_client.get(base_path, params={"limit": 2})
    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert len(first_payload["items"]) == 2
    assert first_payload["next_cursor"]

    second_page = await skills_client.get(
        base_path,
        params={"limit": 2, "cursor": first_payload["next_cursor"]},
    )
    assert second_page.status_code == 200
    second_payload = second_page.json()
    assert len(second_payload["items"]) == 1
    assert second_payload["next_cursor"] is None
    assert {item["id"] for item in first_payload["items"]}.isdisjoint({item["id"] for item in second_payload["items"]})

    damaged = await skills_client.get(base_path, params={"cursor": "not-a-valid-cursor"})
    assert damaged.status_code == 422
    assert damaged.json()["detail"]["code"] == "SKILL_CURSOR_INVALID"


@pytest.mark.anyio
async def test_private_key_warning_blocks_skill_publish(skills_client: AsyncClient) -> None:
    """私钥文件可以进入隔离草稿供检查，但必须阻断版本发布。"""
    principal = await register_workspace_owner(skills_client)
    workspace_id = principal["workspace"]["id"]  # type: ignore[index]
    csrf_token = skills_client.cookies["weora_csrf"]
    base_path = f"/api/v1/workspaces/{workspace_id}/skills"
    created = await skills_client.post(
        base_path,
        files={
            "package": (
                "unsafe-skill.skill",
                build_skill_package(
                    name="unsafe-skill",
                    extra_files={"secrets/deploy.pem": "-----BEGIN PRIVATE KEY-----\n"},
                ),
                "application/zip",
            )
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert created.status_code == 201
    assert any(item["blocks_publish"] for item in created.json()["draft"]["warnings"])

    publish = await skills_client.post(
        f"{base_path}/{created.json()['id']}/publish",
        json={"version": "0.1.0", "changelog": "不应发布"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert publish.status_code == 422
    assert publish.json()["detail"]["code"] == "SKILL_DRAFT_NOT_READY"
