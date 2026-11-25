"""验证规则候选、内容修订与工作空间鉴权。"""

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.api.test_agent_mail import device
from tests.api.test_skill_sharing import sharing_client as sharing_client  # noqa: F401
from weora.modules.team_resources.store import Change


@pytest.mark.anyio
async def test_team_rules_branch_and_device_delivery(sharing_client: tuple[AsyncClient, FastAPI]) -> None:
    client, app = sharing_client
    registration = await client.post(
        "/api/v1/auth/register",
        json={
            "display_name": "规则维护人",
            "email": f"{uuid4().hex}@example.com",
            "password": "isolated rules test password",
            "workspace_name": "规则验收",
        },
    )
    assert registration.status_code == 201, registration.text
    workspace = registration.json()["workspace"]["id"]
    root = f"/api/v1/workspaces/{workspace}"
    url = root + "/team-resources"
    csrf = {"X-CSRF-Token": client.cookies["weora_csrf"]}
    _, auth = await device(client, root + "/agent-collaboration", "规则验收设备")
    draft = {
        "rules/java-vo.md": "# 共享规则",
        "rules/backend/java-vo.md": "---\npaths: ['src/**/*.java']\n---\n新增字段写中文 Javadoc。",
        "rules/frontend/ui.md": "# 前端规则",
        "manifest/projects.yaml": "version: 1\nprojects:\n  - id: hsk-java\n    resources: {knowledge: [backend]}\n",
        "rules/backend/reference/RULE.md": "附件见 ${TEAMAI_RULE_DIR}/fixture.bin。",
        "rules/backend/reference/fixture.bin": {"base64": "AAH+/w=="},
    }
    payload = {"branch": "weora/push/fixture", "files": draft}
    candidate = await client.post(url + "/changes", headers=auth, json=payload)
    assert candidate.status_code == 201, candidate.text
    change = candidate.json()
    retry = await client.post(url + "/changes", headers=auth, json=payload)
    assert retry.json()["id"] == change["id"]
    assert (await client.get(url + "/snapshot", headers=auth)).json()["rules"] == []
    preview = await client.get(url + "/snapshot", headers=auth, params={"ref": change["branch"]})
    assert preview.json()["revision"] == change["revision"]
    assert preview.json()["files"]["rules/backend/reference/fixture.bin"] == {"base64": "AAH+/w=="}
    detail = await client.get(url + f"/changes/{change['id']}", headers=auth)
    assert (
        next(row["after"] for row in detail.json()["changes"] if row["path"].endswith(".bin")) == "二进制文件 · 4 字节"
    )

    invitation = await client.post(f"/api/v1/auth/workspaces/{workspace}/invitations", headers=csrf, json={})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member:
        joined = await member.post(
            "/api/v1/auth/invitations/register",
            json={
                "token": invitation.json()["token"],
                "display_name": "成员",
                "email": f"{uuid4().hex}@example.com",
                "password": "member isolated rules password",
            },
        )
        assert joined.status_code == 201, joined.text
        denied = await member.post(
            f"{url}/changes/{change['id']}/merge",
            json={"expected": change["revision"]},
            headers={"X-CSRF-Token": member.cookies["weora_csrf"]},
        )
        assert denied.status_code == 403

    merged = await client.post(
        f"{url}/changes/{change['id']}/merge", headers=csrf, json={"expected": change["revision"]}
    )
    assert merged.status_code == 200, merged.text
    live = (await client.get(url + "/snapshot", headers=auth)).json()
    assert live["revision"] == merged.json()["merged_revision"]
    assert live["projects"][0]["knowledge"] == ["backend"]
    assert next(rule for rule in live["rules"] if rule["namespace"] == "backend")["paths"] == ["src/**/*.java"]
    tag_url = url + "/refs/tags/rules/v1"
    tagged = await client.put(tag_url, headers=csrf, json={"revision": live["revision"]})
    assert tagged.status_code == 200, tagged.text
    pinned = await client.get(url + "/snapshot", headers=auth, params={"ref": "rules/v1"})
    assert pinned.json()["revision"] == live["revision"]
    for reserved_ref in ("rules/v1", live["revision"], live["revision"].replace("-", "").upper()):
        shadow = await client.post(
            url + "/changes",
            headers=auth,
            json={"branch": reserved_ref, "base": live["revision"], "files": {"rules/java-vo.md": "未合入"}},
        )
        assert shadow.status_code == 409
        assert shadow.json()["detail"]["code"] == "RESOURCE_REF_CONFLICT"
    blocked_tag = await client.put(
        url + "/refs/tags/" + change["branch"], headers=csrf, json={"revision": live["revision"]}
    )
    assert blocked_tag.status_code == 409
    assert blocked_tag.json()["detail"]["code"] == "RESOURCE_REF_CONFLICT"

    # 已存在的同名候选也不能改变旧检出的固定引用。
    async with app.state.db_session_factory() as db, db.begin():
        for legacy_ref in ("rules/v1", live["revision"]):
            db.add(
                Change(
                    workspace_id=UUID(workspace),
                    branch=legacy_ref,
                    author_id=UUID(change["author_id"]),
                    base_id=UUID(live["revision"]),
                    revision_id=UUID(change["revision"]),
                )
            )
    for fixed_ref in ("rules/v1", live["revision"]):
        fixed = await client.get(url + "/snapshot", headers=auth, params={"ref": fixed_ref})
        assert fixed.json()["revision"] == live["revision"]
    moving_tag = await client.put(tag_url, headers=csrf, json={"revision": change["revision"]})
    assert moving_tag.status_code == 409
    wrong_workspace = await client.get(f"/api/v1/workspaces/{uuid4()}/team-resources/snapshot", headers=auth)
    assert wrong_workspace.status_code == 401
    stale = await client.put(url + "/default", headers=csrf, json={"files": {"rules/other.md": "old mode"}})
    assert stale.status_code == 409
    bad_path = await client.put(
        url + "/default", headers=csrf, json={"base": live["revision"], "files": {"rules/../forbidden.md": "unsafe"}}
    )
    assert bad_path.status_code == 422
    imported = await client.put(
        url + "/default",
        headers=csrf,
        json={
            "base": live["revision"],
            "files": {
                "rules/example.md": "团队指导",
                "docs/imported-api.md": "技术说明",
                "learnings/known-problem.md": "解决方案",
            },
        },
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["files"]["docs/imported-api.md"] == "技术说明"
    published_revision = imported.json()["revision"]
    fresh = await client.post(
        url + "/changes",
        headers=auth,
        json={"branch": "weora/push/fresh", "base": published_revision, "files": {"rules/example.md": "新候选正文"}},
    )
    assert fresh.status_code == 201, fresh.text
    stale_candidate = await client.post(
        url + "/changes",
        headers=auth,
        json={
            "branch": "weora/push/fresh",
            "base": live["revision"],
            "expected": fresh.json()["revision"],
            "files": {"rules/example.md": "过期本机正文"},
        },
    )
    assert stale_candidate.status_code == 409
    assert stale_candidate.json()["detail"]["code"] == "RESOURCE_HEAD_CHANGED"
    invalid_binary = await client.put(
        url + "/default",
        headers=csrf,
        json={
            "base": imported.json()["revision"],
            "files": {
                "rules/backend/reference/fixture.bin": {"base64": "????"},
            },
        },
    )
    assert invalid_binary.json()["detail"]["code"] == "RESOURCE_BINARY_INVALID"
