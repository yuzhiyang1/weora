"""验证项目工作流终态留存、重跑替换、取消及权限。"""

import asyncio

import pytest

from tests.api.test_agent_mail import device
from tests.api.test_projects import project, repository
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import member

pytestmark = pytest.mark.anyio


async def test_workflow_digest_counts_latest_eligible_attempt(runs: RunsHarness) -> None:
    await exercise_workflow_activity(runs)


async def exercise_workflow_activity(runs: RunsHarness, *, postgres: bool = False) -> None:
    project_id = str((await project(runs))["id"])
    other = {"project_id": str((await project(runs, "工作流统计隔离"))["id"])}
    repo = await repository(runs, project_id, "knowledge", git_url="https://github.com/Example/Knowledge.git")
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    collab = f"/api/v1/workspaces/{runs.workspace_id}/agent-collaboration"
    _, token = await device(runs.client, collab, "可信工作流上报")
    scope = {"project_id": project_id}
    route, digest = f"{root}/activity/workflows", f"{root}/digest"
    first = {
        "repository_id": repo["id"], "connection_version": repo["connection_version"],
        "repository": "example/knowledge",
        "run_id": "789721530", "attempt": 1, "workflow": "self-improvement", "outcome": "failed", "duration_ms": 1100,
    }

    async def record(payload: dict[str, object]) -> str:
        response = await runs.client.put(route, headers=token, params=scope, json=payload)
        assert response.status_code == 200, response.text
        return str(response.json()["status"])

    assert (await runs.client.get(digest, params=scope)).json()["workflows"] is None
    rejected = await runs.client.put(route, headers=token, params=scope, json=first | {"repository": "notice/fake"})
    assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "KNOWLEDGE_WORKFLOW_REPOSITORY"
    stale = await runs.client.put(route, headers=token, params=scope, json=first | {"connection_version": 99})
    assert stale.status_code == 409
    assert (await runs.client.put(route, headers=token, params=other, json=first)).status_code == 409
    assert await record(first) == "recorded"
    assert (await runs.client.get(digest, params=scope)).json()["workflows"] == {
        "runs": 1, "succeeded": 0, "failed": 1, "total_duration_ms": 1100,
    }

    second = first | {"attempt": 2, "outcome": "succeeded", "duration_ms": 300}
    if postgres:
        assert sorted(await asyncio.gather(record(second), record(second))) == ["already_recorded", "recorded"]
    else:
        assert await record(second) == "recorded"
    assert await record(first) == "already_recorded"
    contradiction = await runs.client.put(route, headers=token, params=scope, json=second | {"duration_ms": 301})
    assert contradiction.status_code == 409
    # 先收到较新的取消结果，再补发较早失败，当前统计仍使用最新终态。
    cancelled = first | {"run_id": "789721531", "attempt": 2, "outcome": "cancelled", "duration_ms": 40}
    await record(cancelled)
    await record(cancelled | {"attempt": 1, "outcome": "failed", "duration_ms": 100})
    skipped = first | {"run_id": "789721532"}
    await record(skipped)
    await record(skipped | {"attempt": 2, "outcome": "skipped", "duration_ms": 0})
    await record(first | {"run_id": "789721533", "duration_ms": 70})
    async with member(runs, "工作流周报成员") as (teammate, _):
        _, member_device = await device(teammate, collab, "普通统计读取者")
        forbidden = await teammate.put(route, params=scope, headers=member_device, json=second)
        assert forbidden.status_code == 403
        response = await teammate.get(digest, params=scope)
        assert response.status_code == 200 and str(first["run_id"]) not in response.text
        assert response.json()["workflows"] == {
            "runs": 3, "succeeded": 1, "failed": 1, "total_duration_ms": 410,
        }
        assert (await teammate.get(digest, params=other)).json()["workflows"] is None
        assert (await teammate.get(digest)).json()["workflows"] is None
    policy = await runs.client.put(f"{root}/settings/usage-report", headers=runs.headers(), json={"enabled": False})
    assert policy.status_code == 200
    refused = await runs.client.put(route, headers=token, params=scope, json=first | {"attempt": 3})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "KNOWLEDGE_USAGE_DISABLED"
