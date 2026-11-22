"""项目召回默认隔离，显式配置才引入工作空间共享知识。"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from tests.api.test_agent_mail import device
from tests.api.test_projects import project, repository, task
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora.modules.knowledge.models import KnowledgeProjectRecallPolicy, KnowledgeRecallRun
from weora.modules.runs.application.service import RunActor
from weora.modules.tools.application.gateway import ToolScope

pytestmark = pytest.mark.anyio


async def test_project_recall_scope_applies_to_hits_preview_links_open_and_device_queue(runs: RunsHarness) -> None:
    workspace = f"/api/v1/workspaces/{runs.workspace_id}"
    root = f"{workspace}/knowledge"
    settings = f"{root}/settings/recall-scope"
    scope = {"project_id": str((await project(runs, "召回范围"))["id"])}
    other = {"project_id": str((await project(runs, "另一个召回范围"))["id"])}
    maintainer = runs.headers()
    _, paired = await device(runs.client, f"{workspace}/agent-collaboration", "召回范围验收设备")

    async def publish(title: str, params: dict[str, str], **values: object) -> str:
        response = await runs.client.post(root, params=params, headers=maintainer, json={
            "operation_id": str(uuid4()), "title": title, "body": "回归检查使用固定对象。", **values,
        })
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    shared = await publish("meteorvector procedure", {}, source={"type": "manual", "resource_path": "learnings/shared"})
    current = await publish("stormrelay runbook", scope, relations=[{"kind": "REFERENCES", "target": shared}])
    other_hit = await publish("meteorvector procedure", other)
    assert (await runs.client.get(settings, params=scope)).json() == {"inherit_shared": False, "revision": 0}
    assert (await runs.client.get(settings)).status_code == 422
    missing_scope = {"project_id": str(uuid4())}
    assert (await runs.client.get(settings, params=missing_scope)).status_code == 404
    async with member(runs, "普通项目成员") as (teammate, _):
        forbidden = await teammate.put(settings, params=scope, headers=headers(teammate), json={"inherit_shared": True})
        assert forbidden.status_code == 403, forbidden.text

    query = {"query": "meteorvector", "check": True, "check_details": True}
    preview = await runs.client.post(f"{root}/recall/preview", params=scope, headers=maintainer, json=query)
    assert preview.status_code == 200 and preview.json()["relevant"] is False
    project_result = await runs.client.get(root, params=scope | {"query": "stormrelay", "recall_mode": "1"})
    [published] = project_result.json()["items"]
    assert published["id"] == current and published["relations"] == []
    assert (await runs.client.get(f"{root}/{shared}", params=scope | {"recall_mode": "1"})).status_code == 404
    assert (await runs.client.get(f"{root}/{shared}", params=scope)).status_code == 200
    audit = {"query": "stormrelay", "session": "scope-open-proof"}
    read = await runs.client.post(f"{root}/by-path/open", params=scope, headers=maintainer, json={
        "session": audit["session"], "path": "learnings/shared", "recall_mode": True,
    })
    assert read.status_code == 404, read.text

    identifier = str(uuid4())
    queue = {"queued_learnings": [{
        "operation_id": identifier, "project_id": None, "path": "learnings/queued-shared", "matches": True,
    }]}
    local = await runs.client.post(f"{root}/recall/preview", params=scope, headers=paired, json=query | queue)
    assert local.json()["queued_visible"] == [] and local.json()["relevant"] is False
    async with runs.sessions() as db:
        assert await db.scalar(select(KnowledgeRecallRun.id).where(
            KnowledgeRecallRun.workspace_id == UUID(runs.workspace_id),
        )) is None

    async def set_inheritance(enabled: bool) -> dict[str, object]:
        response = await runs.client.put(settings, params=scope, headers=paired, json={"inherit_shared": enabled})
        assert response.status_code == 200, response.text
        result: dict[str, object] = response.json()
        assert result["inherit_shared"] is enabled
        return result

    enabled = await set_inheritance(True)
    assert await set_inheritance(True) == enabled
    inherited = await runs.client.post(f"{root}/recall", params=scope, headers=paired, json={
        "query": "meteorvector", "session": "enabled-scope",
    })
    assert [row["id"] for row in inherited.json()["items"]] == [shared]
    separate = await runs.client.get(root, params=other | {"query": "meteorvector", "recall_mode": "1"})
    assert [item["id"] for item in separate.json()["items"]] == [other_hit]
    opened = await runs.client.post(f"{root}/{shared}/open", params=scope, headers=paired, json={
        "session": "enabled-scope", "recall_mode": True,
    })
    assert opened.status_code == 200, opened.text
    local = await runs.client.post(f"{root}/recall/preview", params=scope, headers=paired, json=query | queue)
    assert local.json()["queued_visible"] == [identifier] and local.json()["relevant"] is True
    linked = await runs.client.post(f"{root}/recall", params=scope, headers=maintainer, json=audit)
    assert [link["target"] for link in linked.json()["items"][0]["relations"]] == [shared]

    disabled = await set_inheritance(False)
    assert disabled["revision"] != enabled["revision"]
    new = await runs.client.get(root, params=scope | {"query": "meteorvector", "recall_mode": "1"})
    assert new.json()["items"] == []
    manual = await runs.client.get(root, params=scope | {"query": "meteorvector"})
    assert [row["id"] for row in manual.json()["items"]] == [shared]
    async with runs.sessions() as db:
        rows = list(await db.scalars(select(KnowledgeProjectRecallPolicy).where(
            KnowledgeProjectRecallPolicy.workspace_id == UUID(runs.workspace_id),
        ).order_by(KnowledgeProjectRecallPolicy.id)))
        assert [row.inherit_shared for row in rows] == [True, False]
        assert {row.updated_by for row in rows} == {UUID(runs.user_id)}


async def test_platform_knowledge_tools_use_current_project_inheritance(runs: RunsHarness) -> None:
    project_id = str((await project(runs, "平台 Agent 范围"))["id"])
    scope = {"project_id": project_id}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    note = await runs.client.post(root, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "platformmeteor instructions", "body": "共享团队知识。",
    })
    assert note.status_code == 201, note.text
    ident = note.json()["id"]
    registered = await repository(runs, project_id, "knowledge-tool-fixture")
    created = await task(runs, project_id, [registered["id"]])
    assert created.status_code == 201, created.text
    run_id = UUID(created.json()["id"])
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    lease = await runs.app.state.execution_store.claim(
        workspace_id=actor.workspace_id, run_id=run_id, owner="knowledge-scope-tool", lease_seconds=60,
    )
    assert lease is not None
    tool = ToolScope(run_id, actor, lease.owner, lease.fencing_token)
    gateway = runs.app.state.tool_gateway

    for stage, inherit in enumerate((False, True, False)):
        if stage:
            updated = await runs.client.put(
                f"{root}/settings/recall-scope", params=scope, headers=runs.headers(), json={"inherit_shared": inherit},
            )
            assert updated.status_code == 200, updated.text
        found = await gateway.invoke(tool, f"search-{stage}", "knowledge.search", {"query": "platformmeteor"})
        assert [item["id"] for item in found["items"]] == ([ident] if inherit else [])
        read = await gateway.invoke(tool, f"read-{stage}", "knowledge.read", {"id": ident})
        if inherit:
            assert read["id"] == ident and read["body"] == "共享团队知识。"
        else:
            assert read["error"]["code"] == "KNOWLEDGE_NOT_FOUND"
