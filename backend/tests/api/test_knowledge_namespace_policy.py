"""覆盖清单权限、两类目录合并、直接访问、规则覆盖及设备交付。"""

import asyncio
from uuid import uuid4

import pytest

from tests.api.test_agent_mail import device
from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_skills import build_skill_package
from tests.api.test_task_collaboration import headers, member

pytestmark = pytest.mark.anyio


async def test_role_and_project_directory_manifest(runs: RunsHarness) -> None:
    workspace = runs.workspace_id
    root = f"/api/v1/workspaces/{workspace}"
    path = f"{root}/knowledge"
    settings = f"{path}/settings/resource-namespaces"
    project_id = str((await project(runs, "搜索项目"))["id"])
    scope = {"project_id": project_id}
    auth = runs.headers()

    async def entry(kind: str, logical: str, title: str, body: str = "召回夹具正文。") -> str:
        created = await runs.client.post(path, headers=auth, json={
            "operation_id": str(uuid4()), "kind": kind, "title": title, "body": body,
            "source": {"type": "file" if kind != "learning" else "manual", "resource_path": logical},
        })
        assert created.status_code == 201, created.text
        identity = str(created.json()["id"])
        if kind != "learning":
            reviewed = await runs.client.post(f"{path}/{identity}/review", headers=auth,
                json={"revision": 1, "decision": "accept"})
            assert reviewed.status_code == 200, reviewed.text
        return identity

    cases = {
        "general": await entry("doc", "docs/handbook/common.md", "未声明目录文档"),
        "engineering": await entry("doc", "docs/eng_team/service.md", "工程文档"),
        "risk": await entry("doc", "docs/risk/billing.md", "风险文档"),
        "root_rule": await entry("rule", "rules/release.md", "共享规则"),
        "role_rule": await entry("rule", "rules/eng_team/release.md", "职能发布规则"),
        "other_rule": await entry("rule", "rules/eng-team/release.md", "相近目录规则"),
        "project_learning": await entry("learning", "learnings/payment/note", "支付项目经验"),
        "private_learning": await entry("learning", "learnings/risk/ops", "风险经验"),
    }

    def package(slug: str, caption: str) -> dict:
        return {"package": (f"{slug}.skill", build_skill_package(name=slug, description=caption), "application/zip")}

    async def skill(slug: str, directory: str, caption: str) -> tuple[str, str]:
        created = await runs.client.post(
            f"{root}/skills", headers=auth, data={"namespace": directory}, files=package(slug, caption),
        )
        assert created.status_code == 201, created.text
        identity = str(created.json()["id"])
        version = await runs.client.post(f"{root}/skills/{identity}/publish", headers=auth, json={
            "version": "1.0.0", "changelog": "命名空间验收发布",
        })
        assert version.status_code == 201, version.text
        return identity, str(version.json()["id"])

    await skill("release-procedure", "", "共享发布 Skill")
    engineering, _ = await skill("release-procedure", "eng_team/platform", "工程发布 Skill")
    risk, risk_version = await skill("risk-procedure", "risk/finance", "风险流程 Skill")
    link = await entry(
        "doc", "docs/handbook/links.md", "关系夹具",
        "参看 [风险技能](../../skills/risk/finance/risk-procedure/SKILL.md)。",
    )

    async with member(runs, "工程目录成员") as (engineer, user):
        identity = str(user)
        manifest = {
            "base_revision": 0,
            "roles": {
                "engineer": {"knowledge": ["eng_team"], "docs": ["eng_team"], "skills": ["eng_team"]},
                "risk": {"knowledge": ["risk"], "docs": ["risk"], "learnings": ["risk"], "skills": ["risk"]},
            },
            "members": {identity: "engineer"},
            "projects": {project_id: {"docs": ["risk"], "learnings": ["payment"], "skills": ["risk"]}},
        }
        assert (await engineer.put(settings, headers=headers(engineer), json=manifest)).status_code == 403
        invalid = await runs.client.put(settings, headers=auth, json=manifest | {"members": {str(uuid4()): "engineer"}})
        assert invalid.status_code == 422
        wrong = await runs.client.put(settings, headers=auth, json=manifest | {"projects": {str(uuid4()): {}}})
        assert wrong.status_code == 404

        async def save(payload=manifest):
            return await runs.client.put(settings, headers=auth, json=payload)

        bind = runs.sessions.kw.get("bind")
        if bind is not None and bind.dialect.name == "postgresql":
            first, replayed = await asyncio.gather(save(), save())
            assert first.status_code == replayed.status_code == 200
            assert first.json() == replayed.json()
        else:
            first = await save()
            assert first.status_code == 200, first.text
            replayed = await save()
            assert replayed.json() == first.json()
        assert first.json()["revision"] == 1
        assert (await save(manifest | {"members": {}})).status_code == 409
        active = await engineer.get(settings, params=scope)
        assert active.status_code == 200, active.text
        assert set(active.json()) == {"effective"}
        assert active.json()["effective"]["docs"] == ["eng_team", "risk"]
        assert "inactive_docs" not in active.json()["effective"]
        assert active.json()["effective"]["learnings"] == ["payment"]

        async def search(client, params: dict[str, str] | None = None) -> set[str]:
            response = await client.get(path, params=params)
            assert response.status_code == 200, response.text
            return {item["id"] for item in response.json()["items"]}

        own = await search(engineer)
        assert {cases["general"], cases["engineering"], cases["role_rule"], engineering} <= own
        assert not {cases["risk"], cases["root_rule"], cases["other_rule"], cases["private_learning"], risk} & own
        selected = await search(engineer, scope)
        assert {cases["engineering"], cases["risk"], cases["project_learning"], engineering, risk} <= selected
        default = await search(runs.client)
        assert {cases["root_rule"], cases["general"]} <= default
        assert cases["role_rule"] not in default and cases["engineering"] not in default
        assert (await engineer.get(f"{path}/{cases['risk']}")).status_code == 404
        assert (await engineer.get(f"{path}/{link}")).json()["relations"] == []
        assert (await engineer.get(f"{path}/{link}", params=scope)).json()["relations"][0]["target"] == risk
        unavailable = await engineer.get(f"{path}/by-path", params={"path": "skills/risk/finance/risk-procedure"})
        assert unavailable.status_code == 404

        payload = {"operation_id": str(uuid4()), "title": "命名空间经验", "body": "按权限保存资源。"}
        target = payload | {"source": {"type": "manual", "namespace": "payment"}}
        denied = await engineer.post(path, headers=headers(engineer), json=target)
        assert denied.status_code == 403
        explicit = payload | {"source": {"type": "manual", "resource_path": "learnings/payment/explicit"}}
        assert (await engineer.post(path, headers=headers(engineer), json=explicit)).status_code == 403
        contributed = await engineer.post(path, params=scope, headers=headers(engineer), json=target)
        assert contributed.status_code == 201, contributed.text
        logical = f"learnings/payment/{payload['operation_id']}"
        assert contributed.json()["source"]["resource_path"] == logical
        readback = await engineer.get(f"{path}/by-path", params=scope | {"path": logical})
        assert readback.status_code == 200 and readback.json()["id"] == contributed.json()["id"]

        device_id, device_header = await device(engineer, f"{root}/agent-collaboration", "工程设备")
        grant = await engineer.put(f"{root}/skill-distribution/devices/{device_id}/grant",
            headers=headers(engineer), json={"usage_enabled": True})
        assert grant.status_code == 200, grant.text
        sync = f"{root}/skill-distribution"
        shared = await engineer.get(f"{sync}/manifest", params={"runtime": "codex"}, headers=device_header)
        assert shared.status_code == 200, shared.text
        delivered = [row["resource_path"] for row in shared.json()["items"]]
        assert delivered == ["skills/eng_team/platform/release-procedure"]
        target_package = f"{sync}/versions/{risk_version}/package"
        assert (await engineer.get(target_package, headers=device_header)).status_code == 404
        assert (await engineer.get(target_package, params=scope, headers=device_header)).status_code == 200
        push = await engineer.post(f"{sync}/contributions", headers=device_header, data={"namespace": "risk/finance"},
            files=package("new-risk-procedure", "未启用的目录"))
        assert push.status_code == 403

        await skill("release-procedure", "risk/finance", "同名风险 Skill")
        conflict = await engineer.get(f"{sync}/manifest", params=scope | {"runtime": "codex"}, headers=device_header)
        assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "SKILL_NAMESPACE_CONFLICT"
        assert engineering not in await search(engineer, scope)

        changed = await save(manifest | {"base_revision": 1, "members": {identity: "risk"}})
        assert changed.status_code == 200 and changed.json()["revision"] == 2
        assert (await engineer.get(f"{path}/{engineering}")).status_code == 404
        assert (await engineer.get(f"{path}/{risk}")).status_code == 200
        response = await engineer.post(f"{path}/{cases['role_rule']}/feedback", headers=headers(engineer),
            json={"session": "hidden-feedback", "positive": True})
        assert response.status_code == 404
