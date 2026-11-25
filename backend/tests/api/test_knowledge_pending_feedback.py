"""验证未确认反馈只影响提交者的本次召回。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import member
from weora.modules.knowledge.models import KnowledgeRecall, KnowledgeRecallRun

pytestmark = pytest.mark.anyio


async def test_pending_feedback_previews_same_score_as_delivery_without_writes(runs: RunsHarness) -> None:
    active = str((await project(runs, "待反馈项目"))["id"])
    other = str((await project(runs, "其他项目"))["id"])
    scope = {"project_id": active}
    knowledge = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    search_request = {"query": "Billing retry", "recall_mode": True}

    async def add(detail: str) -> str:
        response = await runs.client.post(knowledge, params=scope, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": "Billing retry", "body": f"Billing retry evidence {detail}",
        })
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    await add("redis")
    await add("gateway")
    baseline_request = await runs.client.get(
        knowledge, params=scope | {"query": "Billing retry", "recall_mode": "true"},
    )
    baseline = baseline_request.json()["items"]
    assert len(baseline) == 2
    target = baseline[-1]["id"]
    when = datetime.now(UTC) - timedelta(hours=2)
    vote = {
        "entry_id": target, "project_id": active, "session": "pending-root-fixture",
        "positive": True, "operation_id": str(uuid4()), "occurred_at": when.isoformat(),
    }

    async def preview(*votes: dict[str, object], check: bool = False) -> dict[str, object]:
        response = await runs.client.post(f"{knowledge}/recall/preview", params=scope, headers=runs.headers(), json={
            **search_request, "pending_feedback": list(votes), "check": check,
        })
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    expected_baseline = {"items": baseline}
    assert await preview(vote | {"project_id": other}) == expected_baseline
    assert await preview(vote | {"project_id": None}) == expected_baseline
    after_preview = await preview(vote)
    assert isinstance(rows := after_preview["items"], list)
    assert rows[0]["id"] == target and rows[0]["score"] > baseline[-1]["score"]
    assert await preview(vote, vote) == after_preview
    assert set(await preview(vote, check=True)) == {"relevant"}
    async with member(runs, "未提交此反馈的成员") as (peer, _):
        page = await peer.get(knowledge, params=scope | {"query": "Billing retry", "recall_mode": "true"})
        assert page.status_code == 200, page.text
        assert page.json()["items"] == baseline
    async with runs.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(KnowledgeRecall).where(
            KnowledgeRecall.entry_id.in_([UUID(row["id"]) for row in baseline]),
        )) == 0
        assert await db.scalar(select(func.count()).select_from(KnowledgeRecallRun).where(
            KnowledgeRecallRun.workspace_id == UUID(runs.workspace_id),
        )) == 0

    accepted = await runs.client.post(
        f"{knowledge}/{target}/feedback", params=scope, headers=runs.headers(),
        json={key: value for key, value in vote.items() if key not in {"entry_id", "project_id"}},
    )
    assert accepted.status_code == 204, accepted.text
    confirmed = await preview()
    assert confirmed == after_preview
    assert await preview(vote) == confirmed
    assert await preview(vote | {
        "operation_id": str(uuid4()), "positive": False, "occurred_at": (when - timedelta(minutes=1)).isoformat(),
    }) == confirmed
    # 已确认操作的回执丢失时，携带原操作只计一次。正式召回继续记录实际命中。
    with_session = await runs.client.post(f"{knowledge}/recall", params=scope, headers=runs.headers(), json={
        **search_request, "session": "actually-recalled-root", "pending_feedback": [vote],
    })
    assert with_session.status_code == 200 and with_session.json()["items"] == rows
    assert "run_id" in with_session.json()


async def test_unmatched_feedback_activates_hotness_only_in_its_own_scope(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    active = str((await project(runs, "冷热权重项目"))["id"])
    other = str((await project(runs, "隔离反馈项目"))["id"])
    scope = {"project_id": active}
    enabled = await runs.client.put(
        f"{root}/settings/recall-scope", params=scope, headers=runs.headers(), json={"inherit_shared": True},
    )
    assert enabled.status_code == 200, enabled.text

    async def add(title: str, location: dict[str, str], domain: str) -> str:
        result = await runs.client.post(root, params=location, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": title, "body": "团队核验条目。", "domain": domain,
        })
        assert result.status_code == 201, result.text
        return str(result.json()["id"])

    local = await add("API", scope, "technical")
    shared = await add("API", {}, "technical")
    shared_unmatched = await add("共享上线记录", {}, "ops")
    local_unmatched = await add("项目上线记录", scope, "ops")
    outside = await add("其他项目上线记录", {"project_id": other}, "ops")

    def vote(entry: str, project_id: str | None) -> dict[str, object]:
        return {
            "entry_id": entry, "project_id": project_id, "positive": True, "session": "unmatched-vote",
            "operation_id": str(uuid4()), "occurred_at": datetime.now(UTC).isoformat(),
        }

    async def scores(*votes: dict[str, object]) -> dict[str, float]:
        response = await runs.client.post(f"{root}/recall/preview", params=scope, headers=runs.headers(), json={
            "query": "API", "pending_feedback": list(votes),
        })
        assert response.status_code == 200, response.text
        return {str(row["id"]): float(row["score"]) for row in response.json()["items"]}

    async def confirm(value: dict[str, object], location: dict[str, str]) -> None:
        response = await runs.client.post(
            f"{root}/{value['entry_id']}/feedback", params=location, headers=runs.headers(),
            json={key: item for key, item in value.items() if key not in {"entry_id", "project_id"}},
        )
        assert response.status_code == 204, response.text

    clean = {local: 4.0, shared: 4.0}
    inherited = {local: 4.0, shared: 3.2}
    both = {local: 3.2, shared: 3.2}
    assert await scores() == clean
    assert await scores(vote(outside, other), vote(outside, active), vote(local_unmatched, None)) == clean
    pending = vote(shared_unmatched, None)
    assert await scores(pending) == inherited
    assert await scores() == clean
    await confirm(pending, {})
    assert await scores(pending) == await scores() == inherited
    own_vote = vote(local_unmatched, active)
    assert await scores(own_vote) == both
    await confirm(own_vote, scope)
    assert await scores() == both
