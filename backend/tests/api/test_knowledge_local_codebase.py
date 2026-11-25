"""通过正常项目入口验证本地代码快照及其派生知识。"""

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs

pytestmark = pytest.mark.anyio


async def test_local_snapshot_reuses_code_and_archives_removed_paths(runs: RunsHarness) -> None:
    team, other = await project(runs, "工作目录"), await project(runs, "其他项目")
    scope = {"project_id": str(team["id"])}
    other_scope = {"project_id": str(other["id"])}
    knowledge = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    endpoint = f"{knowledge}/codebases/local"
    files = [
        {"path": "app.py", "text": "from billing import estimate\ndef checkout_local(): return estimate()\n"},
        {"path": "billing.py", "text": "# PRIVATE_FIXTURE_BODY\ndef estimate(): return 1\n"},
    ]
    snapshot: dict[str, object] = {
        "source_key": "a" * 64,
        "name": "源码项目",
        "incremental": True,
        "candidates": 2,
        "files": files,
    }
    assert (await runs.client.post(endpoint, headers=runs.headers(), json=snapshot)).status_code == 422
    bad = snapshot | {"candidates": 1, "files": [{"path": "../app.py", "text": "PRIVATE_FIXTURE_BODY"}]}
    error = await runs.client.post(endpoint, params=scope, json=bad, headers=runs.headers())
    assert error.status_code == 422 and "PRIVATE_FIXTURE_BODY" not in error.text
    first = await runs.client.post(endpoint, params=scope, json=snapshot, headers=runs.headers())
    assert first.status_code == 200, first.text
    initial = first.json()
    assert (initial["extracted"], initial["generation"], initial["source"]) == (2, 1, "local_directory")
    codebase = f"{endpoint}/{initial['codebase_id']}"
    duplicate = await runs.client.post(endpoint, params=scope, json=snapshot, headers=runs.headers())
    assert duplicate.status_code == 200 and duplicate.json()["generation"] == 1
    assert duplicate.json()["reused"] == 2
    results = (await runs.client.get(knowledge, params=scope | {"query": "checkout_local"})).json()["items"]
    caller = next(result for result in results if result["title"] == "app.py")
    assert caller["source"]["snapshot"] == initial["revision"] and caller["source"]["freshness"] == "unknown"
    assert any(edge["kind"] == "REFERENCES" and edge["title"] == "billing.py" for edge in caller["relations"])
    detail = f"{knowledge}/{caller['id']}"
    assert (await runs.client.get(detail, params=other_scope)).status_code == 404
    assert (await runs.client.get(codebase, params=other_scope)).status_code == 404
    assert (await runs.client.get(f"{knowledge}/codebases", params=scope)).json()["items"][0]["name"] == "源码项目"
    assert (await runs.client.get(knowledge, params=scope | {"query": "PRIVATE_FIXTURE_BODY"})).json()["items"] == []

    snapshot["candidates"] = 1
    files.pop()
    changed = await runs.client.post(endpoint, params=scope, json=snapshot, headers=runs.headers())
    assert changed.status_code == 200 and changed.json()["generation"] == 2
    assert (changed.json()["reused"], changed.json()["removed"]) == (1, 1)
    after = (await runs.client.get(detail, params=scope)).json()
    assert after["revision"] == 2 and all(item["title"] != "billing.py" for item in after["relations"])
    inspected = (await runs.client.get(codebase, params=scope | {"lint": "true"})).json()
    assert inspected["indexed"] == 1 and inspected["problems"] == []

    emptied = await runs.client.post(
        endpoint,
        params=scope,
        json=snapshot | {"files": [], "candidates": 0},
        headers=runs.headers(),
    )
    assert emptied.status_code == 200 and emptied.json()["removed"] == 1
    assert (await runs.client.get(detail, params=scope)).status_code == 404
