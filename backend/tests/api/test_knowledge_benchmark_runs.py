"""覆盖冻结矩阵、原子结算以及双方评判不同的比较规则。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from tests.api.test_knowledge_benchmark import SUITE, exercise_benchmark
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import member
from weora.modules.knowledge.benchmark_models import KnowledgeRegressionRun

pytestmark = pytest.mark.anyio


async def test_benchmark_freezes_arms_and_excludes_unmatched_judges(runs: RunsHarness) -> None:
    await exercise_benchmark_run(runs, "a" * 40, "b" * 40)


async def exercise_benchmark_run(runs: RunsHarness, base: str, proposed: str, *, postgres: bool = False) -> str:
    project = await exercise_benchmark(runs, base, postgres=postgres)
    scope = {"project_id": project}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge/benchmark"
    suite = (await runs.client.get(f"{root}/suites", params=scope)).json()["suite"]
    plan = {
        "operation_id": str(uuid4()), "resource_repository_id": suite["repository_id"],
        "base_resources": base, "proposal_resources": proposed, "tasks": ["fallback"],
    }
    h = runs.headers()

    async def create(**changes: object) -> dict:
        response = await runs.client.post(f"{root}/runs", params=scope, headers=h, json=plan | changes)
        assert response.status_code == 201, response.text
        return response.json()

    frozen = await create()
    assert frozen == await create()
    assert frozen["gate"] == "incomplete" and frozen["status"] == "pending"
    assert frozen["base_resources"] == base and frozen["proposal_resources"] == proposed
    assert len(frozen["trials"]) == 12
    assert {slot["starting_commit"] for slot in frozen["trials"]} == {base}
    assert "TASK_PROMPT" not in str(frozen)
    path = f"{root}/runs/{frozen['id']}"
    conflict = await runs.client.post(f"{root}/runs", params=scope, headers=h, json=plan | {"proposal_resources": base})
    assert conflict.status_code == 409, conflict.text
    invalid = await runs.client.post(f"{root}/runs", params=scope, headers=h,
                                     json=plan | {"operation_id": str(uuid4()), "tasks": ["missing"]})
    assert invalid.status_code == 422, invalid.text

    inputs = await runs.client.get(f"{path}/inputs", params=scope)
    assert inputs.status_code == 200, inputs.text
    assert inputs.json()["tasks"][0]["prompt"] == "TASK_PROMPT"
    assert (await runs.client.get(path)).status_code == 404
    async with member(runs, "基准报告只读成员") as (teammate, _):
        visible = await teammate.get(path, params=scope)
        assert visible.status_code == 200 and "TASK_PROMPT" not in visible.text
        assert (await teammate.get(f"{path}/inputs", params=scope)).status_code == 403

    async def mark(run_path: str, *items: dict[str, object]):
        return await runs.client.post(f"{run_path}/results", params=scope, headers=h, json={"items": items})

    def verdict(slot: dict, *, outcome: str = "passed", model: str = "judge-v1") -> dict[str, object]:
        return {"trial_id": slot["id"], "outcome": outcome, "judge_tool": "codex", "judge_model": model}

    first = verdict(frozen["trials"][0])
    foreign = await mark(path, first, first | {"trial_id": str(uuid4())})
    assert foreign.status_code == 404, foreign.text
    assert (await runs.client.get(path, params=scope)).json()["trials"][0]["outcome"] == "not_run"
    assert (await mark(path, {"trial_id": first["trial_id"], "outcome": "passed"})).status_code == 422

    records = [verdict(slot, model="judge-v2" if i == 1 else "judge-v1") for i, slot in enumerate(frozen["trials"])]
    if postgres:
        updates = await asyncio.gather(mark(path, *records[:-1]), mark(path, records[-1]))
        assert all(response.status_code == 200 for response in updates)
    else:
        response = await mark(path, *records)
        assert response.status_code == 200, response.text
    report = (await runs.client.get(path, params=scope)).json()
    assert report["status"] == "complete" and report["gate"] == "incomplete"
    assert sum(item["excluded_judge_changed"] for item in report["comparisons"]) == 1
    assert (await mark(path, *records)).json() == report
    assert (await mark(path, first | {"outcome": "failed"})).status_code == 409

    next_run = await create(operation_id=str(uuid4()))
    next_path = f"{root}/runs/{next_run['id']}"
    graded = await mark(next_path, *[
        verdict(slot, outcome="failed" if (slot["tool"], slot["arm"]) in {("claude", "pulled"), ("codex", "main")}
                else "passed") for slot in next_run["trials"]
    ])
    assert graded.status_code == 200 and graded.json()["gate"] == "regressed", graded.text

    bound = await create(operation_id=str(uuid4()))
    bound_path = f"{root}/runs/{bound['id']}"
    setup = {
        "image": "sha256:" + "a" * 64, "network": "review-internal", "gateway": "http://fixture-gateway:8080",
        "judge_tool": "codex", "judge_model": "judge-v1", "runner_sha256": "c" * 64,
    }

    async def configure(profile: dict[str, str]):
        return await runs.client.put(f"{bound_path}/execution", params=scope, headers=h, json=profile)

    other_setup = setup | {"image": "sha256:" + "b" * 64}
    if postgres:
        contenders = await asyncio.gather(configure(setup), configure(other_setup))
        assert sorted(response.status_code for response in contenders) == [200, 409]
        bound_info = next(response.json() for response in contenders if response.status_code == 200)
    else:
        registered = await configure(setup)
        assert registered.status_code == 200, registered.text
        bound_info = registered.json()
    fixed = bound_info["profile"]
    assert bound_info["registered_by"] == runs.user_id
    assert (await configure(fixed)).json() == bound_info
    changed = await configure(fixed | {"judge_model": "judge-v2"})
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "KNOWLEDGE_BENCHMARK_CONFIG"
    assert (await configure(fixed | {"runner_sha256": "d" * 64})).status_code == 409
    assert (await configure(fixed | {"gateway": "http://user:password@fixture-gateway:8080"})).status_code == 422
    assert (await runs.client.get(f"{bound_path}/inputs", params=scope)).json()["execution"] == bound_info
    assert "fixture-gateway" not in (await runs.client.get(bound_path, params=scope)).text
    assert (await runs.client.put(f"{bound_path}/execution", headers=h, json=fixed)).status_code == 404
    late = await runs.client.put(f"{next_path}/execution", params=scope, headers=h, json=fixed)
    assert late.status_code == 409

    bound_results = [verdict(slot) | {"execution_id": bound_info["id"]} for slot in bound["trials"]]
    assert (await mark(bound_path, verdict(bound["trials"][0]))).status_code == 409
    foreign_executor = await mark(bound_path, bound_results[0], bound_results[1] | {"execution_id": str(uuid4())})
    assert foreign_executor.status_code == 409
    assert (await mark(bound_path, bound_results[0] | {"judge_model": "judge-v2"})).status_code == 409
    unseen = (await runs.client.get(bound_path, params=scope)).json()["trials"]
    assert all(row["outcome"] == "not_run" for row in unseen)
    complete = await mark(bound_path, *bound_results)
    assert complete.status_code == 200 and complete.json()["gate"] == "held"
    assert (await mark(bound_path, *bound_results)).json() == complete.json()

    expiry = await create(operation_id=str(uuid4()))
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeRegressionRun).where(
            KnowledgeRegressionRun.id == UUID(expiry["id"]),
        ).values(deadline=datetime.now(UTC) - timedelta(seconds=1)))
    expiry_path = f"{root}/runs/{expiry['id']}"
    expired = await runs.client.get(expiry_path, params=scope)
    assert expired.json()["status"] == "expired" and expired.json()["gate"] == "incomplete"
    assert (await mark(expiry_path, verdict(expiry["trials"][0]))).status_code == 409
    assert (await runs.client.get(f"{root}/suites", params=scope | {"suite": SUITE})).status_code == 200
    dashboard_path = root.removesuffix("/benchmark") + "/dashboard/benchmarks"
    history = await runs.client.get(dashboard_path, params=scope | {"days": "30", "limit": "1"})
    assert history.status_code == 200, history.text
    dashboard = history.json()
    assert dashboard["total"] == 4 and dashboard["next_offset"] == 1
    assert dashboard["suite_id"] == suite["id"]
    assert len(dashboard["runs"]) == 1
    assert sum(arm["planned"] for arm in dashboard["arms"]) == 48
    assert sum(pair["excluded_judge_changed"] for pair in dashboard["comparisons"]) == 1
    assert sum(pair["unjudged"] for pair in dashboard["comparisons"]) == 6
    assert {row["revision"] for row in dashboard["revisions"]} == {base, proposed}
    assert "TASK_PROMPT" not in history.text and "fixture-gateway" not in history.text
    assert (await runs.client.get(dashboard_path, params=scope | {"suite_id": str(uuid4())})).status_code == 404
    return project
