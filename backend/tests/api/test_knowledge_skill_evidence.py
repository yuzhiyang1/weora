"""会话 Skill 快照仅引用该设备、该项目实际持有的安装版本。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tests.api.test_agent_mail import device
from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_skills import build_skill_package
from tests.api.test_task_collaboration import member

pytestmark = pytest.mark.anyio


async def test_session_skill_evidence_is_scoped_and_frozen(runs: RunsHarness) -> None:
    workspace = f"/api/v1/workspaces/{runs.workspace_id}"
    root = workspace + "/knowledge"
    dist = workspace + "/skill-distribution"
    scope = {"project_id": str((await project(runs, "Skill 证据项目"))["id"])}
    other = {"project_id": str((await project(runs, "Skill 无关项目"))["id"])}
    csrf = runs.headers()

    async def paired(name: str) -> dict[str, str]:
        identifier, authorization = await device(runs.client, workspace + "/agent-collaboration", name)
        grant = await runs.client.put(f"{dist}/devices/{identifier}/grant", headers=csrf, json={"usage_enabled": True})
        assert grant.status_code == 200, grant.text
        return authorization

    auth = await paired("证据设备")
    other_device = await paired("另一个配对设备")
    slug = "release-check"
    created = await runs.client.post(workspace + "/skills", params=scope, headers=csrf,
                                     data={"namespace": "ops"}, files={"package": (
        f"{slug}.skill", build_skill_package(name=slug, description="Check a release."), "application/zip",
    )})
    assert created.status_code == 201, created.text
    published = await runs.client.post(f"{workspace}/skills/{created.json()['id']}/publish",
                                      params=scope, headers=csrf, json={"version": "1.0.0", "changelog": "First"})
    assert published.status_code == 201, published.text
    version = published.json()["id"]
    start = datetime.now(UTC) - timedelta(minutes=3)
    observed, cutoff = start + timedelta(minutes=1), start + timedelta(minutes=2)

    async def install(authorization, occurred_at):
        payload = {"operation_id": str(uuid4()), "slot_id": str(uuid4()), "installation_id": str(uuid4()),
                   "version_id": version, "occurred_at": occurred_at.isoformat(), "runtime": "codex"}
        result = await runs.client.post(dist + "/installation-receipts", params=scope,
                                       headers=authorization, json=payload)
        assert result.json() == {"status": "accepted"}, result.text
        return payload

    installation, foreign = await install(auth, start), await install(other_device, start)
    original = {"generation": installation["installation_id"], "observed_at": observed.isoformat(),
                "description": "Check a release."}
    references = [original, original | {"generation": foreign["installation_id"]}]
    capture = {"installations": references, "usage_profile": "a" * 64, "usage_session": "b" * 64}
    payload = {
        "session": str(uuid4()), "runtime": "codex", "title": "发布检查", "captured_at": cutoff.isoformat(),
        "turns": [{"role": "assistant", "text": "Finished."}], "skill_capture": capture,
    }

    async def publish_session(params=scope, body=payload, credential=auth):
        return await runs.client.post(root + "/shared-sessions", params=params, json=body, headers=credential)

    assert (await publish_session(credential=csrf)).status_code == 403
    late = capture | {"installations": [original | {"observed_at": (cutoff + timedelta(seconds=1)).isoformat()}]}
    assert (await publish_session(body=payload | {"skill_capture": late})).status_code == 422
    saved = await publish_session()
    assert saved.status_code == 201, saved.text
    archive = saved.json()
    helper = f"{root}/shared-sessions/{archive['id']}/skill-evidence"
    evidence_query = scope | {"revision": str(archive["revision"])}
    expected = {"name": slug, "namespace": "ops", "version_id": version,
                "description": "Check a release.", "installation_id": installation["installation_id"]}
    first = await runs.client.get(helper, params=evidence_query)
    assert first.json() == {"skills": {"available": [expected], "used": []}}, first.text
    assert (await runs.client.get(helper, params=other | {"revision": "1"})).status_code == 404
    async with member(runs, "普通项目同事") as (teammate, _):
        assert (await teammate.get(helper, params=evidence_query)).status_code == 404

    event = {
        "installation_id": installation["installation_id"], "runtime": "codex", "profile_id": "a" * 64,
        "session_key": "b" * 64, "turn_key": "c" * 64, "kind": "loaded", "occurred_at": observed.isoformat(),
    }
    loaded = await runs.client.post(dist + "/usage-events/batch", params=scope, headers=auth, json={"events": [event]})
    assert loaded.json()["items"][0]["status"] == "accepted", loaded.text
    retired = await runs.client.post(dist + "/installation-receipts", params=scope, headers=auth, json={
        "operation_id": str(uuid4()), "slot_id": installation["slot_id"], "runtime": "codex",
        "previous_id": installation["installation_id"], "occurred_at": (cutoff + timedelta(seconds=5)).isoformat(),
    })
    assert retired.json() == {"status": "accepted"}, retired.text
    replayed = await publish_session()
    assert replayed.json() == archive
    assert (await runs.client.get(helper, params=evidence_query)).json() == first.json()

    # 描述脱敏后，新快照仍可按历史安装代次纳入已确认的加载记录。
    revised = capture | {"installations": [original | {"description": "token=synthetic-test-secret"}]}
    updated = await publish_session(body=payload | {"skill_capture": revised})
    assert updated.json()["revision"] == 2, updated.text
    newer = await runs.client.get(helper, params=scope | {"revision": "2"})
    assert newer.json() == {"skills": {"available": [expected | {"description": None}], "used": [slug]}}

    crossed = await publish_session(params=other)
    assert crossed.status_code == 201, crossed.text
    no_cross = await runs.client.get(root + f"/shared-sessions/{crossed.json()['id']}/skill-evidence",
                                    params=other | {"revision": "1"})
    assert no_cross.json() == {"skills": {"available": [], "used": []}}, no_cross.text
    local = await runs.client.post(root + "/session-scoring/local-subjects", params=scope, headers=auth, json={
        "runtime": "codex", "subject_key": "d" * 64, "digest": "e" * 64, "skill_capture": capture,
        "captured_at": cutoff.isoformat(), "recall_session_key": "f" * 64, "run_mode": "attended",
    })
    assert local.status_code == 403  # 保留现有后台评分授权。
    policy = await runs.client.put(root + "/settings/session-scoring/attended", headers=csrf, json={"action": "auto"})
    assert policy.status_code == 200, policy.text
    registered = await runs.client.post(root + "/session-scoring/local-subjects", params=scope, headers=auth, json={
        "runtime": "codex", "subject_key": "d" * 64, "digest": "e" * 64, "skill_capture": capture,
        "captured_at": cutoff.isoformat(), "recall_session_key": "f" * 64, "run_mode": "attended",
    })
    assert registered.status_code == 201, registered.text
    local_ref = {"id": registered.json()["id"], "revision": registered.json()["revision"], "digest": "e" * 64}
    local_view = await runs.client.get(root + f"/shared-sessions/{registered.json()['id']}/skill-evidence",
                                       params=scope | {"revision": "1"})
    assert local_view.json() == {"skills": {"available": [expected], "used": [slug]}}

    named = f"{root}/session-scoring"
    question = "teamai:routing:ops:release-check"
    subject = {"kind": "archive", "archive_id": archive["id"], "revision": 1}

    async def plan(selected=subject, names=None, **options):
        return await runs.client.post(named + "/named-plan", params=scope, headers=auth,
                                      json={"subject": selected, "scorers": names or [question], **options})

    first_plan = await plan()
    assert first_plan.status_code == 200, first_plan.text
    row = first_plan.json()["items"][0]
    evidence = row["routing"]
    assert evidence["skill"] == expected and evidence["used"] == []
    assert [choice["value"] for choice in evidence["rubric"]["labels"]] == ["fit", "idle", "extra", "missed"]
    second_plan = await plan(subject | {"revision": 2})
    newer_question = second_plan.json()["items"][0]
    assert newer_question["source_hash"] == row["source_hash"]
    assert newer_question["routing"]["evidence_hash"] != evidence["evidence_hash"]
    absent = await plan(names=["teamai:routing:release-check"])
    assert absent.status_code == 404 and absent.json()["detail"]["code"] == "KNOWLEDGE_ROUTING_SKILL_NOT_AVAILABLE"

    label = {"name": question, "label": "idle", "driver": "human",
             "source_hash": row["source_hash"], "evidence_hash": evidence["evidence_hash"]}
    operation = str(uuid4())

    async def submit(selected, operation_id, choice, **options):
        return await runs.client.post(named + "/named-calls", params=scope, headers=auth, json={
            "subject": selected, "operation_id": operation_id, "items": [choice], **options,
        })

    rejected = await submit(subject, operation, label | {"evidence_hash": "c" * 64})
    assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "KNOWLEDGE_ROUTING_EVIDENCE_CHANGED"
    settled = await submit(subject, operation, label)
    assert settled.status_code == 200, settled.text
    receipt = settled.json()
    assert receipt["items"][0]["skill"]["installation_id"] == installation["installation_id"]
    assert receipt["items"][0]["passed"] and "applied" not in receipt["items"][0]
    assert (await submit(subject, operation, label)).json() == receipt
    saved_receipt = await runs.client.get(named + f"/named-calls/{operation}", params=scope, headers=auth)
    assert saved_receipt.json() == receipt

    template = await runs.client.put(root + "/session-scorers/teamai-routing", params=scope, headers=csrf, json={
        "prompt": "Evaluate {{ skill.name }} with the evidence.", "sessions": True, "passing_score": 1,
        "isolation": "verified", "labels": [
            {"value": "fit", "score": 1, "description": "Fit for {{ skill.name }}."},
            {"value": "review", "score": 0, "description": "Needs review."},
        ],
    })
    assert template.status_code == 200 and template.json()["routing_template"], template.text
    old_client = await plan()
    assert old_client.status_code == 409 and old_client.json()["detail"]["code"] == "KNOWLEDGE_SCORING_ISOLATION"
    team = (await plan(accepts_isolation=True)).json()["items"][0]
    assert team["routing"]["rubric"]["prompt"] == "Evaluate release-check with the evidence."
    ordinary = await runs.client.post(root + "/session-scorers/plan", params=scope, headers=auth,
                                      json={"scorers": ["teamai-routing"]})
    assert ordinary.status_code == 422
    reset = await runs.client.delete(root + "/session-scorers/teamai-routing", params=scope, headers=csrf)
    assert reset.status_code == 200 and reset.json()["archived"]
    assert (await plan()).json()["items"][0]["source_hash"] == row["source_hash"]
    pinned = await submit(subject, str(uuid4()), {
        "name": question, "label": "fit", "driver": "codex", "source_hash": team["source_hash"],
        "evidence_hash": team["routing"]["evidence_hash"], "scorer_id": template.json()["id"], "scorer_revision": 1,
    }, isolation="verified")
    assert pinned.status_code == 200 and pinned.json()["items"][0]["scorer_revision"] == 1, pinned.text

    # 本机会话的点名计划先核验证据，标签提交只向服务端登记快照和摘要。
    local_subject = {
        "kind": "local", "runtime": "codex", "subject_key": "1" * 64, "digest": "2" * 64,
        "recall_session_key": "3" * 64, "captured_at": cutoff.isoformat(), "skill_capture": capture,
    }
    local_plan = await plan(local_subject)
    assert local_plan.status_code == 200, local_plan.text
    local_question = local_plan.json()["items"][0]
    local_label = label | {"label": "fit", "evidence_hash": local_question["routing"]["evidence_hash"]}
    registered = await submit(local_subject, str(uuid4()), local_label)
    assert registered.status_code == 200, registered.text
    registered_id = registered.json()["subject_id"]
    from_pin = await plan({key: value for key, value in local_subject.items() if key != "skill_capture"})
    assert from_pin.json()["items"] == local_plan.json()["items"]
    view = await runs.client.get(root + f"/shared-sessions/{registered_id}", params=scope)
    assert view.status_code == 404

    ask = await runs.client.put(root + "/settings/session-scoring/attended", headers=csrf, json={"action": "ask"})
    assert ask.status_code == 200
    offer = await runs.client.post(named + "/offer", params=scope, headers=auth,
                                   json={"run_mode": "attended", "subject": subject, "scorers": [question]})
    assert offer.status_code == 200 and offer.json()["status"] == "ask" and offer.json()["items"] == [row], offer.text
    confirmation = await submit(subject, str(uuid4()), label | {"driver": "codex"}, offer_mode="attended")
    assert confirmation.status_code == 200 and confirmation.json()["items"][0]["label"] == "idle"

    unclear_capture = {"installations": [original]}
    unclear = await publish_session(body=payload | {"skill_capture": unclear_capture})
    no_usage = subject | {"revision": unclear.json()["revision"]}
    uncertain = await plan(no_usage)
    assert uncertain.status_code == 422 and uncertain.json()["detail"]["code"] == "KNOWLEDGE_ROUTING_USAGE_UNKNOWN"
    manual = await plan(no_usage, manual=True)
    assert manual.status_code == 200 and manual.json()["items"][0]["routing"]["used"] is None

    routing_auto = await runs.client.put(root + "/settings/session-scoring/attended", headers=csrf, json={
        "action": "auto", "skill_routing": True,
    })
    assert routing_auto.status_code == 200, routing_auto.text

    async def claim_local(references):
        operation = str(uuid4())
        claim = await runs.client.post(named + "/claim", params=scope, headers=auth, json={
            "operation_id": operation, "run_mode": "attended", "accept_routing": True, "local_evidence": references,
        })
        assert claim.status_code == 200, claim.text
        return operation, claim.json()

    assert (await claim_local([]))[1] == {"status": "empty"}
    assert (await claim_local([local_ref | {"digest": "a" * 64}]))[1] == {"status": "empty"}
    operation, local_work = await claim_local([local_ref])
    assert local_work["status"] == "open" and local_work["archive"]["evidence"] == "local"
    assert local_work["archive"]["id"] == local_ref["id"]
    assert local_work["routing_batch"][0]["routing"]["used"] == [slug]
    private = await runs.client.get(root + f"/shared-sessions/{local_ref['id']}", params=scope, headers=auth)
    assert private.status_code == 404
    completion = await runs.client.post(named + f"/routing/{operation}/settle", params=scope, headers=auth,
                                        json={"labels": {question: "fit"}})
    assert completion.status_code == 200 and completion.json()["status"] == "completed", completion.text
