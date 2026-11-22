"""验证同一提案的资源差异和任务在同一事务登记。"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tests.api.test_knowledge_improvement import exercise_plan
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import member

pytestmark = pytest.mark.anyio


async def test_improvement_rolls_back_failed_batches_and_appends_to_one_proposal(runs: RunsHarness) -> None:
    await exercise_improvement(runs, "a" * 40)


async def exercise_improvement(
    runs: RunsHarness, commit: str, *, postgres: bool = False,
    repository_url: str = "https://git.example.test/team/review.git",
) -> tuple[str, str]:
    project = await exercise_plan(runs, commit, repository_url=repository_url)
    scope = {"project_id": project}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    route = f"{root}/self-improvement/proposals"
    params = scope | {"scorer": "review-quality"}
    before = (await runs.client.get(f"{root}/self-improvement/candidates", params=params)).json()
    pinned = before["subjects"][0]["recorded"]
    repo = pinned["git_start"]["repository_id"]
    headers = runs.headers()
    suite = await runs.client.post(f"{root}/benchmark/suites", params=scope, headers=headers, json={
        "repository_id": repo, "tools": ["codex"], "timeout_seconds": 600,
    })
    assert suite.status_code == 201, suite.text

    async def next_failure() -> int:
        now = datetime.now(UTC)
        archive = await runs.client.post(f"{root}/shared-sessions", params=scope, headers=headers, json={
            "session": str(uuid4()), "runtime": "codex", "title": "回归新证据",
            "turns": [{"role": "user", "text": "Review the boundary check."}],
            "captured_at": now.isoformat(), "git_start": {
                "repository": repository_url, "commit": commit,
                "tracked_changes": False, "observed_at": (now - timedelta(seconds=1)).isoformat(),
            },
        })
        assert archive.status_code == 201, archive.text
        result = await runs.client.post(
            f"{root}/shared-sessions/{archive.json()['id']}/labels", params=scope | {"revision": "1"},
            headers=headers, json={"scorer_id": before["scorer_id"], "scorer_revision": 1,
                                  "operation_id": str(uuid4()), "driver": "human", "label": "fail"},
        )
        assert result.status_code == 201, result.text
        return int(result.json()["id"])

    def case(label: int, name: str) -> dict[str, object]:
        return {"label_id": label, "name": name, "title": "Regression", "prompt": "Review the edge condition.",
                "starting_commit": commit, "success_criteria": "Report the missing edge case."}

    first, second = case(pinned["label_id"], "initial"), case(await next_failure(), "second")
    initial = {
        "operation_id": str(uuid4()), "scorer": "review-quality", "base_revision": 0,
        "resource_repository_id": repo, "base_commit": commit, "reason": "A repeated edge case.",
        "tasks": [first, second], "changes": [
            {"path": "rules/review.md", "before_sha256": None, "content": "Check boundary conditions."},
        ],
    }
    resume = f"{root}/self-improvement/resume"
    first_operation = params | {"operation_id": initial["operation_id"]}
    assert (await runs.client.get(resume, params=first_operation)).json() == {"status": "missing"}
    threshold = await runs.client.post(route, params=scope, headers=headers, json=initial | {"tasks": [first]})
    assert threshold.status_code == 422, threshold.text
    outside = await runs.client.post(route, params=scope, headers=headers, json=initial | {"changes": [
        {"path": "src/app.py", "before_sha256": None, "content": "print(1)"},
    ]})
    assert outside.status_code == 422, outside.text
    non_atomic = await runs.client.post(route, params=scope, headers=headers, json=initial | {
        "tasks": [first, second | {"starting_commit": "f" * 40}],
    })
    assert non_atomic.status_code == 422, non_atomic.text
    current = (await runs.client.get(f"{root}/self-improvement/candidates", params=params)).json()
    assert current["proposal_id"] is None and all(subject["task"] is None for subject in current["subjects"])

    saved = await runs.client.post(route, params=scope, headers=headers, json=initial)
    assert saved.status_code == 201, saved.text
    ref = saved.json()
    assert ref["revision"] == 1 and len(ref["task_ids"]) == 2
    assert (await runs.client.get(resume, params=first_operation)).json() == ref | {"status": "pending_delivery"}
    wrong = await runs.client.get(resume, params=first_operation | {"scorer": "other"})
    assert wrong.status_code == 409, wrong.text
    assert (await runs.client.post(route, params=scope, headers=headers, json=initial)).json() == ref
    mismatch = await runs.client.post(route, params=scope, headers=headers, json=initial | {"reason": "Other"})
    assert mismatch.status_code == 409, mismatch.text
    identifier = ref["proposal_id"]
    detail = f"{route}/{identifier}/revision"
    readonly = (await runs.client.get(detail, params=scope)).json()
    assert readonly["changes"] == initial["changes"]

    third = case(await next_failure(), "third")
    extension = initial | {
        "operation_id": str(uuid4()), "expected_proposal_id": identifier, "base_revision": 1,
        "tasks": [third], "changes": [
            {"path": "rules/review.md", "before_sha256": hashlib.sha256(b"Check boundary conditions.").hexdigest(),
             "content": "Check boundary conditions and report the smallest example."},
        ],
    }
    stale = await runs.client.post(route, params=scope, headers=headers, json=extension | {"base_revision": 0})
    assert stale.status_code == 409, stale.text
    bad = extension | {
        "changes": [{"path": "rules/review.md", "before_sha256": "d" * 64, "content": "Stale edit"}],
    }
    stale_asset = await runs.client.post(route, params=scope, headers=headers, json=bad)
    assert stale_asset.status_code == 409, stale_asset.text
    if postgres:
        # 两个执行器用同一版本续写时，第二个必须重新基于已提交的修订生成。
        competing = extension | {"operation_id": str(uuid4())}
        results = await asyncio.gather(
            runs.client.post(route, params=scope, headers=headers, json=extension),
            runs.client.post(route, params=scope, headers=headers, json=competing),
        )
        assert sorted(result.status_code for result in results) == [201, 409]
        winner = next(index for index, result in enumerate(results) if result.status_code == 201)
        raised = results[winner]
        operation = str((extension, competing)[winner]["operation_id"])
    else:
        raised = await runs.client.post(route, params=scope, headers=headers, json=extension)
        operation = str(extension["operation_id"])
    assert raised.status_code == 201 and raised.json()["revision"] == 2, raised.text
    assert raised.json()["proposal_id"] == identifier
    assert (await runs.client.get(resume, params=first_operation)).json() == ref | {"status": "superseded"}
    revision = (await runs.client.get(detail, params=scope)).json()
    assert revision["changes"][0]["before_sha256"] is None
    assert "smallest example" in revision["changes"][0]["content"]
    coverage = (await runs.client.get(f"{root}/self-improvement/candidates", params=params)).json()
    assert sum(item["task"] is not None for item in coverage["subjects"]) == 3
    async with member(runs, "提案只读测试") as (teammate, _):
        assert (await teammate.get(detail, params=scope)).status_code == 403
        assert (await teammate.get(resume, params=first_operation)).status_code == 403
    result = await runs.client.put(f"{root}/benchmark/proposals/{identifier}", params=scope, headers=headers,
                                   json={"status": "merged"})
    assert result.status_code == 409 and result.json()["detail"]["code"] == "KNOWLEDGE_IMPROVEMENT_DELIVERY_REQUIRED"
    return project, operation
