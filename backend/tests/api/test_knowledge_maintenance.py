"""知识维护的阈值、范围、置信度版本和审计。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora.modules.knowledge.models import (
    KnowledgeConfidenceSnapshot,
    KnowledgeEntry,
    KnowledgeEvent,
    KnowledgeRecall,
)

pytestmark = pytest.mark.anyio


async def test_maintenance_threshold_preview_and_published_confidence(runs: RunsHarness) -> None:
    selected = {"project_id": str((await project(runs, "知识维护项目"))["id"])}
    other = {"project_id": str((await project(runs, "隔离项目"))["id"])}
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    confidence = f"{path}/maintenance/confidence"
    stamp = datetime.now(UTC)

    async def create(name: str, scope: dict[str, str]) -> str:
        response = await runs.client.post(path, params=scope, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": name, "body": f"{name}的当前发布正文。",
        })
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    candidate = await create("maintenancealpha", selected)
    stale = await create("maintenanceold", selected)
    untouched = await create("maintenancenew", selected)
    shared = await create("maintenanceshared", {})
    separate = await create("maintenanceother", other)
    for entry, scope in ((candidate, selected), (stale, selected), (shared, {}), (separate, other)):
        # 指定经验的历史反馈用独立会话，避免同词条影响样本。
        feedback = await runs.client.post(f"{path}/{entry}/feedback", params=scope, headers=runs.headers(), json={
            "session": f"fixture-{entry}", "positive": False,
        })
        assert feedback.status_code == 204, feedback.text

    async def observe(entry: str, count: int, days: int) -> None:
        async with runs.sessions() as db, db.begin():
            await db.execute(update(KnowledgeRecall).where(
                KnowledgeRecall.entry_id == UUID(entry),
            ).values(recall_count=count, last_recalled_at=stamp - timedelta(days=days)))

    for entry, count in ((candidate, 1), (stale, 6), (shared, 1), (separate, 1)):
        await observe(entry, count, 181)
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeEntry).where(KnowledgeEntry.id == UUID(stale))
                         .values(created_at=stamp - timedelta(days=181)))
        postgres = db.get_bind().dialect.name == "postgresql"

    prune = f"{path}/maintenance"
    for value in ("nan", "inf", "-inf"):
        assert (await runs.client.get(prune, params=selected | {"threshold": value})).status_code == 422
    preview = await runs.client.get(prune, params=selected | {"threshold": "0.04", "candidates_only": "true"})
    assert [(row["id"], row["prune_reason"]) for row in preview.json()["items"]] == [(stale, "stale_low_confidence")]
    expanded = (await runs.client.get(
        prune, params=selected | {"threshold": "0.05", "candidates_only": "true"},
    )).json()["items"]
    assert {row["id"]: row["status"] for row in expanded} == {candidate: "active", stale: "active"}

    inspected = (await runs.client.get(confidence, params=selected)).json()
    assert inspected["preview"] is True
    assert {row["id"]: row["confidence"] for row in inspected["items"]} == {candidate: 0.04, stale: 0.24}
    async with runs.sessions() as db:
        assert await db.scalar(select(KnowledgeConfidenceSnapshot.entry_id)) is None

    async with member(runs, "对比分组成员") as (peer, _):
        denied = await peer.post(confidence, params=selected, headers=headers(peer))
        assert denied.status_code == 403
    assert (await runs.client.post(confidence, params=selected)).status_code == 403

    async def writeback() -> dict[str, object]:
        written = await runs.client.post(confidence, params=selected, headers=runs.headers())
        assert written.status_code == 200, written.text
        return written.json()  # type: ignore[no-any-return]

    if postgres:
        first, replay = await asyncio.gather(writeback(), writeback())
    else:
        first, replay = await writeback(), await writeback()
    assert sorted((bool(first["items"]), bool(replay["items"]))) == [False, True]
    prior = (await runs.client.get(f"{path}/{candidate}", params=selected)).json()
    assert prior["confidence_snapshot"]["confidence"] == 0.04 and prior["revision"] == 1

    await observe(candidate, 1, 150)  # 0.04 + 0.05，达到刚好 0.05 的差值。
    assert (await writeback())["items"] == []
    await observe(candidate, 3, 181)
    change = (await runs.client.get(confidence, params=selected)).json()["items"]
    assert [(row["id"], row["previous_confidence"], row["confidence"]) for row in change] == [
        (candidate, 0.04, 0.12),
    ]
    assert (await writeback())["items"] == change
    after = (await runs.client.get(f"{path}/{candidate}", params=selected)).json()
    assert (after["body"], after["revision"]) == (prior["body"], 1)
    assert after["confidence_snapshot"]["confidence"] == 0.12

    edited = await runs.client.post(f"{path}/{candidate}/revisions", params=selected, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "maintenancealpha", "body": "待审修订。", "base_revision": 1,
    })
    assert edited.status_code == 201, edited.text
    if edited.json()["revision_status"] == "pending":
        approved = await runs.client.post(f"{path}/{candidate}/review", params=selected, headers=runs.headers(), json={
            "decision": "accept", "revision": 2,
        })
        assert approved.status_code == 200, approved.text
    current = (await runs.client.get(f"{path}/{candidate}", params=selected)).json()
    assert current["revision"] == 2 and "confidence_snapshot" not in current
    assert (await writeback())["items"] == [{
        "id": candidate, "revision": 2, "title": "maintenancealpha",
        "confidence": 0.12, "previous_confidence": None,
    }]

    archived = await runs.client.post(
        f"{prune}/archive", params=selected | {"threshold": "0.13", "candidates_only": "true"},
        headers=runs.headers(),
    )
    assert archived.status_code == 200, archived.text
    assert {row["id"] for row in archived.json()["items"] if row["status"] == "archived"} == {candidate, stale}
    assert (await runs.client.get(f"{path}/{untouched}", params=selected)).status_code == 200
    restored = await runs.client.post(f"{path}/{candidate}/restore", params=selected, headers=runs.headers())
    assert restored.json()["revision"] == 2 and restored.json()["status"] == "active"
    async with runs.sessions() as db:
        rows = (await db.scalars(select(KnowledgeConfidenceSnapshot))).all()
        assert {(str(row.entry_id), row.revision, row.score) for row in rows} == {
            (candidate, 1, 12), (candidate, 2, 12), (stale, 1, 24),
        }
        events = (await db.scalars(select(KnowledgeEvent).where(KnowledgeEvent.action == "confidence_updated"))).all()
        assert len(events) == 4 and all(event.actor_id == UUID(runs.user_id) for event in events)
