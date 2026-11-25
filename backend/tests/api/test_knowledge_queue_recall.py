"""验证本机待发送经验只投影到当前设备的召回结果。"""

from uuid import uuid4

import pytest

from tests.api.test_agent_mail import device
from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs

pytestmark = pytest.mark.anyio


async def test_queued_learning_projects_scope_and_published_journal(runs: RunsHarness) -> None:
    workspace = f"/api/v1/workspaces/{runs.workspace_id}"
    root = workspace + "/knowledge"
    project_id = str((await project(runs, "当前召回项目"))["id"])
    other = str((await project(runs, "其他召回项目"))["id"])
    scope = {"project_id": project_id}
    headers = runs.headers()
    _, paired = await device(runs.client, workspace + "/agent-collaboration", "离线经验验收设备")

    async def contribute(title: str, path: str, params: dict[str, str], operation: str) -> str:
        out = await runs.client.post(root, params=params, headers=headers, json={
            "operation_id": operation, "title": title, "body": "A published knowledge note.",
            "source": {"type": "manual", "resource_path": path},
        })
        assert out.status_code == 201, out.text
        return str(out.json()["id"])

    inherited = await contribute("Atlas original", "learnings/atlas.md", {}, str(uuid4()))
    other_hit = await contribute("Atlas checkpoint", "learnings/checkpoint.md", scope, str(uuid4()))
    alias, outside, forbidden, inherited_local = (str(uuid4()) for _ in range(4))
    manifest = await runs.client.put(root + "/settings/resource-namespaces", headers=headers, json={
        "base_revision": 0, "projects": {project_id: {"learnings": ["release"]}, other: {"learnings": ["private"]}},
    })
    assert manifest.status_code == 200, manifest.text
    queued: list[dict[str, str | bool | None]] = [
        {"operation_id": alias, "project_id": project_id, "path": "learnings/atlas", "matches": True},
        {"operation_id": outside, "project_id": other, "path": "learnings/outside", "matches": True},
        {"operation_id": forbidden, "project_id": None,
         "path": "learnings/private/note", "matches": True},
        {"operation_id": inherited_local, "project_id": None,
         "path": "learnings/checkpoint", "matches": True},
    ]
    request = {"query": "Atlas", "recall_mode": True, "session": "queue-recall", "queued_learnings": queued}
    denied = await runs.client.post(root + "/recall", params=scope, headers=headers, json=request)
    assert denied.status_code == 403
    preview = await runs.client.post(root + "/recall/preview", params=scope, headers=paired, json={
        "query": "local only", "check": True, "queued_learnings": queued,
    })
    assert preview.json() == {"relevant": True, "queued_visible": [alias]}
    explain = await runs.client.post(root + "/recall/preview", params=scope, headers=paired, json={
        "query": "local only", "check": True, "check_details": True, "queued_learnings": queued,
    })
    assert explain.json() == {
        "relevant": True, "score": None, "threshold": None, "reason": "local-queue", "queued_visible": [alias],
    }
    local_only = await runs.client.post(root + "/recall", params=scope, headers=paired, json=request | {"limit": 1})
    assert local_only.json() == {"items": [], "queued_visible": [alias]}

    mixed = await runs.client.post(root + "/recall", params=scope, headers=paired, json=request | {"limit": 2})
    assert mixed.status_code == 200, mixed.text
    assert [item["id"] for item in mixed.json()["items"]] == [other_hit]
    assert mixed.json()["queued_visible"] == [alias]
    stats = await runs.client.post(root + "/recall/stats", params=scope, headers=paired, json={
        "session": "queue-recall",
    })
    assert stats.status_code == 200, stats.text
    snapshot, = stats.json()["items"]
    assert snapshot["runs"] == 1 and snapshot["recalled"] == 1

    # 本机新正文未匹配本次查询时，旧版也不会因为恰好匹配而重新出现。
    older_query = await runs.client.post(root + "/recall/preview", params=scope, headers=paired, json={
        "query": "Atlas", "recall_mode": True, "queued_learnings": [queued[0] | {"matches": False}],
    })
    assert older_query.status_code == 200, older_query.text
    assert [item["id"] for item in older_query.json()["items"]] == [other_hit]

    # 模拟平台已提交但 CLI 未收到回执。正式版本直接接替这条本机结果。
    published = await contribute("Atlas synchronized", "learnings/atlas.md", scope, alias)
    recovered = await runs.client.post(root + "/recall", params=scope, headers=paired, json=request | {"limit": 2})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["queued_visible"] == []
    assert {item["id"] for item in recovered.json()["items"]} == {published, other_hit}
    assert (await runs.client.get(f"{root}/{inherited}")).status_code == 200


async def test_default_device_contribution_keeps_queued_path(runs: RunsHarness) -> None:
    workspace = f"/api/v1/workspaces/{runs.workspace_id}"
    root = workspace + "/knowledge"
    scope = {"project_id": str((await project(runs, "设备贡献路径"))["id"])}
    _, paired = await device(runs.client, workspace + "/agent-collaboration", "验证默认路径")
    operation = str(uuid4())
    logical = f"learnings/{operation}"
    payload = {
        "operation_id": operation, "title": "水星发布说明", "body": "Record the upload outcome.",
    }
    submitted = await runs.client.post(root, params=scope, headers=paired, json=payload)
    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["source"]["resource_path"] == logical
    retrieved = await runs.client.get(root + "/by-path", params=scope | {"path": logical + ".md"}, headers=paired)
    assert retrieved.status_code == 200 and retrieved.json()["id"] == submitted.json()["id"]
    replay = await runs.client.post(root, params=scope, headers=paired, json=payload)
    assert replay.status_code == 201 and replay.json()["id"] == submitted.json()["id"], replay.text

    # 旧条目仍用原路径，设备重试同一请求时复用历史记录。
    historical_payload = payload | {"operation_id": str(uuid4())}
    historical = await runs.client.post(root, params=scope, headers=runs.headers(), json=historical_payload)
    assert historical.status_code == 201, historical.text
    old_id = historical.json()["id"]
    same = await runs.client.post(root, params=scope, headers=paired, json=historical_payload)
    assert same.status_code == 201 and same.json()["id"] == old_id, same.text
    old = await runs.client.get(root + "/by-path", params=scope | {"path": f"learnings/{old_id}"}, headers=paired)
    assert old.status_code == 200 and old.json()["id"] == old_id, old.text
