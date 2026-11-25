"""约束评分计划下发和结算，保留人工标签。"""

from uuid import uuid4

import httpx
import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.models import KnowledgeScoringAttempt

pytestmark = pytest.mark.anyio


async def test_isolation_tracks_directory_scorer_and_claim_capability(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "隔离评审项目"))["id"])}
    settings = root + "/settings/session-scoring"
    auth = runs.headers()

    async def put(path: str, data: dict[str, object], *, project_scope: bool = False) -> dict[str, object]:
        result = await runs.client.put(root + path, headers=auth, json=data, params=scope if project_scope else None)
        assert result.status_code == 200, result.text
        result_json: dict[str, object] = result.json()
        return result_json

    async def post(path: str, **data: object) -> httpx.Response:
        return await runs.client.post(root + path, headers=auth, params=scope, json=data)

    await put("/settings/session-scoring/attended", {"action": "auto", "daily_limit": 5, "isolation": "verified"})
    await put("/settings/resource-namespaces", {
        "base_revision": 0, "roles": {"risk": {"scorers": ["risk"]}}, "members": {runs.user_id: "risk"},
        "projects": {scope["project_id"]: {"scorers": ["delivery"]}},
    })
    delivery = await put(
        "/settings/session-scoring/namespaces/delivery", {"base_revision": 0, "isolation": "best-effort"},
    )
    assert (await runs.client.get(settings, params=scope)).json()["attended"]["isolation"] == "best-effort"
    risk = await put("/settings/session-scoring/namespaces/risk", {"base_revision": 0, "isolation": "verified"})
    assert (await runs.client.get(settings, params=scope)).json()["attended"]["isolation"] == "verified"
    await put("/settings/session-scoring/namespaces/risk", {"base_revision": risk["revision"]})
    rubric = await put("/session-scorers/quality", {
        "prompt": "Check project evidence.", "isolation": "verified", "passing_score": 1, "sessions": True,
        "sampling_rate": 100, "labels": [
            {"value": "pass", "score": 1, "description": "通过"}, {"value": "fail", "score": 0, "description": "退回"},
        ],
    }, project_scope=True)
    archive = await post("/shared-sessions", session=str(uuid4()), runtime="codex", title="评分证据",
                         turns=[{"role": "assistant", "text": "Finished."}])
    assert archive.status_code == 201, archive.text
    saved = archive.json()
    plan_url = "/session-scorers/plan"
    assert (await post(plan_url, scorers=["quality"])).status_code == 409
    manual_plan = await post(plan_url, scorers=["quality"], manual=True)
    assert manual_plan.status_code == 200
    plan = await post(plan_url, scorers=["quality"], accepts_isolation=True)
    assert plan.json()["isolation"] == "best-effort" and plan.json()["items"][0]["isolation"] == "verified"
    offer = await post("/session-scoring/offer", run_mode="attended", accepts_isolation=True,
                       subject={"kind": "archive", "archive_id": saved["id"], "revision": saved["revision"]})
    assert offer.status_code == 200 and offer.json()["items"][0]["isolation"] == "verified", offer.text

    label_path = f"{root}/shared-sessions/{saved['id']}/labels"
    label_payload = {
        "operation_id": str(uuid4()), "scorer_id": rubric["id"], "scorer_revision": rubric["revision"],
        "driver": "codex", "label": "pass",
    }
    rejected = await runs.client.post(label_path, params=scope | {"revision": "1"}, headers=auth, json=label_payload)
    assert rejected.status_code == 409, rejected.text
    manual = await runs.client.post(label_path, params=scope | {"revision": "1"}, headers=auth,
                                   json=label_payload | {"driver": "human"})
    assert manual.status_code == 201 and "isolation" not in manual.json()
    second = await post("/shared-sessions", session=str(uuid4()), runtime="codex", title="后台评分证据",
                        turns=[{"role": "assistant", "text": "Finished the second task."}])
    assert second.status_code == 201

    operation = uuid4()
    route = "/session-scoring/claim"
    request = {"operation_id": str(operation), "run_mode": "attended"}
    for options in ({}, {"accepts_isolation": True, "verified_tools": ["claude"]}):
        denied = await post(route, **request, **options)
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"]["code"] == "KNOWLEDGE_SCORING_ISOLATION"
        async with runs.sessions() as db:
            assert await db.get(KnowledgeScoringAttempt, operation) is None
    claimed = await post(route, **request, accepts_isolation=True, verified_tools=["codex"])
    assert claimed.status_code == 200 and claimed.json()["status"] == "open", claimed.text
    settle = f"/session-scoring/attempts/{operation}/settle"
    wrong = await post(settle, label="pass", isolation="best-effort")
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "KNOWLEDGE_SCORING_ISOLATION", wrong.text
    correct = await post(settle, label="pass", isolation="verified")
    assert correct.status_code == 200 and correct.json()["label"]["isolation"] == "verified", correct.text
    assert (await post(settle, label="pass")).json() == correct.json()

    async with runs.sessions() as db:
        job = await db.get(KnowledgeScoringAttempt, operation)
        assert job is not None and job.state == "completed"

    # 标准没有单独要求时，领取的团队隔离仍应在策略放宽后随租约保留。
    updated = await put("/session-scorers/quality", {
        "base_revision": rubric["revision"], "prompt": "Check updated evidence.", "passing_score": 1,
        "sampling_rate": 100, "sessions": True, "labels": rubric["labels"],
    }, project_scope=True)
    assert "isolation" not in updated
    minimum = await put("/settings/session-scoring/namespaces/delivery", {
        "base_revision": delivery["revision"], "isolation": "verified",
    })
    operation = uuid4()
    request["operation_id"] = str(operation)
    pinned = await post(route, **request, accepts_isolation=True, verified_tools=["codex"])
    assert pinned.status_code == 200 and pinned.json()["isolation"] == "verified", pinned.text
    await put("/settings/session-scoring/namespaces/delivery", {
        "base_revision": minimum["revision"], "isolation": "best-effort",
    })
    replay = await post(route, **request, accepts_isolation=True, verified_tools=["codex"])
    assert replay.json() == pinned.json()
    settle = f"/session-scoring/attempts/{operation}/settle"
    assert (await post(settle, label="pass")).status_code == 409
    assert (await post(settle, label="pass", isolation="verified")).json()["label"]["isolation"] == "verified"
