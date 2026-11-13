"""验证真实设备凭据、固定版本下载和轮次去重。"""

import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.api.test_agent_mail import device
from tests.api.test_skills import build_skill_package
from tests.support.database import enable_sqlite_foreign_keys
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.application import create_app
from weora.modules.skills.infrastructure.models import SkillPackageModel, SkillVersionModel
from weora.shared.infrastructure.database import Base
from weora.shared.object_storage import BytesObjectSource, ObjectKey
from weora.shared.object_storage.testing import InMemoryObjectStorage


@pytest.fixture
async def sharing_client(request: pytest.FixtureRequest) -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    postgres = os.environ.get("WEORA_SKILL_TEST_DATABASE_URL")
    engine = (
        create_async_engine(postgres)
        if postgres
        else create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    )
    if not postgres:
        enable_sqlite_foreign_keys(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    app = create_app(
        IsolatedAppSettings(
            environment="test",
            session_cookie_secure=False,
            workspace_invite_key=SecretStr("skill-test-invite-key" * 3),
        ),
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        object_storage_factory=lambda _: (
            InMemoryObjectStorage() if getattr(request, "param", None) == "legacy" else None
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, app
    await engine.dispose()


@pytest.mark.anyio
async def test_member_contributes_and_owner_reviews_without_overwriting_newer_draft(
    sharing_client: tuple[AsyncClient, FastAPI],
) -> None:
    """覆盖成员发布与审计、候选包审核、草稿修订冲突和上传人快照。"""
    owner, app = sharing_client
    registered = await owner.post(
        "/api/v1/auth/register",
        json={
            "display_name": "维护人",
            "email": f"{uuid4().hex}@example.com",
            "password": "a separate isolated test password",
            "workspace_name": "候选包验收",
        },
    )
    assert registered.status_code == 201, registered.text
    workspace = registered.json()["workspace"]["id"]
    root = f"/api/v1/workspaces/{workspace}"
    owner_csrf = {"X-CSRF-Token": owner.cookies["weora_csrf"]}
    invite = await owner.post(f"/api/v1/auth/workspaces/{workspace}/invitations", headers=owner_csrf, json={})
    assert invite.status_code == 201, invite.text
    upload_path = root + "/skill-distribution/contributions"
    original = build_skill_package(
        extra_files={
            "agents/openai.yaml": 'interface:\n  display_name: "会议纪要助手"\n  short_description: "提取决策与待办"\n',
        }
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member:
        joined = await member.post(
            "/api/v1/auth/invitations/register",
            json={
                "token": invite.json()["token"],
                "display_name": "林晓",
                "email": f"{uuid4().hex}@example.com",
                "password": "a separate isolated test password",
            },
        )
        assert joined.status_code == 201, joined.text
        assert joined.json()["workspace"]["role"] == "member"
        member_id = joined.json()["user"]["id"]
        member_csrf = {"X-CSRF-Token": member.cookies["weora_csrf"]}
        device_id, auth = await device(member, root + "/agent-collaboration", "贡献者设备")
        files = {"package": ("meeting-summary.zip", original)}
        assert (await member.post(upload_path, headers=auth, files=files)).status_code == 403
        assert (
            await member.put(
                f"{root}/skill-distribution/devices/{device_id}/grant",
                headers=member_csrf,
                json={},
            )
        ).status_code == 200
        assert (
            await member.post(upload_path.replace(workspace, str(uuid4())), headers=auth, files=files)
        ).status_code == 401
        first = await member.post(upload_path, headers=auth, files=files)
        assert first.status_code == 201, first.text
        assert first.json()["status"] == "draft"
        assert (await member.post(upload_path, headers=auth, files=files)).json() == first.json()
        skill_path = f"{root}/skills/{first.json()['skill_id']}"
        draft_preview = await member.get(skill_path + "/files/preview", params={"path": "SKILL.md"})
        assert draft_preview.status_code == 200 and "# 使用说明" in draft_preview.json()["text"]
        summary = (await member.get(root + "/skills", params={"search": "会议纪要助手"})).json()["items"][0]
        assert (summary["display_name"], summary["description"], summary["uploader_name"]) == (
            "会议纪要助手",
            "提取决策与待办",
            "林晓",
        )
        publish = {"version": "1.0.0", "changelog": "首次发布"}
        assert (await member.post(skill_path + "/publish", json=publish)).status_code == 403
        assert (
            await member.post(
                skill_path.replace(workspace, str(uuid4())) + "/publish", headers=member_csrf, json=publish,
            )
        ).status_code == 404
        release = await member.post(skill_path + "/publish", headers=member_csrf, json=publish)
        assert release.status_code == 201, release.text
        assert release.json()["uploader_name"] == "林晓"
        assert release.json()["published_by"] == member_id
        events = await owner.get(skill_path + "/events")
        assert events.status_code == 200, events.text
        publication = next(item for item in events.json()["items"] if item["event_type"] == "SKILL_PUBLISHED")
        assert (publication["actor_id"], publication["actor_name"]) == (member_id, "林晓")
        assert publication["data"]["actor_name"] == "林晓"
        preview = await member.get(skill_path + "/files/preview", params={"version": "1.0.0", "path": "SKILL.md"})
        assert preview.status_code == 200 and "# 使用说明" in preview.json()["text"]
        assert (
            await member.get(
                skill_path + "/files/preview",
                params={"version": "1.0.0", "path": "../SKILL.md"},
            )
        ).status_code == 404

        replacement = {"package": ("meeting-summary.zip", build_skill_package(reference_content="# 第二版"))}
        proposed = await member.post(upload_path, headers=auth, files=replacement)
        assert proposed.status_code == 201, proposed.text
        candidate_id = proposed.json()["contribution_id"]
        candidate_path = f"{skill_path}/contributions/{candidate_id}"
        assert (await member.get(candidate_path)).status_code == 403
        assert (
            await member.post(candidate_path + "/decision", headers=member_csrf, json={"accept": True})
        ).status_code == 403
        assert (await member.post(upload_path, headers=auth, files=replacement)).json() == proposed.json()
        assert (await owner.get(skill_path)).json()["draft"] is None
        candidate = await owner.get(candidate_path)
        assert candidate.status_code == 200 and candidate.json()["uploader_name"] == "林晓"
        assert (await owner.post(skill_path + "/draft", headers=owner_csrf, files=files)).status_code == 200
        stale = await owner.post(candidate_path + "/decision", headers=owner_csrf, json={"accept": True})
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "SKILL_CONTRIBUTION_STALE"
        assert (
            await owner.post(candidate_path + "/decision", headers=owner_csrf, json={"accept": False})
        ).status_code == 204
        resubmitted = await member.post(upload_path, headers=auth, files=replacement)
        assert resubmitted.status_code == 201, resubmitted.text
        candidate_id = resubmitted.json()["contribution_id"]
        accepted = await owner.post(
            f"{skill_path}/contributions/{candidate_id}/decision",
            headers=owner_csrf,
            json={"accept": True},
        )
        assert accepted.status_code == 204, accepted.text
        assert (await owner.get(skill_path)).json()["draft"]["uploader_name"] == "林晓"
        stale_publish = await owner.post(
            skill_path + "/publish",
            headers=owner_csrf,
            json={"version": "1.1.0", "changelog": "审核通过", "draft_sha256": "0" * 64},
        )
        assert stale_publish.status_code == 409
        assert stale_publish.json()["detail"]["code"] == "SKILL_DRAFT_CHANGED"
        released = await owner.post(
            skill_path + "/publish",
            headers=owner_csrf,
            json={"version": "1.1.0", "changelog": "审核通过"},
        )
        assert released.status_code == 201 and released.json()["uploader_name"] == "林晓"
        assert released.json()["published_by"] == registered.json()["user"]["id"]
        assert (await owner.get(skill_path + "/contributions")).json() == []

        unsafe = await member.post(
            upload_path,
            headers=auth,
            files={"package": ("unsafe-skill.zip", build_skill_package(
                name="unsafe-skill", extra_files={"secrets/deploy.pem": "-----BEGIN PRIVATE KEY-----\n"},
            ))},
        )
        assert unsafe.status_code == 201, unsafe.text
        unsafe_path = f"{root}/skills/{unsafe.json()['skill_id']}"
        private_file = {"path": "secrets/deploy.pem"}
        assert (await member.get(unsafe_path + "/files/preview", params=private_file)).status_code == 403
        unsafe_release = await member.post(unsafe_path + "/publish", headers=member_csrf, json=publish)
        assert unsafe_release.status_code == 422
        assert unsafe_release.json()["detail"]["code"] == "SKILL_DRAFT_NOT_READY"


@pytest.mark.anyio
async def test_device_download_and_versioned_usage(sharing_client: tuple[AsyncClient, FastAPI]) -> None:
    client, app = sharing_client
    registration = await client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "Skill test",
            "email": f"{uuid4().hex}@example.com",
            "password": "a separate isolated test password",
            "workspace_name": "Skill sharing test",
        },
    )
    assert registration.status_code == 201, registration.text
    principal = registration.json()
    workspace = principal["workspace"]["id"]
    root = f"/api/v1/workspaces/{workspace}"
    device_id, authorization = await device(client, root + "/agent-collaboration", "isolated device")
    distribution = root + "/skill-distribution"
    csrf = {"X-CSRF-Token": client.cookies["weora_csrf"]}

    query = {"runtime": "codex"}
    assert (await client.get(distribution + "/manifest", params=query, headers=authorization)).status_code == 403
    grant_path = f"{distribution}/devices/{device_id}/grant"
    grant = await client.put(grant_path, json={"usage_enabled": True}, headers=csrf)
    assert grant.status_code == 200, grant.text

    created = await client.post(
        root + "/skills",
        headers=csrf,
        files={
            "package": ("meeting-summary.skill", build_skill_package(), "application/zip"),
        },
    )
    assert created.status_code == 201, created.text
    skill_id = created.json()["id"]
    publish = await client.post(
        f"{root}/skills/{skill_id}/publish",
        headers=csrf,
        json={
            "version": "1.0.0",
            "changelog": "first",
        },
    )
    assert publish.status_code == 201, publish.text
    release = publish.json()
    manifest = await client.get(distribution + "/manifest", params=query, headers=authorization)
    assert manifest.json()["items"][0]["version_id"] == release["id"]
    downloaded = await client.get(
        f"{distribution}/versions/{release['id']}/package",
        headers=authorization,
    )
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == release["package_sha256"]

    observed = datetime.now(UTC)
    install = {
        "operation_id": str(uuid4()),
        "slot_id": str(uuid4()),
        "installation_id": str(uuid4()),
        "version_id": release["id"],
        "runtime": "codex",
        "occurred_at": observed.isoformat(),
    }
    receipt_path = distribution + "/installation-receipts"
    response = await client.post(receipt_path, headers=authorization, json=install)
    assert response.json() == {"status": "accepted"}
    assert (await client.post(receipt_path, headers=authorization, json=install)).json() == {"status": "duplicate"}

    event = {
        "installation_id": install["installation_id"],
        "runtime": "codex",
        "profile_id": "1" * 64,
        "session_key": "2" * 64,
        "turn_key": "3" * 64,
        "kind": "mentioned",
        "occurred_at": observed.isoformat(),
    }
    evidence = {"events": [event, event | {"kind": "loaded"}, event | {"kind": "loaded"}]}
    response = await client.post(distribution + "/usage-events/batch", headers=authorization, json=evidence)
    assert response.status_code == 200, response.text
    assert [item["status"] for item in response.json()["items"]] == ["accepted", "accepted", "duplicate"]
    stats = f"{root}/skills/{skill_id}/usage"
    period = {"from": (observed - timedelta(days=1)).isoformat(), "to": (observed + timedelta(days=1)).isoformat()}
    for group_by in ("day", "user", "version"):
        result = await client.get(stats, params=period | {"group_by": group_by, "timezone": "Asia/Shanghai"})
        assert result.status_code == 200, result.text
        data = result.json()
        assert len(data["groups"]) == 1
        for key in ("uses", "mentions", "loads", "users"):
            assert data["totals"][key] == data["groups"][0][key] == 1

    # 新代次关闭旧代次；上报到达顺序可变，服务端按实际观测时间判断。
    draft = await client.post(
        f"{root}/skills/{skill_id}/draft",
        headers=csrf,
        files={"package": ("meeting-summary.skill", build_skill_package(reference_content="# V2"), "application/zip")},
    )
    assert draft.status_code == 200, draft.text
    next_release = await client.post(
        f"{root}/skills/{skill_id}/publish",
        headers=csrf,
        json={"version": "2.0.0", "changelog": "second"},
    )
    assert next_release.status_code == 201, next_release.text
    next_at = observed + timedelta(seconds=1)
    switch = install | {
        "operation_id": str(uuid4()),
        "installation_id": str(uuid4()),
        "previous_id": install["installation_id"],
        "version_id": next_release.json()["id"],
        "occurred_at": next_at.isoformat(),
    }
    assert (await client.post(receipt_path, headers=authorization, json=switch)).json() == {"status": "accepted"}
    response = await client.post(
        distribution + "/usage-events/batch",
        headers=authorization,
        json={
            "events": [
                event | {"occurred_at": next_at.isoformat()},
                event | {"occurred_at": next_at.isoformat(), "installation_id": switch["installation_id"]},
            ]
        },
    )
    assert [row["status"] for row in response.json()["items"]] == ["rejected", "accepted"]
    versions = (await client.get(stats, params=period | {"group_by": "version"})).json()
    assert {row["key"] for row in versions["groups"]} == {release["id"], next_release.json()["id"]}
    assert versions["totals"]["users"] == 1 and versions["totals"]["uses"] == 2

    invite = await client.post(
        f"/api/v1/auth/workspaces/{workspace}/invitations",
        headers=csrf,
        json={"expires_in_hours": 24},
    )
    assert invite.status_code == 201, invite.text
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member:
        joined = await member.post(
            "/api/v1/auth/invitations/register",
            json={
                "display_name": "另一位使用者",
                "email": f"{uuid4().hex}@example.com",
                "password": "a separate isolated test password",
                "token": invite.json()["token"],
            },
        )
        assert joined.status_code == 201, joined.text
        member_id = joined.json()["user"]["id"]
        peer_device, peer_auth = await device(member, root + "/agent-collaboration", "另一台设备")
        peer_csrf = {"X-CSRF-Token": member.cookies["weora_csrf"]}
        peer_grant = await member.put(
            f"{distribution}/devices/{peer_device}/grant",
            headers=peer_csrf,
            json={"usage_enabled": True},
        )
        assert peer_grant.status_code == 200
        for response in (
            await member.get(f"{root}/skills/{skill_id}/versions/1.0.0/download"),
            await member.get(f"{distribution}/versions/{release['id']}/package", headers=peer_auth),
        ):
            assert response.status_code == 200
        activities = (await client.get(f"{root}/skills/{skill_id}/events")).json()["items"]
        downloads = [
            item
            for item in activities
            if item["event_type"] == "SKILL_DOWNLOAD_REQUESTED" and item["actor_id"] == member_id
        ]
        assert len(downloads) == 2
        assert all(item["actor_name"] == item["data"]["actor_name"] == "另一位使用者" for item in downloads)
        # 已有的审计事件没有姓名快照，仍可通过当前工作空间解析。
        created_event = next(item for item in activities if item["event_type"] == "SKILL_CREATED")
        assert created_event["actor_name"] == "Skill test"
        foreign = await member.post(distribution + "/usage-events/batch", headers=peer_auth, json={"events": [event]})
        assert foreign.json()["items"][0]["status"] != "accepted"
        peer_install = install | {
            "operation_id": str(uuid4()),
            "slot_id": str(uuid4()),
            "installation_id": str(uuid4()),
        }
        assert (await member.post(receipt_path, headers=peer_auth, json=peer_install)).status_code == 200
        peer_event = event | {"installation_id": peer_install["installation_id"]}
        collected = await member.post(
            distribution + "/usage-events/batch",
            headers=peer_auth,
            json={"events": [peer_event]},
        )
        assert collected.json()["items"][0]["status"] == "accepted"
        own = (await member.get(stats, params=period | {"group_by": "user"})).json()
        assert [row["key"] for row in own["groups"]] == [member_id]
        denied = await member.get(stats, params=period | {"user_id": principal["user"]["id"]})
        assert denied.status_code == 403
    all_users = (await client.get(stats, params=period | {"group_by": "user"})).json()
    assert {row["key"] for row in all_users["groups"]} == {principal["user"]["id"], member_id}
    assert all_users["totals"]["users"] == 2 and all_users["totals"]["uses"] == 3

    archived = await client.post(f"{root}/skills/{skill_id}/archive", headers=csrf)
    assert archived.status_code == 200
    assert (await client.get(distribution + "/manifest", params=query, headers=authorization)).json()["items"] == []
    assert (
        await client.get(
            f"{distribution}/versions/{release['id']}/package",
            headers=authorization,
        )
    ).status_code == 404
    assert (await client.delete(grant_path, headers=csrf)).status_code == 204
    assert (await client.get(distribution + "/manifest", params=query, headers=authorization)).status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize("sharing_client", ["legacy"], indirect=True)
async def test_legacy_download_backfills_verified_bytes(sharing_client: tuple[AsyncClient, FastAPI]) -> None:
    client, app = sharing_client
    registration = await client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "Legacy Skill test",
            "email": f"{uuid4().hex}@example.com",
            "password": "a separate isolated test password",
            "workspace_name": "Historical Skill test",
        },
    )
    assert registration.status_code == 201
    workspace = UUID(registration.json()["workspace"]["id"])
    path = f"/api/v1/workspaces/{workspace}/skills"
    csrf = {"X-CSRF-Token": client.cookies["weora_csrf"]}
    created = await client.post(
        path,
        headers=csrf,
        files={
            "package": (
                "meeting-summary.skill",
                build_skill_package(extra_files={"agents/openai.yaml": "interface:\n  display_name: 旧会议助手\n"}),
                "application/zip",
            ),
        },
    )
    assert created.status_code == 201
    skill_path = f"{path}/{created.json()['id']}"
    release = await client.post(f"{skill_path}/publish", headers=csrf, json={"version": "1.0.0", "changelog": "first"})
    assert release.status_code == 201
    payload = release.json()
    sessions: async_sessionmaker[AsyncSession] = app.state.db_session_factory
    storage: InMemoryObjectStorage = app.state.object_storage
    key = ObjectKey(f"skills/{workspace}/historical.skill")
    locator = (workspace, payload["package_sha256"])
    async with sessions() as db, db.begin():
        package = await db.get(SkillPackageModel, locator)
        version = await db.get(SkillVersionModel, UUID(payload["id"]))
        assert package and version
        original = package.archive_bytes
        version.storage_key = key.value
        version.interface = None
        await db.delete(package)
    await storage.put(key, BytesObjectSource(b"tampered", content_type="application/zip"))
    download = f"{skill_path}/versions/1.0.0/download"
    invalid = await client.get(download)
    assert invalid.status_code == 503
    assert invalid.json()["detail"]["code"] == "SKILL_OBJECT_MISSING"
    async with sessions() as db:
        assert await db.get(SkillPackageModel, locator) is None
    await storage.delete(key)
    await storage.put(key, BytesObjectSource(original, content_type="application/zip"))
    assert (await client.get(download)).content == original
    assert (await client.get(skill_path)).json()["display_name"] == "旧会议助手"
    await storage.delete(key)
    assert (await client.get(download)).content == original
