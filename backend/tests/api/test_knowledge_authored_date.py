"""原文日期驱动维护与晋升，审核和入库仍使用平台时间。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora.modules.knowledge.models import KnowledgeRecall

pytestmark = pytest.mark.anyio


async def test_approved_authored_dates_control_promotion_prune_and_health(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "原文日期验收"))["id"])}
    now = datetime.now(UTC)

    async def publish(title: str, at: datetime) -> str:
        result = await runs.client.post(root, params=scope, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": title, "body": "固定知识正文。", "authored_at": at.isoformat(),
        })
        assert result.status_code == 201, result.text
        assert datetime.fromisoformat(result.json()["authored_at"]) == at
        assert datetime.fromisoformat(result.json()["created_at"]).replace(tzinfo=UTC) > now
        return str(result.json()["id"])

    mature = await publish("turquoisetopic", now - timedelta(days=14, minutes=5))
    too_early = await publish("ultramarinetopic", now - timedelta(days=14) + timedelta(hours=2))
    stale = await publish("ambertopic", now - timedelta(days=181))
    fresh = await publish("bronzetopic", now - timedelta(days=45))
    async with member(runs, "第二位知识采纳者") as (teammate, _):
        for entry in (mature, too_early):
            for number in range(5):
                actor = teammate if number < 2 else runs.client
                response = await actor.post(f"{root}/{entry}/feedback", params=scope, headers=headers(actor), json={
                    "session": f"mature-{number}", "positive": True,
                })
                assert response.status_code == 204, response.text
    for entry, topic in ((stale, "ambertopic"), (fresh, "bronzetopic")):
        result = await runs.client.post(f"{root}/recall", params=scope, headers=runs.headers(), json={
            "query": topic, "session": f"aging-{entry}",
        })
        assert result.status_code == 200 and [item["id"] for item in result.json()["items"]] == [entry]
    # 固定采纳近期度，确保只由原文日期决定是否命中 180 天规则。
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeRecall).where(
            KnowledgeRecall.entry_id.in_([UUID(stale), UUID(fresh)]),
        ).values(last_recalled_at=now - timedelta(days=40)))

    promotion = f"{root}/maintenance/promote"
    found = await runs.client.get(promotion, params=scope)
    assert [row["id"] for row in found.json()["items"]] == [mature]
    assert (await runs.client.get(f"{promotion}/{mature}", params=scope)).status_code == 200
    assert (await runs.client.get(f"{promotion}/{too_early}", params=scope)).status_code == 409
    maintenance = f"{root}/maintenance"
    preview = await runs.client.get(maintenance, params=scope)
    rows = {item["id"]: item for item in preview.json()["items"]}
    assert rows[stale]["prune_reason"] == "stale_low_confidence" and not rows[fresh]["cold"]
    report = (await runs.client.get(f"{root}/health", params=scope)).json()["maintenance"]
    assert [item["id"] for item in report["prune"]["items"]] == [stale]
    assert [item["id"] for item in report["promote"]["items"]] == [mature]

    # 待审稿的原文日期不会改变已发布版本的晋升判断。
    correction = await runs.client.post(f"{root}/{too_early}/revisions", params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "base_revision": 1, "title": "ultramarinetopic", "body": "核对原文日期。",
        "authored_at": (now - timedelta(days=20)).isoformat(),
    })
    assert correction.status_code == 201 and correction.json()["revision_status"] == "pending", correction.text
    assert (await runs.client.get(f"{promotion}/{too_early}", params=scope)).status_code == 409
    decision = await runs.client.post(f"{root}/{too_early}/review", params=scope, headers=runs.headers(), json={
        "revision": 2, "decision": "accept",
    })
    assert decision.status_code == 200, decision.text
    eligible = await runs.client.get(f"{promotion}/{too_early}", params=scope)
    assert eligible.status_code == 200 and eligible.json()["revision"] == 2
    drafted = await runs.client.post(f"{promotion}/{too_early}", params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "source_revision": 2, "kind": "doc", "reason": "已确认经验可复用",
        "body": "晋升的文档正文。",
    })
    assert drafted.status_code == 201 and drafted.json()["revision_status"] == "pending", drafted.text
    archived = await runs.client.post(
        f"{maintenance}/archive", params=scope | {"candidates_only": "1"}, headers=runs.headers(),
    )
    assert archived.status_code == 200, archived.text
    assert [(item["id"], item["status"]) for item in archived.json()["items"]] == [(stale, "archived")]
    assert (await runs.client.get(f"{root}/{fresh}", params=scope)).status_code == 200


async def test_original_date_preserves_idempotency_across_offsets_and_distinguishes_reshare_days(
    runs: RunsHarness,
) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    payload = {
        "operation_id": str(uuid4()), "title": "oldSourceCalendar", "body": "正文保持一致。",
        "authored_at": "2026-02-18T19:30:00-05:00",
    }
    first = await runs.client.post(root, headers=runs.headers(), json=payload)
    assert first.status_code == 201 and first.json()["authored_at"] == "2026-02-19T00:30:00+00:00", first.text
    replay = await runs.client.post(
        root, headers=runs.headers(), json=payload | {"authored_at": "2026-02-19T00:30:00Z"},
    )
    assert replay.status_code == 201 and replay.json()["id"] == first.json()["id"], replay.text
    conflict = await runs.client.post(root, headers=runs.headers(), json=payload | {"authored_at": "2026-02-20"})
    assert conflict.status_code == 409, conflict.text
    bad = await runs.client.post(root, headers=runs.headers(), json=payload | {"authored_at": "2026-02-30"})
    assert bad.status_code == 422, bad.text
    second = await runs.client.post(root, headers=runs.headers(), json=payload | {
        "operation_id": str(uuid4()), "authored_at": "2026-02-20",
    })
    assert second.status_code == 201, second.text
    older_import = await runs.client.post(root, headers=runs.headers(), json=payload | {
        "operation_id": str(uuid4()), "authored_at": "2026-02-17",
    })
    assert older_import.status_code == 201, older_import.text
    found = await runs.client.get(root, params={"query": "oldSourceCalendar", "recall_mode": "1"})
    assert [item["id"] for item in found.json()["items"]] == [
        second.json()["id"], first.json()["id"], older_import.json()["id"],
    ]
    manual = await runs.client.get(root, params={"query": "oldSourceCalendar"})
    assert [item["id"] for item in manual.json()["items"]] == [
        older_import.json()["id"], second.json()["id"], first.json()["id"],
    ]
    edited = await runs.client.post(f"{root}/{first.json()['id']}/revisions", headers=runs.headers(), json={
        "operation_id": str(uuid4()), "base_revision": 1, "title": payload["title"], "body": "修订后正文。",
    })
    assert edited.status_code == 201 and edited.json()["authored_at"] == first.json()["authored_at"], edited.text
