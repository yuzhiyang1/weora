"""验证设备补发的会话反馈顺序与旧接口兼容性。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.models import KnowledgeEvent, KnowledgeRecall
from weora.modules.knowledge.recall_journal import session_digest

pytestmark = pytest.mark.anyio


async def test_delayed_feedback_cannot_replace_newer_manual_choice(runs: RunsHarness) -> None:
    selected = {"project_id": str((await project(runs, "反馈项目"))["id"])}
    other = {"project_id": str((await project(runs, "其他项目"))["id"])}
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    source = await runs.client.post(path, params=selected, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "反馈用例", "body": "使用固定项目的版本。",
    })
    assert source.status_code == 201, source.text
    entry = UUID(source.json()["id"])
    endpoint = f"{path}/{entry}/feedback"
    same_session = "feedback-fixture-root"
    earlier = datetime.now(UTC) - timedelta(minutes=8)
    later = earlier + timedelta(minutes=1)
    old = {"session": same_session, "positive": True, "operation_id": str(uuid4()), "occurred_at": earlier.isoformat()}
    latest = old | {"positive": False, "operation_id": str(uuid4()), "occurred_at": later.isoformat()}

    async def send(payload: dict[str, str | bool]) -> int:
        return (await runs.client.post(endpoint, params=selected, headers=runs.headers(), json=payload)).status_code

    assert await send(old | {"occurred_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}) == 422
    assert await send({"session": same_session, "positive": True, "operation_id": str(uuid4())}) == 422
    unauthorized = await runs.client.post(endpoint, params=other, headers=runs.headers(), json=old)
    assert unauthorized.status_code == 404

    offset = later.astimezone(timezone(timedelta(hours=8))).isoformat()
    assert await send(latest | {"occurred_at": offset}) == 204
    assert await send(old) == 204
    assert await send(latest) == 204
    assert await send(latest | {"positive": True}) == 409
    middle = old | {"operation_id": str(uuid4()), "occurred_at": (earlier + timedelta(seconds=30)).isoformat()}
    assert await send(middle) == 204

    async with runs.sessions() as db:
        vote = await db.scalar(select(KnowledgeRecall).where(KnowledgeRecall.entry_id == entry))
        assert vote is not None and vote.positive is False and vote.credit_source == "manual"
        assert vote.manual_vote_id == UUID(str(latest["operation_id"]))
        assert vote.session_key == session_digest(same_session)
        records = (await db.scalars(select(KnowledgeEvent).where(
            KnowledgeEvent.entry_id == entry, KnowledgeEvent.action == "manual_feedback_recorded",
        ))).all()
        assert len(records) == 1 and json.loads(records[0].reason)["operation_id"] == latest["operation_id"]
        assert same_session not in records[0].reason

    # 旧客户端沿用服务端时钟，其历史记录也能抵住晚到的反馈。
    assert await send({"session": same_session, "positive": True}) == 204
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeRecall).where(KnowledgeRecall.entry_id == entry).values(
            manual_vote_id=None, manual_vote_at=None,
        ))
        postgres = db.get_bind().dialect.name == "postgresql"
    assert await send(latest) == 204

    # 相同方向的新选择也更新顺序基线。并发的较早反向反馈不能拖回最终选择。
    stamp = datetime.now(UTC)
    newer = old | {"operation_id": str(uuid4()), "occurred_at": (stamp + timedelta(seconds=2)).isoformat()}
    older = latest | {"operation_id": str(uuid4()), "occurred_at": (stamp + timedelta(seconds=1)).isoformat()}
    if postgres:
        assert await asyncio.gather(send(older), send(newer)) == [204, 204]
    else:
        assert await send(newer) == await send(older) == 204
    async with runs.sessions() as db:
        vote = await db.scalar(select(KnowledgeRecall).where(KnowledgeRecall.entry_id == entry))
        assert vote is not None and vote.positive is True and vote.manual_vote_id == UUID(str(newer["operation_id"]))
