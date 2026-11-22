"""在知识读取与采纳加锁之间切换发布版本，验证响应和账本一致。"""

import asyncio
from collections.abc import Awaitable, Callable
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.models import KnowledgeRecall, KnowledgeRecallRunHit
from weora.modules.knowledge.recall_journal import session_digest
from weora.modules.knowledge.store import KnowledgeStore

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("operation", ("id", "path", "recall"))
async def test_publication_between_read_and_record(runs: RunsHarness, operation: str) -> None:
    selected = await project(runs, "发布竞争")
    scope = {"project_id": str(selected["id"])}
    base = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    session = "publication-race"
    path = "learnings/version-race.md"

    async def post(suffix: str, **payload: object) -> httpx.Response:
        return await runs.client.post(base + suffix, headers=runs.headers(), params=scope, json=payload)

    original = {"title": "midnightledger 原整理", "body": "待更新的旧正文。"}
    created = await post("", operation_id=str(uuid4()), source={"type": "manual", "resource_path": path}, **original)
    assert created.status_code == 201, created.text
    target = UUID(created.json()["id"])
    stable = await post("", operation_id=str(uuid4()), title="midnightledger 独立结论", body="同步更新时依旧有效。")
    assert stable.status_code == 201, stable.text
    stable_id = stable.json()["id"]
    patch_body = "已审核的新正文。"
    pending = await post(f"/{target}/revisions", operation_id=str(uuid4()), base_revision=1,
                         title=original["title"], body=patch_body)
    assert pending.status_code == 201, pending.text

    async def recall() -> httpx.Response:
        return await post("/recall", query="midnightledger", session=session)

    async def read() -> httpx.Response:
        if operation == "recall":
            return await recall()
        if operation == "path":
            return await post("/by-path/open", session=session, path=path)
        return await post(f"/{target}/open", session=session)

    if operation != "recall":
        first = await recall()
        assert first.status_code == 200, first.text
        hit = next(row for row in first.json()["items"] if row["id"] == str(target))
        assert hit["revision"] == 1

    waiting, released = asyncio.Event(), asyncio.Event()
    record: Callable[..., Awaitable[None]] = KnowledgeStore._record

    async def pause(store: KnowledgeStore, ident: UUID, root: str, **options: object) -> None:
        if ident == target and root == session:
            # 稳定控制竞争顺序，直到新版本经公开审核入口提交后才加锁。
            waiting.set()
            await asyncio.wait_for(released.wait(), 8)
        await record(store, ident, root, **options)

    with patch.object(KnowledgeStore, "_record", pause):
        task = asyncio.create_task(read())
        try:
            await asyncio.wait_for(waiting.wait(), 8)
            published = await post(f"/{target}/review", revision=2, decision="accept")
            assert published.status_code == 200, published.text
        finally:
            released.set()
            response = await asyncio.wait_for(task, 8)

    if operation == "recall":
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert [row["id"] for row in receipt["items"]] == [stable_id]
        async with runs.sessions() as db:
            hits = await db.scalars(select(KnowledgeRecallRunHit.entry_id).where(
                KnowledgeRecallRunHit.run_id == UUID(receipt["run_id"]),
            ))
            assert [str(hit) for hit in hits] == [stable_id]
            assert await db.get(KnowledgeRecall, (target, UUID(runs.user_id), session_digest(session))) is None
        return

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "KNOWLEDGE_PUBLICATION_CHANGED"
    retried = await read()
    assert retried.status_code == 200 and retried.json()["body"] == patch_body
    async with runs.sessions() as db:
        history = await db.get(KnowledgeRecall, (target, UUID(runs.user_id), session_digest(session)))
        assert history is not None and history.recalled_revision == 1 and history.positive is None

    assert (await recall()).status_code == 200
    for _ in range(2):
        opened = await read()
        assert opened.status_code == 200 and opened.json()["revision"] == 2
    stats = await post("/recall/stats", session=session)
    assert stats.status_code == 200 and stats.json()["items"][0]["adopted"] == 1
