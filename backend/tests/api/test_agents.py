"""通过公开 HTTP 接口验证 Agent 资产与 SkillVersion 绑定行为。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from io import BytesIO
from typing import cast
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.support.database import enable_sqlite_foreign_keys
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.application import create_app
from weora.modules.agents.application.service import AgentActor
from weora.modules.agents.domain.errors import AgentRuntimeMaterialInvalidError
from weora.shared.infrastructure.database import Base
from weora.shared.object_storage import BytesObjectSource, ObjectKey
from weora.shared.object_storage.testing import InMemoryObjectStorage


def build_skill_package(*, reference_content: str) -> bytes:
    """构造可发布的会议纪要 Skill 测试包。"""
    content = BytesIO()
    with ZipFile(content, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "meeting-summary/SKILL.md",
            "---\n"
            "name: meeting-summary\n"
            "description: 把会议记录整理为结构化纪要\n"
            "---\n\n"
            "# 使用说明\n\n先提取决策，再整理待办。\n",
        )
        archive.writestr("meeting-summary/references/output.md", reference_content)
        archive.writestr("meeting-summary/scripts/format.py", "print('runtime tool')\n")
    return content.getvalue()


@pytest.fixture
async def agents_client() -> AsyncIterator[AsyncClient]:
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
    app = create_app(
        IsolatedAppSettings(environment="test", session_cookie_secure=False),
        session_factory=session_factory,
        object_storage_factory=lambda _: InMemoryObjectStorage(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await engine.dispose()


@dataclass(frozen=True, slots=True)
class AgentsRuntimeHarness:
    """运行时装配测试需要的应用、客户端和对象存储。"""

    client: AsyncClient
    app: FastAPI
    storage: InMemoryObjectStorage


@pytest.fixture
async def agents_runtime_harness() -> AsyncIterator[AgentsRuntimeHarness]:
    """暴露应用服务，以验证未来 Pi Adapter 使用的内部契约。"""
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
        yield AgentsRuntimeHarness(client=client, app=app, storage=storage)
    await engine.dispose()


async def register_workspace_owner(client: AsyncClient) -> dict[str, object]:
    """注册工作空间 Owner，并返回公开身份上下文。"""
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "余则成",
            "email": "agent-owner@example.com",
            "password": "correct horse battery staple",
            "workspace_name": "Weora Agent 研发组",
        },
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


async def publish_skill_version(
    client: AsyncClient,
    *,
    workspace_id: object,
    version: str,
    reference_content: str,
    skill_id: str | None = None,
) -> tuple[str, dict[str, object]]:
    """创建或替换 Skill 草稿，并发布指定不可变版本。"""
    csrf_token = client.cookies["weora_csrf"]
    base_path = f"/api/v1/workspaces/{workspace_id}/skills"
    package = ("meeting-summary.skill", build_skill_package(reference_content=reference_content))
    if skill_id is None:
        draft_response = await client.post(
            base_path,
            files={"package": (*package, "application/zip")},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert draft_response.status_code == 201, draft_response.text
        skill_id = cast(str, draft_response.json()["id"])
    else:
        draft_response = await client.post(
            f"{base_path}/{skill_id}/draft",
            files={"package": (*package, "application/zip")},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert draft_response.status_code == 200, draft_response.text

    publish_response = await client.post(
        f"{base_path}/{skill_id}/publish",
        json={"version": version, "changelog": f"发布 {version}"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert publish_response.status_code == 201, publish_response.text
    return skill_id, cast(dict[str, object], publish_response.json())


@pytest.mark.anyio
async def test_agent_version_keeps_published_skill_binding_after_draft_changes(
    agents_client: AsyncClient,
) -> None:
    """Agent 发布后必须固定 SkillVersion，后续草稿改绑不能污染历史版本。"""
    principal = await register_workspace_owner(agents_client)
    workspace_id = principal["workspace"]["id"]  # type: ignore[index]
    csrf_token = agents_client.cookies["weora_csrf"]
    skill_id, skill_v1 = await publish_skill_version(
        agents_client,
        workspace_id=workspace_id,
        version="0.1.0",
        reference_content="# 输出格式\n- 决策\n",
    )
    agents_path = f"/api/v1/workspaces/{workspace_id}/agents"

    create_response = await agents_client.post(
        agents_path,
        json={
            "display_name": "会议纪要 Agent",
            "description": "把会议原文整理成团队可执行纪要",
            "system_prompt": "你是企业会议纪要助手。",
            "skill_version_ids": [skill_v1["id"]],
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    assert create_response.status_code == 201, create_response.text
    created = create_response.json()
    assert created["status"] == "active"
    assert created["latest_published_version"] is None
    assert created["draft"]["system_prompt"] == "你是企业会议纪要助手。"
    assert created["draft"]["skills"] == [
        {
            "skill_version_id": skill_v1["id"],
            "skill_id": skill_id,
            "skill_slug": "meeting-summary",
            "skill_display_name": "meeting-summary",
            "version": "0.1.0",
            "content_sha256": skill_v1["content_sha256"],
        }
    ]

    publish_agent = await agents_client.post(
        f"{agents_path}/{created['id']}/publish",
        json={"version": "0.1.0", "changelog": "首个可运行版本"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert publish_agent.status_code == 201, publish_agent.text
    assert publish_agent.json()["skills"][0]["skill_version_id"] == skill_v1["id"]

    _, skill_v2 = await publish_skill_version(
        agents_client,
        workspace_id=workspace_id,
        skill_id=skill_id,
        version="0.2.0",
        reference_content="# 输出格式\n- 决策\n- 待办\n",
    )
    update_draft = await agents_client.patch(
        f"{agents_path}/{created['id']}/draft",
        json={
            "system_prompt": "你是企业会议纪要助手，输出必须简洁。",
            "skill_version_ids": [skill_v2["id"]],
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert update_draft.status_code == 200, update_draft.text
    assert update_draft.json()["skills"][0]["skill_version_id"] == skill_v2["id"]

    old_version = await agents_client.get(
        f"{agents_path}/{created['id']}/versions/0.1.0"
    )
    assert old_version.status_code == 200
    assert old_version.json()["system_prompt"] == "你是企业会议纪要助手。"
    assert old_version.json()["skills"][0]["skill_version_id"] == skill_v1["id"]


@pytest.mark.anyio
async def test_runtime_context_loads_verified_skill_instructions_for_pinned_agent_version(
    agents_runtime_harness: AgentsRuntimeHarness,
) -> None:
    """未来 Pi Runtime 应拿到固定版本的 SKILL.md 和文本参考，而不是脚本内容。"""
    client = agents_runtime_harness.client
    principal = await register_workspace_owner(client)
    workspace_id = principal["workspace"]["id"]  # type: ignore[index]
    csrf_token = client.cookies["weora_csrf"]
    _, skill_version = await publish_skill_version(
        client,
        workspace_id=workspace_id,
        version="0.1.0",
        reference_content="# 输出格式\n- 决策\n- 待办\n",
    )
    agents_path = f"/api/v1/workspaces/{workspace_id}/agents"
    created = await client.post(
        agents_path,
        json={
            "display_name": "会议纪要 Agent",
            "description": "把会议原文整理成团队可执行纪要",
            "system_prompt": "你是企业会议纪要助手。",
            "skill_version_ids": [skill_version["id"]],
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    published = await client.post(
        f"{agents_path}/{created.json()['id']}/publish",
        json={"version": "0.1.0", "changelog": "首个可运行版本"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert published.status_code == 201, published.text

    actor = AgentActor(
        user_id=UUID(cast(str, principal["user"]["id"])),  # type: ignore[index]
        workspace_id=UUID(cast(str, workspace_id)),
        role="owner",
    )
    context = await agents_runtime_harness.app.state.agent_registry_service.build_runtime_context(
        actor=actor,
        workspace_id=UUID(cast(str, workspace_id)),
        agent_id=UUID(created.json()["id"]),
        version="0.1.0",
    )

    assert str(context.agent_version_id) == published.json()["id"]
    assert context.system_prompt == "你是企业会议纪要助手。"
    assert len(context.skills) == 1
    assert context.skills[0].name == "meeting-summary"
    assert context.skills[0].version == "0.1.0"
    assert "先提取决策，再整理待办" in context.skills[0].instructions
    assert [(item.path, item.content) for item in context.skills[0].references] == [
        ("references/output.md", "# 输出格式\n- 决策\n- 待办\n")
    ]
    assert "format.py" not in context.skills[0].instructions


@pytest.mark.anyio
async def test_agent_rejects_missing_and_duplicate_skill_versions(
    agents_client: AsyncClient,
) -> None:
    """草稿不能用不存在的 ID 伪装已发布 SkillVersion，也不能重复绑定。"""
    principal = await register_workspace_owner(agents_client)
    workspace_id = principal["workspace"]["id"]  # type: ignore[index]
    csrf_token = agents_client.cookies["weora_csrf"]
    agents_path = f"/api/v1/workspaces/{workspace_id}/agents"
    missing_id = str(uuid4())

    missing = await agents_client.post(
        agents_path,
        json={
            "display_name": "非法绑定 Agent",
            "description": "不应创建成功",
            "system_prompt": "测试无效绑定。",
            "skill_version_ids": [missing_id],
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "AGENT_SKILL_VERSION_INVALID"

    _, published = await publish_skill_version(
        agents_client,
        workspace_id=workspace_id,
        version="0.1.0",
        reference_content="# 输出格式\n",
    )
    duplicated = await agents_client.post(
        agents_path,
        json={
            "display_name": "重复绑定 Agent",
            "description": "不应创建成功",
            "system_prompt": "测试重复绑定。",
            "skill_version_ids": [published["id"], published["id"]],
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert duplicated.status_code == 422
    assert duplicated.json()["detail"]["code"] == "AGENT_SKILL_VERSION_INVALID"

    blank_prompt = await agents_client.post(
        agents_path,
        json={
            "display_name": "空提示词 Agent",
            "description": "不应创建成功",
            "system_prompt": "   ",
            "skill_version_ids": [],
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert blank_prompt.status_code == 422


@pytest.mark.anyio
async def test_runtime_context_rejects_skill_package_that_no_longer_matches_snapshot(
    agents_runtime_harness: AgentsRuntimeHarness,
) -> None:
    """对象被篡改或错误覆盖后，不得把未经校验的内容交给 Pi Runtime。"""
    client = agents_runtime_harness.client
    principal = await register_workspace_owner(client)
    workspace_id = UUID(cast(str, principal["workspace"]["id"]))  # type: ignore[index]
    csrf_token = client.cookies["weora_csrf"]
    _, skill_version = await publish_skill_version(
        client,
        workspace_id=workspace_id,
        version="0.1.0",
        reference_content="# 输出格式\n",
    )
    agents_path = f"/api/v1/workspaces/{workspace_id}/agents"
    created = await client.post(
        agents_path,
        json={
            "display_name": "完整性校验 Agent",
            "description": "对象被篡改时必须停止运行",
            "system_prompt": "只使用已验证的 Skill。",
            "skill_version_ids": [skill_version["id"]],
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    await client.post(
        f"{agents_path}/{created.json()['id']}/publish",
        json={"version": "0.1.0", "changelog": "验证完整性"},
        headers={"X-CSRF-Token": csrf_token},
    )
    actor = AgentActor(
        user_id=UUID(cast(str, principal["user"]["id"])),  # type: ignore[index]
        workspace_id=workspace_id,
        role="owner",
    )
    version = await agents_runtime_harness.app.state.agent_registry_service.get_version(
        actor=actor,
        workspace_id=workspace_id,
        agent_id=UUID(created.json()["id"]),
        version="0.1.0",
    )
    object_key = ObjectKey(version.skills[0].storage_key)
    await agents_runtime_harness.storage.delete(object_key)
    await agents_runtime_harness.storage.put(
        object_key,
        BytesObjectSource(
            data=b"tampered package",
            content_type="application/zip",
            filename="tampered.skill",
        ),
    )

    with pytest.raises(AgentRuntimeMaterialInvalidError):
        await agents_runtime_harness.app.state.agent_registry_service.build_runtime_context(
            actor=actor,
            workspace_id=workspace_id,
            agent_id=version.agent_id,
            version="0.1.0",
        )
