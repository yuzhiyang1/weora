"""核对 PR 文件、冻结提交和完整基准矩阵以后才发布。"""

import asyncio
from uuid import uuid4

import pytest

from tests.api.test_knowledge_improvement_proposals import exercise_improvement
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge import improvement_delivery

pytestmark = pytest.mark.anyio


async def test_delivery_requires_hosted_readback_and_complete_pinned_benchmark(
    runs: RunsHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await exercise_delivery(runs, monkeypatch, "a" * 40)


async def test_ci_checks_replay_and_only_publish_after_the_latest_complete_run(
    runs: RunsHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await exercise_delivery(runs, monkeypatch, "a" * 40, ci=True)


async def exercise_delivery(
    runs: RunsHarness, monkeypatch: pytest.MonkeyPatch, base: str, *, ci: bool = False, postgres: bool = False,
) -> str:
    github = "https://github.com/example/review.git"
    project, operation = await exercise_improvement(runs, base, repository_url=github)
    resume = f"/api/v1/workspaces/{runs.workspace_id}/knowledge/self-improvement/resume"
    original = {"project_id": project, "scorer": "review-quality", "operation_id": operation}
    scope = {"project_id": project}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    h = runs.headers()
    candidates = (await runs.client.get(f"{root}/self-improvement/candidates", params=scope | {
        "scorer": "review-quality",
    })).json()
    proposal = candidates["proposal_id"]
    path = f"{root}/self-improvement/proposals/{proposal}/delivery"
    specification = await runs.client.get(path, params=scope)
    assert specification.status_code == 200, specification.text
    spec = specification.json()
    assert len(spec["files"]) == 4 and spec["previous"] is None and spec["current"] is None
    head = "b" * 40
    reservation = {"revision": spec["revision"], "head_commit": head, "prefix": ""}
    reserved = await runs.client.post(path, params=scope, headers=h, json=reservation)
    assert reserved.status_code == 200, reserved.text
    pending = await runs.client.get(resume, params=original)
    assert pending.json()["status"] == "pending_delivery"
    assert (await runs.client.post(path, params=scope, headers=h, json=reservation)).json() == reserved.json()
    collision = await runs.client.post(path, params=scope, headers=h, json=reservation | {"head_commit": "c" * 40})
    assert collision.status_code == 409, collision.text

    remote = {
        "number": 17, "html_url": "https://github.com/example/review/pull/17", "body": spec["marker"],
        "head": {"ref": spec["branch"], "sha": head, "repo": {"full_name": "example/review"}},
        "base": {"ref": spec["base_branch"], "repo": {"full_name": "example/review"}}, "state": "open",
        "merged": False, "merge_commit_sha": "d" * 40,
    }
    files = [{"filename": item["path"], "status": "added", "sha": improvement_delivery._blob(item["content"])}
             for item in spec["files"]]
    unexpected = True
    default_head = base

    async def get_hosted(target: improvement_delivery.Target, suffix: str) -> object:
        assert target.repository == "example/review"
        assert target.branch == spec["base_branch"]
        if suffix == "pulls/17":
            return remote
        if suffix == f"git/ref/heads/{spec['base_branch']}":
            return {"object": {"sha": default_head}}
        assert suffix == "pulls/17/files?per_page=100&page=1"
        return files + ([{"filename": "src/hidden.py", "status": "added", "sha": "f" * 40}] if unexpected else [])

    monkeypatch.setattr(improvement_delivery, "_get_review", get_hosted)
    confirmation = {"revision": spec["revision"], "pr_number": 17}
    foreign = await runs.client.put(path, params=scope, headers=h, json=confirmation)
    assert foreign.status_code == 409, foreign.text
    unexpected = False
    async def grade(run: dict, *, regress: bool = False) -> None:
        labels = [{
            "trial_id": row["id"], "outcome": "failed" if regress and row["arm"] == "pulled" else "passed",
            "judge_tool": "codex", "judge_model": "fixed",
        } for row in run["trials"]]
        scored = await runs.client.post(
            f"{root}/benchmark/runs/{run['id']}/results", params=scope, headers=h, json={"items": labels},
        )
        assert scored.status_code == 200, scored.text
        assert scored.json()["gate"] == ("regressed" if regress else "held"), scored.text

    if ci:
        event = {"proposal_id": proposal, "repository": "example/review", "base_branch": spec["base_branch"],
                 "head_commit": head, "pr_number": 17}
        ci_path = f"{root}/self-improvement/ci"

        async def invoke(action: str, payload: dict) -> dict:
            reply = await runs.client.post(f"{ci_path}/{action}", params=scope, headers=h, json=payload)
            assert reply.status_code == 200, reply.text
            return reply.json()

        outdated = await runs.client.post(f"{ci_path}/plan", params=scope, headers=h, json=event | {
            "operation_id": str(uuid4()), "head_commit": "f" * 40,
        })
        assert outdated.status_code == 409
        first = event | {"operation_id": str(uuid4())}
        if postgres:
            planned, repeated = await asyncio.gather(invoke("plan", first), invoke("plan", first))
        else:
            planned = await invoke("plan", first)
            repeated = await invoke("plan", first)
        assert planned["check_state"] == "pending" and repeated == planned
        run = (await runs.client.get(f"{root}/benchmark/runs/{planned['benchmark_id']}", params=scope)).json()
        assert len(run["trials"]) == 18 and run["proposal_resources"] == head
        assert (await invoke("result", first))["check_state"] == "error"
        await grade(run)
        default_head = "e" * 40
        assert (await invoke("result", first))["check_state"] == "error"
        default_head = base
        assert (await invoke("result", first))["check_state"] == "success"

        retry = event | {"operation_id": str(uuid4())}
        next_run = await invoke("plan", retry)
        assert (await invoke("plan", first))["check_state"] == "superseded"
        assert (await invoke("result", first))["check_state"] == "superseded"
        run_retry = (await runs.client.get(f"{root}/benchmark/runs/{next_run['benchmark_id']}", params=scope)).json()
        await grade(run_retry, regress=True)
        assert (await invoke("result", retry))["check_state"] == "failure"

        failed_attempt = event | {"workflow_run": "3111", "attempt": "1", "operation_id": str(uuid4())}
        failed_plan = await invoke("plan", failed_attempt)
        failed_path = f"{root}/benchmark/runs/{failed_plan['benchmark_id']}"
        failed_run = (await runs.client.get(failed_path, params=scope)).json()
        failed_trial = await runs.client.post(failed_path + "/results", params=scope, headers=h, json={
            "check_operation_id": failed_attempt["operation_id"],
            "items": [{"trial_id": failed_run["trials"][0]["id"], "outcome": "error"}],
        })
        assert failed_trial.status_code == 200
        restart = failed_attempt | {"attempt": "2", "operation_id": str(uuid4())}
        assert (await invoke("plan", restart))["benchmark_id"] != failed_plan["benchmark_id"]
        stale_failed = await runs.client.post(failed_path + "/results", params=scope, headers=h, json={
            "check_operation_id": failed_attempt["operation_id"],
            "items": [{"trial_id": failed_run["trials"][1]["id"], "outcome": "error"}],
        })
        assert stale_failed.status_code == 409

        first_attempt = event | {"workflow_run": "3112", "attempt": "2", "operation_id": str(uuid4())}
        if postgres:
            tracked, same_attempt = await asyncio.gather(invoke("plan", first_attempt), invoke("plan", first_attempt))
            assert tracked == same_attempt
        else:
            tracked = await invoke("plan", first_attempt)
        previous_family = restart | {"attempt": "3", "operation_id": str(uuid4())}
        assert (await invoke("plan", previous_family))["check_state"] == "superseded"
        run_path = f"{root}/benchmark/runs/{tracked['benchmark_id']}"
        run = (await runs.client.get(run_path, params=scope)).json()
        profile = {
            "image": "sha256:" + "a" * 64, "runner_sha256": "b" * 64, "network": "none",
            "judge_tool": "codex", "judge_model": "fixed",
        }

        async def bind(owner: dict):
            return await runs.client.put(run_path + "/execution", params=scope, headers=h, json=profile | {
                "check_operation_id": owner["operation_id"],
            })

        execution = await bind(first_attempt)
        assert execution.status_code == 200, execution.text
        labels = [{
            "trial_id": slot["id"], "outcome": "passed", "judge_tool": "codex",
            "judge_model": "fixed", "execution_id": execution.json()["id"],
        } for slot in run["trials"]]

        async def settle(owner: dict, rows: list[dict]):
            return await runs.client.post(run_path + "/results", params=scope, headers=h, json={
                "items": rows, "check_operation_id": owner["operation_id"],
            })

        assert (await settle(first_attempt, labels[:1])).status_code == 200
        resumed_attempt = first_attempt | {"attempt": "4", "operation_id": str(uuid4())}
        settled_before_handoff = 1
        if postgres:
            # 换手与上一尝试的写入同时发生时，按基准行锁的顺序保留合法结果。
            resumed, racing = await asyncio.gather(invoke("plan", resumed_attempt), settle(first_attempt, labels[1:2]))
            assert racing.status_code in (200, 409)
            settled_before_handoff += int(racing.status_code == 200)
        else:
            resumed = await invoke("plan", resumed_attempt)
        assert resumed["benchmark_id"] == tracked["benchmark_id"]
        assert resumed["benchmark_status"] == "pending"
        assert resumed["check_operation_id"] == resumed_attempt["operation_id"]
        behind = first_attempt | {"attempt": "3", "operation_id": str(uuid4())}
        assert (await invoke("plan", behind))["check_state"] == "superseded"
        assert (await invoke("plan", first_attempt))["check_state"] == "superseded"
        assert (await invoke("result", first_attempt))["check_state"] == "superseded"
        assert (await bind(first_attempt)).status_code == 409
        obsolete = await settle(first_attempt, labels[2:3])
        assert obsolete.status_code == 409 and obsolete.json()["detail"]["code"] == "KNOWLEDGE_IMPROVEMENT_CI_ATTEMPT"
        assert (await bind(resumed_attempt)).json() == execution.json()
        resumed_report = (await runs.client.get(run_path, params=scope)).json()
        assert sum(trial["outcome"] == "passed" for trial in resumed_report["trials"]) == settled_before_handoff
        assert (await settle(resumed_attempt, labels)).status_code == 200
        assert (await invoke("result", resumed_attempt))["check_state"] == "success"

        # 已完成的证据可以只重复发布状态。较旧的尝试不能再写判定。
        final_attempt = resumed_attempt | {"attempt": "5", "operation_id": str(uuid4())}
        final = await invoke("plan", final_attempt)
        assert (final["benchmark_id"], final["benchmark_status"]) == (tracked["benchmark_id"], "complete")
        assert (await settle(resumed_attempt, labels)).status_code == 409
        assert (await invoke("result", final_attempt))["check_state"] == "success"
        remote["state"], remote["merged"] = "closed", True
        stale = await runs.client.put(path, params=scope, headers=h, json=confirmation | {
            "benchmark_id": planned["benchmark_id"],
        })
        assert stale.status_code == 409, stale.text
        merged = await invoke("merge", event)
        assert merged == await invoke("merge", event)
        assert merged["merge_commit"] == remote["merge_commit_sha"]
        assert merged["benchmark_id"] == final["benchmark_id"]
        assert (await runs.client.get(resume, params=original)).json()["status"] == "merged"
        return project

    recorded = await runs.client.put(path, params=scope, headers=h, json=confirmation)
    assert recorded.status_code == 200 and recorded.json()["pr_number"] == 17, recorded.text
    recovered = (await runs.client.get(resume, params=original)).json()
    assert (recovered["status"], recovered["head_commit"], recovered["pr_number"]) == ("delivered", head, 17)

    async def pass_matrix(tasks: list[str]) -> str:
        plan = await runs.client.post(f"{root}/benchmark/runs", params=scope, headers=h, json={
            "operation_id": str(uuid4()), "proposal_id": proposal, "resource_repository_id": spec["repository_id"],
            "base_resources": spec["base_commit"], "proposal_resources": head, "tasks": tasks,
        })
        assert plan.status_code == 201, plan.text
        run = plan.json()
        await grade(run)
        return str(run["id"])

    partial = await pass_matrix(["initial"])
    remote["state"], remote["merged"] = "closed", True
    insufficient = await runs.client.put(path, params=scope, headers=h, json=confirmation | {"benchmark_id": partial})
    assert insufficient.status_code == 409, insufficient.text
    whole = await pass_matrix([])
    merged = await runs.client.put(path, params=scope, headers=h, json=confirmation | {"benchmark_id": whole})
    assert merged.status_code == 200 and merged.json()["merge_commit"] == remote["merge_commit_sha"], merged.text
    repeated = await runs.client.put(path, params=scope, headers=h, json=confirmation | {"benchmark_id": whole})
    assert repeated.json() == merged.json()
    dashboard = await runs.client.get(f"{root}/dashboard/self-improvement", params=scope)
    assert dashboard.status_code == 200, dashboard.text
    summary = dashboard.json()
    assert summary["summary"]["merged"] == 1
    delivered = summary["items"][0]
    assert delivered["id"] == proposal and delivered["pr_number"] == 17
    assert delivered["proof"]["run_id"] == whole and delivered["proof"]["verified"] is True
    assert delivered["effect"]["status"] == "insufficient_evidence"
    default = (await runs.client.get(f"{root}/benchmark/suites", params=scope)).json()
    assert {item["title"] for item in default["tasks"]} == {"Regression"}
    assert len(default["tasks"]) == 3
    return project
