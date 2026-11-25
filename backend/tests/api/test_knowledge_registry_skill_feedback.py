"""覆盖独立 Skill 的反馈权威性、版本、队列排序和作用域。"""

from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_skills import build_skill_package
from weora.modules.knowledge.models import KnowledgeSkillFeedbackEvent, KnowledgeSkillRecall
from weora.modules.skills.application.service import SkillActor, SkillRegistryService
from weora.modules.skills.infrastructure.unit_of_work import SqlAlchemySkillsUnitOfWork

pytestmark = pytest.mark.anyio


async def test_recall_collapses_reshared_registry_skills_by_indexed_content(runs: RunsHarness) -> None:
    workspace = UUID(runs.workspace_id)
    project_id = UUID(str((await project(runs, "重复 Skill"))["id"]))
    registry = SkillRegistryService(
        uow_factory=lambda: SqlAlchemySkillsUnitOfWork(runs.sessions, project_id=project_id), object_storage=None,
    )
    actor = SkillActor(UUID(runs.user_id), workspace, "owner")
    title = "QueueLease 手册"

    async def release(slug: str, body: str) -> str:
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr(f"{slug}/SKILL.md", (
                f"---\nname: {slug}\ntitle: {title}\ndescription: Provide project guidance\n---\n\n{body}\n"
            ))
        item = await registry.create_skill(
            actor=actor, workspace_id=workspace, package_name=f"{slug}.skill",
            archive_bytes=buffer.getvalue(), request_id=slug,
        )
        await registry.publish(
            actor=actor, workspace_id=workspace, skill_id=item.id, version="1.0.0",
            changelog="召回测试", request_id=slug,
        )
        return str(item.id)

    first = await release("queue-manual-a", "quartz nebula")
    copy = await release("queue-manual-b", "nebula quartz quartz")
    different = await release("queue-manual-c", "quartz circuit")
    response = await runs.client.get(f"/api/v1/workspaces/{workspace}/knowledge", params={
        "project_id": str(project_id), "query": "QueueLease", "recall_mode": True, "limit": 3,
    })
    assert response.status_code == 200, response.text
    found = {row["id"] for row in response.json()["items"]}
    assert different in found and len(found & {first, copy}) == 1 and len(found) == 2


async def test_skill_feedback_projects_waiting_votes_and_preserves_the_recalled_release(runs: RunsHarness) -> None:
    workspace = UUID(runs.workspace_id)
    project_id = UUID(str((await project(runs, "对账项目"))["id"]))
    scope = {"project_id": str(project_id)}
    other = {"project_id": str((await project(runs, "独立项目"))["id"])}
    root = f"/api/v1/workspaces/{workspace}/knowledge"
    actor = SkillActor(UUID(runs.user_id), workspace, "owner")
    registry = SkillRegistryService(
        uow_factory=lambda: SqlAlchemySkillsUnitOfWork(runs.sessions, project_id=project_id), object_storage=None,
    )
    slug = "ledger-confirmation"
    package = build_skill_package(name=slug, description="Review settlement tags")
    skill = await registry.create_skill(
        actor=actor, workspace_id=workspace, package_name=f"{slug}.skill",
        archive_bytes=package, request_id="fixture",
    )

    async def release(version: str) -> UUID:
        item = await registry.publish(
            actor=actor, workspace_id=workspace, skill_id=skill.id, version=version,
            changelog="反馈验收", request_id=version,
        )
        return item.id

    async def view(suffix: str, data: dict[str, object]) -> dict[str, object]:
        result = await runs.client.post(f"{root}/{suffix}", params=scope, headers=runs.headers(), json=data)
        assert result.status_code == 200, result.text
        payload: dict[str, object] = result.json()
        return payload

    async def send(payload: dict[str, object], params: dict[str, str] = scope, status: int = 204) -> None:
        response = await runs.client.post(
            f"{root}/{skill.id}/feedback", params=params, headers=runs.headers(), json=payload,
        )
        assert response.status_code == status, response.text

    async def score(pending: list[dict[str, object]] | None = None) -> float:
        result = await view("recall/preview", {
            "query": slug, "recall_mode": True, "pending_feedback": pending or [],
        })
        rows = result["items"]
        assert isinstance(rows, list) and rows[0]["id"] == str(skill.id)
        return float(rows[0]["score"])

    first = await release("1.0.0")
    query = {"query": slug, "session": "open-first", "recall_mode": True}
    result = await view("recall", query)
    assert isinstance(result["items"], list) and result["items"][0]["source"]["version_id"] == str(first)
    await view("by-path/open", {"path": f"skills/{slug}", "session": "open-first"})
    adopted = (await view("recall/stats", {"session": "open-first"}))["items"]
    assert isinstance(adopted, list) and adopted[0]["adopted"] == 1

    rejected: dict[str, object] = {
        "session": "open-first", "positive": False,
        "operation_id": str(uuid4()), "occurred_at": datetime.now(UTC).isoformat(),
    }
    await send(rejected, params=other, status=404)
    await send(rejected)
    overridden = (await view("recall/stats", {"session": "open-first"}))["items"]
    assert isinstance(overridden, list) and overridden[0]["adopted"] == 0

    baseline = await score()
    queued: dict[str, object] = {
        "session": "open-first", "positive": True,
        "operation_id": str(uuid4()), "occurred_at": datetime.now(UTC).isoformat(),
    }
    waiting = queued | {"entry_id": str(skill.id), "project_id": str(project_id)}
    projected = await score([waiting])
    assert projected > baseline
    assert await score() == baseline
    async with runs.sessions() as db:
        marks = list(await db.scalars(select(KnowledgeSkillFeedbackEvent.operation_id).where(
            KnowledgeSkillFeedbackEvent.entry_id == skill.id,
        )))
        assert marks == [UUID(str(rejected["operation_id"]))]

    await registry.replace_draft(
        actor=actor, workspace_id=workspace, skill_id=skill.id, package_name=f"{slug}.skill",
        archive_bytes=package, request_id="updated",
    )
    second = await release("1.1.0")
    await send(queued)
    assert await score() == projected
    third: dict[str, object] = {
        "session": "open-first", "positive": False,
        "operation_id": str(uuid4()), "occurred_at": datetime.now(UTC).isoformat(),
    }
    await view("recall", query)
    # 本次投票晚于新版本的召回，按实际看见的新版本归因。
    third["occurred_at"] = datetime.now(UTC).isoformat()
    await send(third)
    await send(queued)
    await send(queued | {"positive": False}, status=409)
    assert await score([waiting]) == await score()

    async with runs.sessions() as db:
        events = (await db.scalars(select(KnowledgeSkillFeedbackEvent).where(
            KnowledgeSkillFeedbackEvent.entry_id == skill.id,
        ).order_by(KnowledgeSkillFeedbackEvent.received_at))).all()
        assert [(event.version_id, event.positive) for event in events] == [
            (first, False), (first, True), (second, False),
        ]
        history = await db.get(KnowledgeSkillRecall, (skill.id, actor.user_id, events[0].session_key))
        assert history is not None and history.recall_count == 2 and history.credited_version_id == second
        assert history.positive is False
