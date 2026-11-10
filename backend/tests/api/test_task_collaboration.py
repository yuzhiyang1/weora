"""用独立登录会话验证协作授权、交接和跨发起人的运行唯一性。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_continuations import finish
from weora.modules.identity.infrastructure.models import WorkspaceMembershipModel
from weora.modules.runs.application.ports import RunSubmission
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.models import RunModel
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork, _record


def headers(client: AsyncClient, key: str = "collaboration") -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["weora_csrf"], "Idempotency-Key": key}


@asynccontextmanager
async def member(
    harness: RunsHarness, name: str, *, same_workspace: bool = True
) -> AsyncIterator[tuple[AsyncClient, str]]:
    async with AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test") as client:
        registered = await client.post(
            "/api/v1/auth/register",
            json={
                "display_name": name,
                "email": f"{uuid4()}@example.com",
                "password": "task-collaboration-test-pass",
                "workspace_name": "协作验证组",
            },
        )
        assert registered.status_code == 201, registered.text
        user_id = registered.json()["user"]["id"]
        if same_workspace:
            async with harness.sessions.begin() as session:
                # 测试为新账号加入现有工作空间；认证仍从真实会话和成员关系解析。
                session.add(
                    WorkspaceMembershipModel(
                        id=uuid4(),
                        workspace_id=UUID(harness.workspace_id),
                        user_id=UUID(user_id),
                        role="member",
                        status="active",
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
            # 会话现在保留已选空间，夹具通过公开入口进入协作所在的空间。
            activated = await client.post(
                f"/api/v1/auth/workspaces/{harness.workspace_id}/activate",
                headers=headers(client),
            )
            assert activated.status_code == 200, activated.text
        yield client, user_id


@pytest.mark.anyio
async def test_shared_task_handoff_isolation_and_concurrent_start(runs: RunsHarness) -> None:
    owner = runs.client
    created = await owner.post(runs.path, json=runs.payload("只有发起人能看到的原始资料"), headers=runs.headers())
    assert created.status_code == 201, created.text
    task_id, old_run_id = created.json()["task_id"], created.json()["id"]
    path = runs.path.replace("/runs", f"/tasks/{task_id}")
    async with member(runs, "林悦") as (recipient, recipient_id), member(runs, "旁观者") as (outsider, outsider_id):
        assert (await recipient.get(path)).status_code == 404
        share = await owner.put(
            f"{path}/sharing",
            headers=runs.headers(),
            json={
                "revision": 0,
                "title": "修复导出空文件",
                "summary": "定位导出失败并执行回归测试",
                "participant_ids": [recipient_id, outsider_id],
            },
        )
        assert share.status_code == 204, share.text
        outsider_view = await outsider.get(path)
        assert outsider_view.status_code == 200
        assert outsider_view.json()["active_run"]["id"] is None
        assert "原始资料" not in outsider_view.text
        assert (await recipient.get(f"{runs.path}/{old_run_id}")).status_code == 404
        start = {
            "revision": 1,
            "kind": "agent",
            "assignee_id": runs.agent_id,
            "version": "0.1.0",
            "mode": "start",
        }
        assert (await outsider.post(f"{path}/assignment", headers=headers(outsider), json=start)).status_code == 404
        assert (await owner.post(f"{path}/assignment", headers=runs.headers("busy"), json=start)).status_code == 409
        handoff = await owner.post(
            f"{path}/assignment",
            headers=runs.headers(),
            json={
                "revision": 1,
                "kind": "member",
                "assignee_id": recipient_id,
                "mode": "assign",
            },
        )
        assert handoff.status_code == 200, handoff.text
        detail = (await recipient.get(path)).json()
        assert detail["assignment"]["id"] == recipient_id and detail["status"] == "backlog"
        assert detail["active_run"] and not detail["active_run"]["stopping"]
        assert (
            await recipient.patch(
                f"{path}/status",
                headers=headers(recipient),
                json={"revision": detail["revision"], "status": "todo"},
            )
        ).status_code == 204
        current = (await recipient.get(path)).json()
        assert current["status"] == "todo"
        start["revision"] = current["revision"]
        assert (
            await recipient.post(
                f"{path}/assignment",
                headers=headers(recipient, "still-active"),
                json=start,
            )
        ).status_code == 409
        await finish(runs, old_run_id)
        assert (await recipient.get(path)).json()["status"] == "todo"
        responses = await asyncio.gather(
            recipient.post(f"{path}/assignment", headers=headers(recipient, "recipient-start"), json=start),
            owner.post(f"{path}/assignment", headers=runs.headers("owner-start"), json=start),
        )
        assert sorted(response.status_code for response in responses) == [200, 409]
        index = 0 if responses[0].status_code == 200 else 1
        winner = recipient if index == 0 else owner
        key = "recipient-start" if index == 0 else "owner-start"
        run_id = responses[index].json()["run_id"]
        replay = await winner.post(f"{path}/assignment", headers=headers(winner, key), json=start)
        assert replay.status_code == 200 and replay.json()["run_id"] == run_id
        own_run = await winner.get(f"{runs.path}/{run_id}")
        assert own_run.status_code == 200, own_run.text
        assert own_run.json()["task_id"] == task_id
        assert own_run.json()["instruction"] == "定位导出失败并执行回归测试"
        async with runs.sessions() as session:
            records = list(await session.scalars(select(RunModel).where(RunModel.task_id == UUID(task_id))))
        assert len(records) == 2
        assert [str(run.id) for run in records if run.status == "QUEUED"] == [run_id]
        launched = next(run for run in records if str(run.id) == run_id)
        assert str(launched.created_by) == (recipient_id if index == 0 else runs.user_id)
        # 即使并发时发起人先获得锁，也单独验证接收者能以自己的身份开下一轮。
        if winner is owner:
            await finish(runs, run_id)
            current = (await recipient.get(path)).json()
            started = await recipient.post(
                f"{path}/assignment",
                headers=headers(recipient, "recipient-next"),
                json=start | {"revision": current["revision"]},
            )
            assert started.status_code == 200, started.text
            run_id = started.json()["run_id"]
        assert (await recipient.get(f"{runs.path}/{run_id}")).status_code == 200
        assert (await owner.get(f"{runs.path}/{run_id}")).status_code == 404
        async with runs.sessions() as session:
            shared_run = await session.get(RunModel, UUID(run_id))
            assert shared_run is not None and str(shared_run.created_by) == recipient_id
            record = _record(shared_run)
        runtime = ModulesExecutionAccess(
            identity=runs.app.state.identity_service,
            agents=runs.app.state.agent_registry_service,
            uow_factory=lambda: SqlAlchemyRunsUnitOfWork(runs.sessions),
        )
        payload = await runtime.load_input(record)
        assert "定位导出失败" in payload.instruction and "原始资料" not in payload.instruction
        # 绕过应用层并发检查，确认数据库约束也会拒绝另一个成员的活跃 Run。
        other = replace(record, id=uuid4(), created_by=UUID(runs.user_id))
        with pytest.raises(IntegrityError, match="uq_runs_active_task|UNIQUE constraint failed: runs.task_id"):
            async with SqlAlchemyRunsUnitOfWork(runs.sessions) as uow:
                await uow.store.add(RunSubmission(other, "db-concurrency-check", "test"), request_id="db-check")
        latest = (await owner.get(path)).json()
        assert (
            await owner.put(
                f"{path}/sharing",
                headers=runs.headers(),
                json={
                    "revision": latest["revision"],
                    "title": "修复导出空文件",
                    "summary": "定位导出失败并执行回归测试",
                    "participant_ids": [recipient_id],
                },
            )
        ).status_code == 204
        assert (await outsider.get(path)).status_code == 404
        assert (await outsider.get(runs.path.replace("/runs", "/tasks"))).json()["items"] == []
        latest = (await owner.get(path)).json()
        assert (
            await owner.put(
                f"{path}/sharing",
                headers=runs.headers(),
                json={
                    "revision": latest["revision"],
                    "title": "修复导出空文件",
                    "summary": "定位导出失败并执行回归测试",
                    "participant_ids": [],
                },
            )
        ).status_code == 204
        assert (await recipient.get(path)).status_code == 404
        assert (await recipient.get(f"{runs.path}/{run_id}")).status_code == 404
        assert (await recipient.get(f"{runs.path}/{run_id}/events")).status_code == 404
        assert (await recipient.get(runs.path)).json()["items"] == []


@pytest.mark.anyio
async def test_stop_handoff_waits_for_worker_and_rejects_external_members(runs: RunsHarness) -> None:
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    task_id, run_id = created.json()["task_id"], created.json()["id"]
    path = runs.path.replace("/runs", f"/tasks/{task_id}")
    async with (
        member(runs, "新负责人") as (recipient, recipient_id),
        member(runs, "外部人员", same_workspace=False) as (external, external_id),
    ):
        share = {"revision": 0, "title": "核对导出", "summary": "运行现有回归", "participant_ids": [external_id]}
        assert (await runs.client.put(f"{path}/sharing", headers=runs.headers(), json=share)).status_code == 409
        share["participant_ids"] = []
        assert (await runs.client.put(f"{path}/sharing", json=share)).status_code == 403
        assert (await runs.client.put(f"{path}/sharing", headers=runs.headers(), json=share)).status_code == 204
        assert (await external.get(path)).status_code == 404
        store = SqlAlchemyExecutionStore(runs.sessions)
        lease = await store.claim(
            workspace_id=UUID(runs.workspace_id),
            run_id=UUID(run_id),
            owner="handoff-test",
            lease_seconds=30,
        )
        assert lease is not None
        stopped = await runs.client.post(
            f"{path}/assignment",
            headers=runs.headers(),
            json={
                "revision": 1,
                "kind": "member",
                "assignee_id": recipient_id,
                "mode": "assign",
                "stop_current": True,
            },
        )
        assert stopped.status_code == 200, stopped.text
        assert (await runs.client.get(path)).json()["active_run"]["stopping"]
        assert (await recipient.get(path)).json()["assignment"]["id"] == recipient_id
        assert await store.finish(lease, status="CANCELLED")
        assert (await recipient.get(path)).json()["assignment"]["id"] == recipient_id
