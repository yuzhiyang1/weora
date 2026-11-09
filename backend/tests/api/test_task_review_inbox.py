"""验证 Task 验收与 Run 审批使用各自权限，收件箱读写保持成员隔离。"""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from tests.api.test_task_continuations import finish
from tests.delivery.test_service import scope
from weora.modules.collaboration.infrastructure.inbox import InboxItemModel, sync_plan_inbox, touch_inbox
from weora.modules.delivery.domain.models import PlanRecord
from weora.modules.delivery.infrastructure.persistence import SqlAlchemyDeliveryStore
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.infrastructure.models import RunModel


@pytest.mark.anyio
async def test_member_submits_review_owner_returns_and_accepts(runs: RunsHarness) -> None:
    owner = runs.client
    created = await owner.post(runs.path, json=runs.payload("私有原始材料"), headers=runs.headers())
    assert created.status_code == 201, created.text
    task_id, run_id = created.json()["task_id"], created.json()["id"]
    task = runs.path.replace("/runs", f"/tasks/{task_id}")
    inbox = runs.path.replace("/runs", "/inbox")
    async with member(runs, "执行人") as (worker, worker_id), member(runs, "旁观者") as (observer, observer_id):
        assert (
            await owner.put(
                task + "/sharing",
                headers=runs.headers(),
                json={"revision": 0, "title": "校验任务", "summary": "共享目标", "participant_ids": [observer_id]},
            )
        ).status_code == 204
        assert (
            await owner.post(
                task + "/assignment",
                headers=runs.headers(),
                json={"revision": 1, "kind": "member", "assignee_id": worker_id, "mode": "assign"},
            )
        ).status_code == 200
        assignment = (await worker.get(inbox)).json()["items"][0]
        assert assignment["type"] == "assignment"
        assert (await observer.get(inbox)).json() == {"items": []}
        submit = {"revision": 2, "status": "in_review", "note": "已运行验收用例"}
        assert (await worker.patch(task + "/status", headers=headers(worker), json=submit)).status_code == 409
        await finish(runs, run_id)
        assert (await observer.patch(task + "/status", headers=headers(observer), json=submit)).status_code == 404
        assert (await worker.patch(task + "/status", headers=headers(worker), json=submit)).status_code == 204
        review = (await owner.get(inbox)).json()["items"][0]
        assert review["type"] == "review" and review["pending"] and review["run_id"] is None
        assert "私有原始材料" not in (await owner.get(inbox)).text

        # 混入其他收件人的 ID 时整批失败，归档也不会改变任务状态。
        mixed = {"item_ids": [assignment["id"], review["id"]], "read": True}
        assert (await worker.patch(inbox, headers=headers(worker), json=mixed)).status_code == 404
        assert not (await worker.get(inbox)).json()["items"][0]["read"]
        assert (
            await owner.patch(
                inbox,
                headers=runs.headers(),
                json={"item_ids": [review["id"]], "archived": True},
            )
        ).status_code == 204
        assert (await owner.get(task)).json()["status"] == "in_review"
        assert (await owner.get(inbox + "?archived=true")).json()["items"][0]["pending"]
        accept = {"revision": 3, "status": "done"}
        assert (await worker.patch(task + "/status", headers=headers(worker), json=accept)).status_code == 404
        assert (
            await owner.patch(
                task + "/status",
                headers=runs.headers(),
                json={"revision": 3, "status": "in_progress", "note": "请补上错误路径"},
            )
        ).status_code == 204
        assert (await worker.get(inbox)).json()["items"][0]["type"] == "review_returned"
        assert not (await owner.get(inbox + "?archived=true")).json()["items"][0]["pending"]
        assert (
            await worker.patch(
                task + "/status",
                headers=headers(worker),
                json=submit | {"revision": 4},
            )
        ).status_code == 204
        assert (await owner.patch(task + "/status", headers=runs.headers(), json=accept)).status_code == 409
        assert (
            await owner.patch(
                task + "/status",
                headers=runs.headers(),
                json=accept | {"revision": 5},
            )
        ).status_code == 204
        view = (await worker.get(task)).json()
        assert view["status"] == "done" and view["activity"][0]["action"] == "review_accepted"
        assert (await owner.get(inbox)).json() == {"items": []}


@pytest.mark.anyio
async def test_approval_inbox_follows_private_run_and_plan_version(runs: RunsHarness) -> None:
    owner = runs.client
    created = await owner.post(runs.path, json=runs.payload(), headers=runs.headers())
    assert created.status_code == 201, created.text
    run_id = UUID(created.json()["id"])
    task_id = created.json()["task_id"]
    inbox = runs.path.replace("/runs", "/inbox")
    async with runs.sessions.begin() as session:
        run = await session.get(RunModel, run_id)
        assert run
        run.status = "WAITING_APPROVAL"
    store = SqlAlchemyDeliveryStore(runs.sessions, sync_plan_inbox)
    first = PlanRecord(
        uuid4(),
        run_id,
        UUID(runs.workspace_id),
        UUID(runs.user_id),
        1,
        scope(),
        "仅授权执行者可见的计划",
        "私有风险说明",
        "PENDING",
        datetime.now(UTC),
    )
    assert await store.save_plan(first, expected_version=0, expected_status=None)
    response = await owner.get(inbox)
    assert response.status_code == 200
    entry = response.json()["items"][0]
    assert entry["pending"] and entry["type"] == "approval" and entry["task_id"] == task_id
    assert first.summary not in response.text
    async with member(runs, "任务参与者") as (collaborator, collaborator_id):
        task = runs.path.replace("/runs", f"/tasks/{task_id}")
        assert (
            await owner.put(
                task + "/sharing",
                headers=runs.headers(),
                json={
                    "revision": 0,
                    "title": "可共享标题",
                    "summary": "协作摘要",
                    "participant_ids": [collaborator_id],
                },
            )
        ).status_code == 204
        assert (await collaborator.get(task)).status_code == 200
        assert (await collaborator.get(inbox)).json() == {"items": []}
        assert (await collaborator.get(f"{runs.path}/{run_id}/plan")).status_code == 404
    second = replace(first, id=uuid4(), version=2)
    assert await store.save_plan(second, expected_version=1, expected_status="PENDING")
    current = (await owner.get(inbox)).json()["items"][0]
    assert current["id"] != entry["id"] and current["title"] == "可共享标题"
    previous = (await owner.get(inbox + "?archived=true")).json()["items"][0]
    assert previous["id"] == entry["id"] and not previous["pending"]
    decisions = f"{runs.path}/{run_id}/plan"
    assert (
        await owner.post(
            f"{decisions}/{first.id}/decision",
            headers=runs.headers(),
            json={"decision": "reject"},
        )
    ).status_code == 409
    decided = await owner.post(
        f"{decisions}/{second.id}/decision",
        headers=runs.headers(),
        json={"decision": "reject"},
    )
    assert decided.status_code == 200, decided.text
    assert (await owner.get(f"{runs.path}/{run_id}")).json()["status"] == "FAILED"
    assert (await owner.get(inbox)).json() == {"items": []}


@pytest.mark.anyio
async def test_archive_paginates_tasks_and_restores_the_entire_group(runs: RunsHarness) -> None:
    owner = runs.client
    inbox = runs.path.replace("/runs", "/inbox")
    workspace = UUID(runs.workspace_id)
    recipient = UUID(runs.user_id)
    sources = []
    for title in ("归档分组", "同时间下一组", "有新通知的组"):
        response = await owner.post(runs.path, json=runs.payload(title), headers=runs.headers(str(uuid4())))
        assert response.status_code == 201, response.text
        record = response.json()
        share = await owner.put(
            runs.path.replace("/runs", f"/tasks/{record['task_id']}/sharing"),
            headers=runs.headers(),
            json={"revision": 0, "title": title, "summary": title, "participant_ids": []},
        )
        assert share.status_code == 204, share.text
        sources.append((UUID(record["task_id"]), UUID(record["id"])))
    (first, first_run), (second, _), (active, _) = sources
    created_at = datetime(2026, 11, 9, tzinfo=UTC)

    def item(
        number: int,
        task: UUID | None,
        *,
        archived: bool = True,
        read: bool = False,
        run: UUID | None = None,
    ) -> InboxItemModel:
        return InboxItemModel(
            id=UUID(int=number),
            workspace_id=workspace,
            recipient_id=recipient,
            task_id=task,
            run_id=run,
            type="approval" if run else "assignment",
            archived=archived,
            read=read,
            created_at=created_at,
        )

    async with runs.sessions.begin() as session:
        session.add_all([item(number, first) for number in range(1, 111)])
        session.add_all(
            [
                item(500, None, read=True, run=first_run),
                item(400, second),
                item(600, active),
                item(601, active, archived=False),
            ]
        )

    archive = inbox + "/archived"
    first_page = (await owner.get(archive, params={"limit": 1})).json()
    assert [entry["task_id"] for entry in first_page["items"]] == [str(first)]
    assert first_page["has_more"]
    cursor = first_page["next_cursor"]
    second_page = (await owner.get(archive, params={"limit": 1, "cursor": cursor})).json()
    assert [entry["task_id"] for entry in second_page["items"]] == [str(second)]
    assert not second_page["has_more"]
    unread = (await owner.get(archive, params={"unread_only": True})).json()
    assert [entry["task_id"] for entry in unread["items"]] == [str(second)]
    assert (await owner.get(archive, params={"cursor": cursor, "unread_only": True})).status_code == 422
    async with member(runs, "其他收件人") as (observer, observer_id):
        assert (await observer.get(archive, params={"cursor": cursor})).status_code == 422
        assert (
            await observer.patch(
                f"{inbox}/groups/{first}",
                headers=headers(observer),
                json={"archived": False},
            )
        ).status_code == 404
        observer_actor = RunActor(UUID(observer_id), workspace)
    actor = RunActor(recipient, workspace)
    service = runs.app.state.inbox_service
    # 未提交的版本不能被另一个会话读到，回滚不会留下刷新信号。
    async with runs.sessions() as session:
        await touch_inbox(session, workspace, {recipient})
        assert await service.revision(actor) == 0
        await session.rollback()
    assert await service.revision(actor) == 0
    restored = await owner.patch(
        f"{inbox}/groups/{first}",
        headers=runs.headers(),
        json={"archived": False},
    )
    assert restored.status_code == 204, restored.text
    current = (await owner.get(inbox)).json()["items"]
    restored_rows = [entry for entry in current if entry["task_id"] == str(first)]
    assert len(restored_rows) == 111
    assert restored_rows[0]["read"] and not restored_rows[1]["read"]
    assert await service.revision(actor) == 1
    assert await service.revision(observer_actor) == 0
    result = await owner.post(inbox + "/bulk", headers=runs.headers(), json={"action": "archive_read"})
    assert result.status_code == 204, result.text
    assert [entry["task_id"] for entry in (await owner.get(inbox)).json()["items"]] == [str(active)]
    assert await service.revision(actor) == 2
