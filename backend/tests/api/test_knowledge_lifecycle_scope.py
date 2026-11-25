"""项目维护只处理实际可见的共享及项目资源，审核始终归属于源条目的范围。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora.modules.knowledge.models import KnowledgeEntry

pytestmark = pytest.mark.anyio


async def test_project_lifecycle_includes_visible_shared_entries_and_retains_origin(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    current = str((await project(runs, "发布项目"))["id"])
    other = str((await project(runs, "其它项目"))["id"])
    selected = {"project_id": current}
    combined = selected | {"include_shared": "true"}
    token = runs.headers()

    async def publish(title: str, path: str, owner: str | None = None, *, kind: str = "learning") -> str:
        query = {"project_id": owner} if owner else {}
        response = await runs.client.post(root, params=query, headers=token, json={
            "operation_id": str(uuid4()), "title": title, "kind": kind, "body": "A stable reusable process.",
            "source": {"type": "manual", "resource_path": path},
        })
        assert response.status_code == 201, response.text
        item = str(response.json()["id"])
        if kind != "learning":
            review = await runs.client.post(f"{root}/{item}/review", params=query, headers=token, json={
                "decision": "accept", "revision": 1,
            })
            assert review.status_code == 200, review.text
        return item

    shared = await publish("Spectral 日常发布经验", "learnings/common.md")
    namespace = await publish("接口签名处理", "learnings/delivery/interface.md")
    hidden = await publish("内部运维系统", "learnings/ops/hidden.md")
    local = await publish("Spectral 本项目部署说明", "learnings/team.md", current)
    outside = await publish("其它项目专有流程", "learnings/outside.md", other)
    shadowed = await publish("Polaris 被覆盖的旧流程", "learnings/duplicate.md")
    covering = await publish("Polaris 项目覆盖流程", "learnings/duplicate", current)
    shared_cold = await publish("待停用共享示例", "learnings/cold.md")
    local_cold = await publish("待停用项目示例", "learnings/local-cold.md", current)
    shared_doc = await publish("Spectral audit shared", "docs/audit-shared.md", kind="doc")
    local_doc = await publish("Spectral audit project", "docs/audit-project.md", current, kind="doc")
    # 同路径的项目规则不能阻止共享经验在其原作用域晋升。
    occupying = await publish("项目已有规则", "rules/common.md", current, kind="rule")

    manifest = await runs.client.put(root + "/settings/resource-namespaces", headers=token, json={
        "base_revision": 0, "projects": {
            current: {"learnings": ["delivery"], "knowledge": ["delivery"], "skills": ["delivery"]},
            other: {"learnings": ["ops"]},
        },
    })
    assert manifest.status_code == 200, manifest.text

    # 两个场景都显式开启共享召回，才会按各项目的命名空间回退。
    for ident in (current, other):
        inherit = await runs.client.put(
            root + "/settings/recall-scope", params={"project_id": ident}, headers=token, json={"inherit_shared": True},
        )
        assert inherit.status_code == 200, inherit.text

    # 召回与按路径打开使用同一套扩展名别名规则。
    for params, expected_id in ((selected, covering), ({}, shadowed), ({"project_id": other}, shadowed)):
        recalled = await runs.client.get(root, params=params | {"query": "Polaris", "recall_mode": "true"})
        assert recalled.status_code == 200, recalled.text
        assert {row["id"] for row in recalled.json()["items"]} == {expected_id}
        by_path = await runs.client.get(root + "/by-path", params=params | {"path": "learnings/duplicate.md"})
        assert by_path.status_code == 200 and by_path.json()["id"] == expected_id, by_path.text

    async with member(runs, "第二位反馈成员") as (teammate, _):
        for entry, owner in ((shared, None), (namespace, current), (local, current), (hidden, other), (outside, other)):
            for index in range(5):
                actor = runs.client if index < 3 else teammate
                response = await actor.post(
                    f"{root}/{entry}/feedback", params={"project_id": owner} if owner else {},
                    headers=headers(actor), json={"session": f"up-{index}", "positive": True},
                )
                assert response.status_code == 204, response.text
        # 真正的召回只给活动范围记分，继承搜索结果不会抬高共享经验的活动分。
        for index in range(5):
            actor = runs.client if index < 3 else teammate
            for params, expected in ((selected, {shared_doc, local_doc}), ({}, {shared_doc})):
                response = await actor.post(root + "/recall", params=params, headers=headers(actor), json={
                    "query": "spectral audit", "session": f"quality-{index}", "recall_mode": True,
                })
                assert response.status_code == 200, response.text
                assert expected <= {row["id"] for row in response.json()["items"]}

        for entry, owner in ((shared_cold, None), (local_cold, current), (shadowed, None)):
            response = await runs.client.post(
                f"{root}/{entry}/feedback", params={"project_id": owner} if owner else {},
                headers=token, json={"session": "low", "positive": False},
            )
            assert response.status_code == 204, response.text

        async with runs.sessions.begin() as db:
            await db.execute(update(KnowledgeEntry).where(KnowledgeEntry.id.in_([
                UUID(entry) for entry in (shared, namespace, hidden, local, outside)
            ])).values(created_at=datetime.now(UTC) - timedelta(days=15)))

        path = root + "/maintenance/promote"
        exact = await teammate.get(path, params=selected)
        assert {row["id"] for row in exact.json()["items"]} == {local}
        cursor = None
        current_candidates: dict[str, str | None] = {}
        while True:
            params = combined | {"limit": "1"} | ({"after": cursor} if cursor else {})
            batch = await teammate.get(path, params=params)
            assert batch.status_code == 200, batch.text
            for row in batch.json()["items"]:
                current_candidates[row["id"]] = row["project_id"]
            cursor = batch.json()["next_after"]
            if cursor is None:
                break
        assert current_candidates == {shared: None, namespace: None, local: current}
        assert (await teammate.get(path + f"/{hidden}", params=combined)).status_code == 404

        original = await teammate.get(path + f"/{shared}", params=combined)
        assert original.json()["project_id"] is None and "rule" not in original.json()["occupied_kinds"]
        assert original.json()["targets"]["rule"] == "rules/common.md"
        nested = await teammate.get(path + f"/{namespace}", params=combined)
        assert nested.json()["targets"] == {
            "rule": "rules/delivery/interface.md", "doc": "docs/delivery/interface.md",
            "skill": "skills/delivery/interface/SKILL.md",
        }
        nomination = {
            "operation_id": str(uuid4()), "source_revision": original.json()["revision"], "kind": "doc",
            "reason": "转为工作空间参考。", "body": "Published reference for stable guidewire operations.",
        }
        wrong = await teammate.post(path + f"/{shared}", params=selected, headers=headers(teammate), json=nomination)
        assert wrong.status_code == 404
        submitted = await teammate.post(path + f"/{shared}", headers=headers(teammate), json=nomination)
        assert submitted.status_code == 201, submitted.text
        promoted = submitted.json()["id"]
        assert submitted.json()["project_id"] is None and submitted.json()["status"] == "pending"
        assert (await teammate.get(f"{root}/{promoted}", params=selected)).status_code == 404
        approved = await runs.client.post(f"{root}/{promoted}/review", headers=token, json={
            "decision": "accept", "revision": 1,
        })
        assert approved.status_code == 200, approved.text
        assert (await teammate.get(f"{root}/{promoted}", params=selected)).json()["body"] == nomination["body"]
        denied = await teammate.post(root + "/maintenance/archive", params=combined, headers=headers(teammate))
        assert denied.status_code == 403

    quality = await runs.client.get(root + "/maintenance/quality", params=combined)
    assert {item["id"]: item["project_id"] for item in quality.json()["items"]} == {
        shared_doc: None, local_doc: current,
    }
    document = await runs.client.get(root + f"/maintenance/quality/{shared_doc}", params=combined)
    assert document.status_code == 200, document.text
    assert document.json()["project_id"] is None
    assert {item["id"] for item in document.json()["related"]} == {shared}
    project_document = await runs.client.get(root + f"/maintenance/quality/{local_doc}", params=combined)
    assert {item["id"] for item in project_document.json()["related"]} == {shared, local}

    report = await runs.client.get(root + "/health", params=combined)
    assert {row["id"] for row in report.json()["maintenance"]["prune"]["items"]} == {shared_cold, local_cold}
    digest = await runs.client.get(root + "/digest", params=combined)
    assert digest.json()["total_learnings"] == 6
    assert {row["id"] for row in digest.json()["new_learnings"]} == {covering, shared_cold, local_cold}
    assert (await runs.client.get(f"{root}/{occupying}", params=selected)).status_code == 200

    confidence = await runs.client.post(root + "/maintenance/confidence", params=combined, headers=token)
    assert {
        item["id"]: item["project_id"] for item in confidence.json()["items"]
    } == {shared: None, namespace: None, local: current, shared_cold: None, local_cold: current}
    prune = await runs.client.post(
        root + "/maintenance/archive", params=combined | {"candidates_only": "true"}, headers=token,
    )
    assert {row["id"]: row["project_id"] for row in prune.json()["items"] if row["status"] == "archived"} == {
        shared_cold: None, local_cold: current,
    }
    assert (await runs.client.get(f"{root}/{shared_cold}")).status_code == 404
    assert (await runs.client.get(f"{root}/{shadowed}")).status_code == 200
    assert (await runs.client.get(f"{root}/{outside}", params={"project_id": other})).status_code == 200

    # 修订没有重传来源时，晋升依然使用首发目录。
    source_path = f"{root}/{namespace}"
    revision = await runs.client.post(source_path + "/revisions", headers=token, json={
        "operation_id": str(uuid4()), "kind": "learning", "title": "新版接口签名处理",
        "body": "Pin the interface configuration before publishing.", "base_revision": 1,
    })
    assert revision.status_code == 201, revision.text
    accepted = await runs.client.post(source_path + "/review", headers=token, json={
        "decision": "accept", "revision": 2,
    })
    assert accepted.status_code == 200, accepted.text
    nested = await runs.client.get(path + f"/{namespace}", params=combined)
    assert nested.json()["targets"]["skill"] == "skills/delivery/interface/SKILL.md"

    # 共享经验晋升后仍按目标资源的目录交付，项目外也不能用 ID 绕过筛选。
    skill = await runs.client.post(path + f"/{namespace}", headers=token, json=nomination | {
        "operation_id": str(uuid4()), "kind": "skill", "source_revision": 2,
    })
    assert skill.status_code == 201, skill.text
    skill_path = f"{root}/{skill.json()['id']}"
    visible = await runs.client.post(skill_path + "/review", headers=token, json={"decision": "accept", "revision": 1})
    assert visible.status_code == 200, visible.text
    assert (await runs.client.get(skill_path, params=selected)).status_code == 200
    assert (await runs.client.get(skill_path, params={"project_id": other})).status_code == 404
    assert (await runs.client.get(skill_path)).status_code == 404
    logical = {"path": "skills/delivery/interface/SKILL.md"}
    result = await runs.client.get(root + "/by-path", params=selected | logical)
    assert result.status_code == 200 and result.json()["id"] == skill.json()["id"]
    registry = f"/api/v1/workspaces/{runs.workspace_id}/skills"
    shared_skills = (await runs.client.get(registry)).json()["items"]
    assert {(row["namespace"], row["slug"]) for row in shared_skills} == {("delivery", "interface")}
    assert (await runs.client.get(root + "/by-path", params={"project_id": other} | logical)).status_code == 404

    # 归档项目副本时共享版本重新可用，恢复项目版本后重新获得优先级。
    vote = await runs.client.post(f"{root}/{covering}/feedback", params=selected, headers=token, json={
        "session": "archive-project-copy", "positive": False,
    })
    assert vote.status_code == 204, vote.text
    archived = await runs.client.post(
        root + "/maintenance/archive", params=selected | {"candidates_only": "true"}, headers=token,
    )
    assert {row["id"] for row in archived.json()["items"] if row["status"] == "archived"} == {covering}
    lookup = selected | {"path": "learnings/duplicate"}
    assert (await runs.client.get(root + "/by-path", params=lookup)).json()["id"] == shadowed
    await runs.client.post(f"{root}/{covering}/restore", params=selected, headers=token)
    assert (await runs.client.get(root + "/by-path", params=lookup)).json()["id"] == covering
