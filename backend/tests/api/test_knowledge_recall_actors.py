"""验证同一根会话内召回子 Agent、自身阅读和实际采纳的边界。"""

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_skills import build_skill_package
from weora.modules.knowledge.models import KnowledgeRecallRun, KnowledgeSkillRecall
from weora.modules.knowledge.recall_journal import session_digest

pytestmark = pytest.mark.anyio


async def test_marked_recall_excludes_own_opens_but_preserves_other_actors(runs: RunsHarness) -> None:
    scope = {"project_id": str((await project(runs, "召回执行者"))["id"])}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    actor, other = str(uuid4()), str(uuid4())
    path = "learnings/forecast-proof.md"

    async def post(suffix: str, **payload: object) -> httpx.Response:
        return await runs.client.post(root + suffix, params=scope, headers=runs.headers(), json=payload)

    linked = await post("", operation_id=str(uuid4()), title="依赖材料", body="Weora 的独立关系目标。")
    assert linked.status_code == 201, linked.text
    target = linked.json()["id"]
    item = await post(
        "", operation_id=str(uuid4()), title="forecastjournal", body="可采纳的计划。",
        source={"type": "manual", "resource_path": path}, relations=[{"kind": "REFERENCES", "target": target}],
    )
    assert item.status_code == 201, item.text
    item_id = item.json()["id"]

    async def stats(session: str, adopted: int, related: int) -> None:
        result = await post("/recall/stats", session=session)
        assert result.status_code == 200, result.text
        [row] = result.json()["items"]
        assert row["adopted"] == adopted and row["via_relation"] == related, row

    session = "recall-owner"
    recalled = await post("/recall", session=session, query="forecastjournal", recall_mode=True, recall_actor=actor)
    assert recalled.status_code == 200 and recalled.json()["items"][0]["id"] == item_id
    run = UUID(recalled.json()["run_id"])
    async with runs.sessions() as db:
        assert await db.scalar(select(KnowledgeRecallRun.recall_actor_digest).where(KnowledgeRecallRun.id == run)) == (
            session_digest(actor)
        )
    assert (await post("/by-path/open", session=session, path=path, actor=actor)).status_code == 200
    assert (await post(f"/{target}/open", session=session, actor=actor)).status_code == 200
    await stats(session, 0, 0)
    assert (await post(f"/{item_id}/open", session=session)).status_code == 200
    assert (await post(f"/{target}/open", session=session, actor=other)).status_code == 200
    await stats(session, 1, 1)

    session = "other-recall"
    for caller in (actor, other):
        assert (await post("/recall", session=session, query="forecastjournal", recall_actor=caller)).status_code == 200
    assert (await post(f"/{item_id}/open", session=session, actor=actor)).status_code == 200
    assert (await post(f"/{target}/open", session=session, actor=actor)).status_code == 200
    # 相同会话的另一次召回仍然有效，但只标记另一个执行者的关系命中。
    await stats(session, 1, 1)


async def test_registry_skill_keeps_marked_read_out_of_version_adoption(runs: RunsHarness) -> None:
    workspace = f"/api/v1/workspaces/{runs.workspace_id}"
    registry, root = workspace + "/skills", workspace + "/knowledge"
    upload = await runs.client.post(registry, headers=runs.headers(), files={
        "package": ("meeting-summary.skill", build_skill_package(), "application/zip"),
    })
    assert upload.status_code == 201, upload.text
    skill = upload.json()["id"]
    published = await runs.client.post(f"{registry}/{skill}/publish", headers=runs.headers(), json={
        "version": "1.0.0", "changelog": "首次发布",
    })
    assert published.status_code == 201, published.text
    version = published.json()["id"]
    actor, session = str(uuid4()), "registry-recall-owner"

    async def post(suffix: str, **payload: object) -> dict[str, object]:
        response = await runs.client.post(root + suffix, headers=runs.headers(), json=payload)
        assert response.status_code == 200, response.text
        data: dict[str, object] = response.json()
        return data

    receipt = await post("/recall", session=session, query="meeting-summary", recall_mode=True, recall_actor=actor)
    items = receipt["items"]
    assert isinstance(items, list) and items[0]["id"] == skill
    await post("/by-path/open", session=session, path="skills/meeting-summary", actor=actor)
    pending = (await post("/recall/stats", session=session))["items"]
    assert isinstance(pending, list) and pending[0]["adopted"] == 0
    await post(f"/{skill}/open", session=session, actor=str(uuid4()))
    adopted = (await post("/recall/stats", session=session))["items"]
    assert isinstance(adopted, list) and adopted[0]["adopted"] == 1
    async with runs.sessions() as db:
        history = await db.get(KnowledgeSkillRecall, (UUID(skill), UUID(runs.user_id), session_digest(session)))
        assert history is not None and history.positive is True and str(history.credited_version_id) == version
