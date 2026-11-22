"""后台路由评分沿用项目授权、确认与每日额度，并冻结整组重试。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.models import (
    KnowledgeAdoptionAttempt,
    KnowledgeEvent,
    KnowledgeNamedScoreCall,
    KnowledgeRoutingAttempt,
    KnowledgeScoringAttempt,
    KnowledgeSessionLabel,
    KnowledgeSharedSessionRevision,
    SessionSkillEvidence,
)

pytestmark = pytest.mark.anyio


async def test_automatic_routing_uses_shared_quota_and_retries_pinned_questions(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    base = root + "/session-scoring"
    scope = {"project_id": str((await project(runs, "路由评分项目"))["id"])}
    other = {"project_id": str((await project(runs, "独立路由项目"))["id"])}
    auth = runs.headers()

    async def configure(action: str, *, limit: int = 2, enabled: bool = True):
        return await runs.client.put(root + "/settings/session-scoring/attended", headers=auth, json={
            "action": action, "daily_limit": limit, "skill_routing": enabled,
        })

    skills: SessionSkillEvidence = {"available": [
        {"name": name, "namespace": ns, "version_id": str(uuid4()), "installation_id": str(uuid4()),
         "description": name + " test description"}
        for name, ns in (("artifact-check", ""), ("release-review", "delivery"))
    ], "used": ["artifact-check"]}
    saved = await runs.client.post(root + "/shared-sessions", params=scope, headers=auth, json={
        "session": str(uuid4()), "runtime": "codex", "title": "自动路由证据",
        "captured_at": datetime.now(UTC).isoformat(),
        "turns": [{"role": "assistant", "text": "I checked the artifact."}],
    })
    assert saved.status_code == 201, saved.text
    subject_id, revision = saved.json()["id"], saved.json()["revision"]
    # 安装核验和采集已由独立 API 测试覆盖，这里固定一份包含两个 Skill 的证据。
    async with runs.sessions() as db, db.begin():
        row = await db.get(KnowledgeSharedSessionRevision, (UUID(subject_id), revision))
        assert row is not None
        row.skill_evidence = skills

    subject = {"kind": "archive", "archive_id": subject_id, "revision": revision}
    request = {"run_mode": "attended", "subject": subject}
    assert (await configure("ask")).status_code == 200
    old = await runs.client.post(base + "/offer", params=scope, headers=auth, json=request)
    assert old.json() == {"status": "ask", "items": []}
    suggested = await runs.client.post(base + "/offer", params=scope, headers=auth,
                                       json=request | {"accept_routing": True})
    names = ["teamai:routing:artifact-check", "teamai:routing:delivery:release-review"]
    assert [item["name"] for item in suggested.json()["items"]] == names, suggested.text
    manifest = await runs.client.put(root + "/settings/resource-namespaces", headers=auth, json={
        "base_revision": 0, "projects": {scope["project_id"]: {"scorers": ["delivery"]}},
    })
    assert manifest.status_code == 200, manifest.text
    directory = root + "/settings/session-scoring/namespaces/delivery"
    disabled = await runs.client.put(directory, headers=auth, json={"base_revision": 0, "skill_routing": False})
    assert disabled.status_code == 200, disabled.text
    stale = await runs.client.post(base + "/offer", params=scope, headers=auth,
                                   json=request | {"scorers": names, "recommended_routing": True})
    assert stale.json() == {"status": "off", "items": []}
    explicit = await runs.client.post(base + "/offer", params=scope, headers=auth, json=request | {"scorers": names})
    assert [item["name"] for item in explicit.json()["items"]] == names, explicit.text
    restored = await runs.client.put(directory, headers=auth, json={
        "base_revision": disabled.json()["revision"], "skill_routing": True,
    })
    assert restored.status_code == 200, restored.text

    assert (await configure("auto")).status_code == 200

    async def claim(operation, *, accepts=True, params=scope, evidence=None):
        payload = {"run_mode": "attended", "operation_id": str(operation), "accept_routing": accepts}
        if evidence is not None:
            payload["local_evidence"] = evidence
        response = await runs.client.post(base + "/claim", params=params, headers=auth, json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    async def settle(operation, data, params=scope):
        return await runs.client.post(base + f"/routing/{operation}/settle", params=params, headers=auth, json=data)

    assert await claim(uuid4(), accepts=False) == {"status": "empty"}
    assert await claim(uuid4(), params=other) == {"status": "empty"}
    original = uuid4()
    first = await claim(original)
    assert first["status"] == "open" and first["attempt"] == 1, first
    assert [item["name"] for item in first["routing_batch"]] == names
    assert await claim(original) == first
    async with runs.sessions() as db:
        is_postgres = db.get_bind().dialect.name == "postgresql"
    if is_postgres:
        assert await asyncio.gather(claim(original), claim(original)) == [first, first]
    assert await claim(uuid4()) == {"status": "empty"}
    half = await settle(original, {"labels": {names[0]: "fit"}})
    assert half.status_code == 422 and half.json()["detail"]["code"] == "KNOWLEDGE_SCORE_BATCH_REQUEST"
    assert (await settle(original, {"error": "judge_failed"}, other)).status_code == 404
    failed = await settle(original, {"error": "judge_failed"})
    assert failed.json() == {"status": "failed", "reason": "judge_failed"}
    assert await claim(uuid4()) == {"status": "empty"}

    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeRoutingAttempt).where(KnowledgeRoutingAttempt.id == original).values(
            settled_at=datetime.now(UTC) - timedelta(minutes=2),
        ))
    replaced = await runs.client.put(root + "/session-scorers/teamai-routing", params=scope, headers=auth, json={
        "prompt": "Updated {{ skill.name }}.", "passing_score": 1, "labels": [
            {"value": "pass", "score": 1, "description": "Pass"}, {"value": "fail", "score": 0, "description": "Fail"},
        ],
    })
    assert replaced.status_code == 200, replaced.text
    retry = uuid4()
    second = await claim(retry)
    assert second["attempt"] == 2 and second["routing_batch"] == first["routing_batch"], second
    correct = {"labels": {names[0]: "fit", names[1]: "missed"}}
    invalid = await settle(retry, {"labels": correct["labels"] | {names[1]: "fail"}})
    assert invalid.status_code == 422 and invalid.json()["detail"]["code"] == "KNOWLEDGE_ROUTING_LABEL"
    completed = await settle(retry, correct)
    assert completed.status_code == 200 and completed.json()["status"] == "completed", completed.text
    assert [row["passed"] for row in completed.json()["items"]] == [True, False]
    assert (await settle(retry, correct)).json() == completed.json()
    receipt = await runs.client.get(base + f"/named-calls/{retry}", params=scope)
    assert receipt.json()["items"] == completed.json()["items"]
    assert await claim(uuid4()) == {"status": "daily_limit"}

    # 新模板形成新问题，设置变更在发放后仍会复核。
    assert (await configure("auto", limit=3)).status_code == 200
    third = uuid4()
    assert (await claim(third))["status"] == "open"
    stopped = await runs.client.put(directory, headers=auth, json={
        "base_revision": restored.json()["revision"], "skill_routing": False,
    })
    assert stopped.status_code == 200
    rejected = await settle(third, {"labels": {name: "pass" for name in names}})
    assert rejected.json() == {"status": "failed", "reason": "policy_revoked"}

@pytest.mark.parametrize("ordinary", [True, False])
async def test_mixed_routing_uses_one_call_and_retries_pinned_members(runs: RunsHarness, ordinary: bool) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    base = "/session-scoring"
    project_id = str((await project(runs, "合并 Skill 路由评分"))["id"])
    scope = {"project_id": project_id}
    headers = runs.headers()

    async def post(path: str, **payload: object):
        return await runs.client.post(root + path, params=scope, headers=headers, json=payload)

    async def settings(path: str, payload: dict[str, object], *, workspace: bool = False):
        response = await runs.client.put(root + path, params={} if workspace else scope, headers=headers, json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    async def claim(operation: str, *, mixed: bool = True):
        return await post(
            base + "/claim", run_mode="attended", operation_id=operation, accept_routing=True, accept_batch=True,
            accept_mixed_batch=True, accept_adoption=True, accept_adoption_template=True, accept_adoption_batch=True,
            accept_routing_mix=mixed,
        )

    policy = "/settings/session-scoring/attended"
    await settings(policy, {"action": "auto", "batch": True, "daily_limit": 1, "skill_routing": True}, workspace=True)
    await settings("/settings/session-context/recall-adoption", {"enabled": True}, workspace=True)
    definition: dict[str, object] = {
        "prompt": "Verify the artifact.", "passing_score": 1, "tool": "claude", "model": "shared-model",
        "sessions": True, "sampling_rate": 100,
        "labels": [{"value": "pass", "score": 1, "description": "Verified."},
                   {"value": "fail", "score": 0, "description": "Missing."}],
    }
    prefix = "/session-scorers/"
    if ordinary:
        await settings(prefix + "a-artifact", definition)
        await settings(prefix + "b-artifact", definition)
        await settings(prefix + "z-other-model", definition | {"model": "separate-model"})
    await settings(prefix + "teamai-adoption", definition | {"prompt": "Judge {{ doc.file }}."})
    await settings(prefix + "teamai-routing", definition | {"prompt": "Judge {{ skill.name }}."})

    session = str(uuid4())
    entry = await post("", operation_id=str(uuid4()), title="spectral mirror", body="Use a spectral mirror.")
    assert entry.status_code == 201, entry.text
    recalled = await post("/recall", query="spectral mirror", session=session, recall_mode=True)
    assert recalled.json()["items"][0]["id"] == entry.json()["id"]
    saved = await post("/shared-sessions", session=session, runtime="codex", title="合并评分证据",
                       captured_at=datetime.now(UTC).isoformat(),
                       turns=[{"role": "assistant", "text": "Verified the spectral mirror."}])
    assert saved.status_code == 201, saved.text
    skills: SessionSkillEvidence = {
        "available": [{
            "name": "artifact-check", "namespace": "delivery", "version_id": str(uuid4()),
            "installation_id": str(uuid4()), "description": "Verify an artifact.",
        }],
        "used": ["artifact-check"],
    }
    async with runs.sessions() as db, db.begin():
        version = await db.get(KnowledgeSharedSessionRevision, (UUID(saved.json()["id"]), 1))
        assert version is not None
        version.skill_evidence = skills

    operation = str(uuid4())
    opened = await claim(operation)
    assert opened.status_code == 200, opened.text
    work = opened.json()
    assert work["archive"] == saved.json()
    members = work["mixed_batch"]
    assert [next(key for key in ("scorer", "adoption", "routing") if key in row) for row in members] == (
        ["scorer", "scorer", "adoption", "routing"] if ordinary else ["adoption", "routing"]
    )
    if ordinary:
        assert [row["scorer"]["name"] for row in members[:2]] == ["a-artifact", "b-artifact"]
    assert (await claim(operation)).json() == work
    assert (await claim(operation, mixed=False)).status_code == 409
    assert (await claim(str(uuid4()))).json() == {"status": "daily_limit"}

    endpoint = base + f"/routing/{operation}/settle"
    routing_member = members[-1]["name"]
    labels = {row["attempt_id"]: "pass" for row in members}
    assert (await post(endpoint, labels={routing_member: "pass"})).status_code == 422
    assert (await post(endpoint, labels=labels | {members[-2]["attempt_id"]: "invalid"})).status_code == 422
    if ordinary:
        assert (await post(base + f"/batches/{operation}/settle", error="judge_failed")).status_code == 409
    assert (await post(f"/adoption/batches/{operation}/settle", error="judge_failed")).status_code == 409
    async with runs.sessions() as db:
        assert await db.get(KnowledgeNamedScoreCall, (UUID(runs.user_id), UUID(operation))) is None
        assert await db.scalar(select(KnowledgeSessionLabel.id).where(
            KnowledgeSessionLabel.archive_id == UUID(saved.json()["id"]),
        )) is None
        assert await db.scalar(select(KnowledgeEvent.id).where(
            KnowledgeEvent.entry_id == UUID(entry.json()["id"]), KnowledgeEvent.action == "judge_adopted",
        )) is None

    await settings(policy, {"action": "auto", "skill_routing": False}, workspace=True)
    stopped = await post(endpoint, labels=labels)
    assert stopped.json() == {"status": "failed", "reason": "policy_revoked"}
    async with runs.sessions() as db, db.begin():
        earlier = datetime.now(UTC) - timedelta(minutes=2)
        for model in (KnowledgeScoringAttempt, KnowledgeAdoptionAttempt):
            await db.execute(update(model).where(model.judge_call_id == UUID(operation)).values(settled_at=earlier))
        await db.execute(update(KnowledgeRoutingAttempt).where(KnowledgeRoutingAttempt.id == UUID(operation)).values(
            settled_at=earlier,
        ))
    await settings(policy, {"action": "auto", "batch": False, "daily_limit": 2, "skill_routing": True}, workspace=True)
    await settings(prefix + "teamai-routing", definition | {"prompt": "Updated {{ skill.name }}.", "base_revision": 1})
    await settings(prefix + "teamai-adoption", definition | {"prompt": "Updated {{ doc.file }}.", "base_revision": 1})
    if ordinary:
        await settings(prefix + "a-artifact", definition | {"model": "changed-model", "base_revision": 1})

    retry_id = str(uuid4())
    retry = await claim(retry_id)
    assert retry.status_code == 200, retry.text
    pinned = retry.json()["mixed_batch"]
    snapshots = [{key: value for key, value in row.items() if key not in ("attempt", "attempt_id")} for row in members]
    assert [{key: value for key, value in row.items() if key not in ("attempt", "attempt_id")} for row in pinned] == (
        snapshots
    )
    assert all(row["attempt"] == 2 for row in pinned)
    answers = {row["attempt_id"]: "pass" for row in pinned}
    endpoint = base + f"/routing/{retry_id}/settle"
    finished = await post(endpoint, labels=answers)
    assert finished.status_code == 200 and finished.json()["status"] == "completed", finished.text
    receipts = finished.json()
    assert receipts["items"][0]["judge_call_id"] == retry_id
    assert receipts["items"][0]["scorer"] == routing_member
    assert receipts["members"][-1]["adoption"]["judge_call_id"] == retry_id
    if ordinary:
        assert {row["label"]["judge_call_id"] for row in receipts["members"][:2]} == {retry_id}
    assert (await post(endpoint, labels=answers)).json() == receipts
    assert (await claim(str(uuid4()))).json() == {"status": "daily_limit"}
