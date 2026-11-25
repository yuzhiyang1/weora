"""验证套件及提案覆盖、源证据校验和并发注册的一致性。"""

import asyncio
from uuid import uuid4

import pytest

from tests.api.test_knowledge_improvement import exercise_plan
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import member

pytestmark = pytest.mark.anyio
SUITE = "benchmarks/regressions"
SCORER = "review-quality"


async def test_benchmark_proposal_covers_and_publishes_only_valid_tasks(runs: RunsHarness) -> None:
    await exercise_benchmark(runs, "a" * 40)


async def exercise_benchmark(runs: RunsHarness, commit: str, *, postgres: bool = False) -> str:
    project = await exercise_plan(runs, commit)
    scope = {"project_id": project}
    base = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    suites, tasks, proposals = (f"{base}/benchmark/{segment}" for segment in ("suites", "tasks", "proposals"))
    candidates = f"{base}/self-improvement/candidates"
    query = scope | {"scorer": SCORER}

    async def plan(**extras: str) -> dict:
        response = await runs.client.get(candidates, params=query | extras)
        assert response.status_code == 200, response.text
        return response.json()

    before = await plan()
    item = before["subjects"][0]
    assert item["task"] is None and before["proposal_id"] is None
    usable = item["recorded"]
    assert (await runs.client.get(candidates, params=query | {"suite": SUITE})).status_code == 404
    spec = {
        "path": SUITE, "repository_id": usable["git_start"]["repository_id"],
        "tools": ["claude", "codex"], "repetitions": 3, "timeout_seconds": 7200,
    }
    h = runs.headers()
    created = await runs.client.post(suites, params=scope, headers=h, json=spec)
    assert created.status_code == 201, created.text
    assert (await runs.client.post(suites, params=scope, headers=h, json=spec)).json() == created.json()
    assert (await runs.client.post(suites, params=scope, headers=h, json=spec | {"repetitions": 4})).status_code == 409
    invalid = await runs.client.post(suites, params=scope, headers=h, json=spec | {"repository_id": str(uuid4())})
    assert invalid.status_code == 409, invalid.text
    alternate = await runs.client.post(
        suites, params=scope, headers=h, json=spec | {"path": "benchmarks/web/regressions"},
    )
    assert alternate.status_code == 201, alternate.text

    draft = {
        "operation_id": str(uuid4()), "scorer": SCORER, "label_id": usable["label_id"], "name": "fallback",
        "title": "回归任务", "prompt": "TASK_PROMPT", "starting_commit": commit,
        "success_criteria": "Identify the missing boundary case.",
    }
    for change in (
        {"starting_commit": "b" * len(commit)},
        {"label_id": before["subjects"][-1]["latest"]["label_id"]},
        {"source": {"platform": "codex", "sessionId": str(uuid4())}},
    ):
        result = await runs.client.post(tasks, params=scope, headers=h, json=draft | change)
        assert result.status_code == 422, result.text
    async with member(runs, "回归任务只读成员") as (teammate, _):
        read = await teammate.get(candidates, params=query)
        assert read.status_code == 200, read.text
        denied = await teammate.post(tasks, params=scope, headers={"X-CSRF-Token": teammate.cookies["weora_csrf"]},
                                     json=draft)
        assert denied.status_code == 403, denied.text

    saved = await runs.client.post(tasks, params=scope, headers=h, json=draft)
    assert saved.status_code == 201, saved.text
    proposal = saved.json()["proposal"]
    assert proposal["status"] == "open"
    duplicate = await runs.client.post(tasks, params=scope, headers=h, json=draft | {"operation_id": str(uuid4())})
    assert duplicate.status_code == 409, duplicate.text
    assert (await runs.client.post(tasks, params=scope, headers=h, json=draft)).json() == saved.json()
    changed = await runs.client.post(tasks, params=scope, headers=h, json=draft | {"prompt": "Different prompt"})
    assert changed.status_code == 409, changed.text
    planned = await plan(suite=SUITE)
    assert planned["proposal_id"] == proposal["id"] and planned["subjects"][0]["task"] == saved.json()["path"]
    assert "TASK_PROMPT" not in str(planned)
    alternate_plan = await plan(suite="benchmarks/web/regressions")
    assert alternate_plan["proposal_id"] is None and alternate_plan["subjects"][0]["task"] is None
    assert (await runs.client.get(suites, params=scope)).json()["tasks"] == []
    overlay = await runs.client.get(suites, params=scope | {"proposal_id": proposal["id"]})
    assert overlay.json()["tasks"] == [saved.json()]

    identifier = proposal["id"]
    closed = await runs.client.put(f"{proposals}/{identifier}", params=scope, headers=h, json={"status": "closed"})
    assert closed.status_code == 200 and closed.json()["status"] == "closed", closed.text
    assert (await plan())["subjects"][0]["task"] is None
    forbidden = await runs.client.put(f"{proposals}/{identifier}", params=scope, headers=h, json={"status": "merged"})
    assert forbidden.status_code == 409, forbidden.text

    if postgres:
        # 同一个失败同时登记，只允许一条被后续计划读取。
        requests = [runs.client.post(tasks, params=scope, headers=h, json=draft | {"operation_id": str(uuid4())})
                    for _ in range(2)]
        responses = await asyncio.gather(*requests)
        assert sorted(response.status_code for response in responses) == [201, 409]
        new = next(response for response in responses if response.status_code == 201)
    else:
        new = await runs.client.post(tasks, params=scope, headers=h, json=draft | {"operation_id": str(uuid4())})
    assert new.status_code == 201 and new.json()["proposal"]["id"] != identifier, new.text
    identifier = new.json()["proposal"]["id"]
    merged = await runs.client.put(f"{proposals}/{identifier}", params=scope, headers=h, json={"status": "merged"})
    assert merged.status_code == 200 and merged.json()["settled_by"] == runs.user_id, merged.text
    repeated = await runs.client.put(f"{proposals}/{identifier}", params=scope, headers=h, json={"status": "merged"})
    assert repeated.json() == merged.json()
    final = await plan()
    assert final["proposal_id"] is None and final["subjects"][0]["task"] == new.json()["path"]
    default = (await runs.client.get(suites, params=scope)).json()
    assert [task["id"] for task in default["tasks"]] == [new.json()["id"]]
    direct = await runs.client.post(
        tasks, params=scope | {"suite": "benchmarks/web/regressions"}, headers=h, json=draft | {"proposed": False},
    )
    assert direct.status_code == 201 and direct.json()["proposal"] is None, direct.text
    alt = await plan(suite="benchmarks/web/regressions")
    assert alt["proposal_id"] is None and alt["subjects"][0]["task"] == direct.json()["path"]
    return project
