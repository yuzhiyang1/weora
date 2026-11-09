"""持续 Task 的真实事务与 HTTP 链路；模型结果明确由夹具提供。"""

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, update

from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.agents.application.service import AgentRegistryService
from weora.modules.collaboration.infrastructure.tasks import TaskInputModel, TaskModel
from weora.modules.delivery.infrastructure.persistence import DeliveryPlanModel
from weora.modules.identity.application.service import IdentityService
from weora.modules.identity.infrastructure.models import WorkspaceMembershipModel
from weora.modules.runs.application.ports import ExecutionProfile, RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.models import RunModel
from weora.modules.runs.infrastructure.pi_runtime import PiTextRuntime
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork


def actor(h: RunsHarness) -> RunActor:
    return RunActor(UUID(h.user_id), UUID(h.workspace_id))


async def create(h: RunsHarness, instruction: str = "记住本任务的测试代号是青竹。") -> dict[str, object]:
    response = await h.client.post(h.path, json=h.payload(instruction), headers=h.headers())
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


async def finish(h: RunsHarness, run_id: object, output: str = "已记住：青竹。") -> None:
    store = SqlAlchemyExecutionStore(h.sessions)
    lease = await store.claim(
        workspace_id=UUID(h.workspace_id), run_id=UUID(str(run_id)), owner="test", lease_seconds=30,
    )
    assert lease is not None
    assert await store.finish(lease, status="SUCCEEDED", output=output)


async def follow(h: RunsHarness, run_id: object, text: str, key: str = "follow-1") -> dict[str, object]:
    response = await h.client.post(f"{h.path}/{run_id}/continue", json={"instruction": text}, headers=h.headers(key))
    assert response.status_code == 202, response.text
    return cast(dict[str, object], response.json())


@pytest.mark.anyio
async def test_followup_survives_app_restart_and_starts_a_new_run_on_same_task(runs: RunsHarness) -> None:
    first = await create(runs)
    queued = await follow(runs, first["id"], "刚才的测试代号是什么？")
    assert queued["status"] == "PENDING" and queued["run_id"] is None
    service = cast(RunService, runs.app.state.run_service)
    assert await service.dispatch_task_inputs() == 0
    await finish(runs, first["id"])
    # 重新装配服务，队列、任务归属和输入完全从数据库恢复。
    from weora.modules.runs.infrastructure.agent_catalog import AgentsModuleVersionCatalog
    fresh = RunService(
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(runs.sessions),
        agent_catalog=AgentsModuleVersionCatalog(cast(AgentRegistryService, runs.app.state.agent_registry_service)),
    )
    assert await fresh.dispatch_task_inputs() == 1
    assert await fresh.dispatch_task_inputs() == 0
    accepted = await fresh.task_input(actor(runs), UUID(str(queued["input_id"])))
    assert accepted.run_id is not None and str(accepted.run_id) != first["id"]
    detail = await service.get(actor=actor(runs), workspace_id=UUID(runs.workspace_id), run_id=accepted.run_id)
    assert str(detail.task.id) == first["task_id"]
    assert detail.task.instruction == "刚才的测试代号是什么？"
    assert detail.run.agent_version_id == UUID(runs.version_id)
    original = await service.get(actor=actor(runs), workspace_id=UUID(runs.workspace_id), run_id=UUID(str(first["id"])))
    assert original.task.instruction == "记住本任务的测试代号是青竹。"
    async with runs.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TaskModel)) == 1
        assert await session.scalar(select(func.count()).select_from(RunModel)) == 2


@pytest.mark.anyio
async def test_concurrent_input_retries_are_idempotent_and_preserve_order(runs: RunsHarness) -> None:
    first = await create(runs)
    same = await asyncio.gather(*(follow(runs, first["id"], "同一条跟进", "same-input") for _ in range(4)))
    assert len({str(item["input_id"]) for item in same}) == 1
    later = await asyncio.gather(*(follow(runs, first["id"], f"跟进 {i}", f"different-{i}") for i in range(3)))
    assert len({item["sequence"] for item in later}) == 3
    conflict = await runs.client.post(f"{runs.path}/{first['id']}/continue", json={"instruction": "换正文"},
                                      headers=runs.headers("same-input"))
    assert conflict.status_code == 409
    await finish(runs, first["id"])
    service = cast(RunService, runs.app.state.run_service)
    counts = await asyncio.gather(*(service.dispatch_task_inputs() for _ in range(3)))
    assert sum(counts) == 1
    receipt = await service.task_input(actor(runs), UUID(str(same[0]["input_id"])))
    assert receipt.run_id is not None
    for item in later:
        assert (await service.task_input(actor(runs), UUID(str(item["input_id"])))).run_id is None
    async with runs.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RunModel)) == 2
        assert await session.scalar(select(func.count()).select_from(TaskInputModel)) == 4


@pytest.mark.anyio
async def test_run_insert_failure_keeps_accepted_input_for_retry(runs: RunsHarness) -> None:
    first = await create(runs)
    item = await follow(runs, first["id"], "事务失败后仍须处理")
    await finish(runs, first["id"])
    service = cast(RunService, runs.app.state.run_service)

    def fail_insert(*_: object) -> None:
        raise RuntimeError("injected follow-up launch failure")

    event.listen(RunModel, "before_insert", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            await service.dispatch_task_inputs()
    finally:
        event.remove(RunModel, "before_insert", fail_insert)
    receipt = await service.task_input(actor(runs), UUID(str(item["input_id"])))
    assert receipt.status == "PENDING" and receipt.run_id is None
    assert await service.dispatch_task_inputs() == 1


@pytest.mark.anyio
async def test_cancel_discards_preexisting_queue_but_new_explicit_request_can_continue(runs: RunsHarness) -> None:
    first = await create(runs)
    old = await follow(runs, first["id"], "叫停前排队的消息")
    assert (await runs.client.post(f"{runs.path}/{first['id']}/cancel", headers=runs.headers())).status_code == 200
    service = cast(RunService, runs.app.state.run_service)
    assert await service.dispatch_task_inputs() == 0
    assert (await service.task_input(actor(runs), UUID(str(old["input_id"])))).status == "REJECTED"
    new = await follow(runs, first["id"], "现在明确重新开始", "after-stop")
    assert new["run_id"] is not None


@pytest.mark.anyio
async def test_continuation_permissions_csrf_and_forbidden_fields(runs: RunsHarness) -> None:
    first = await create(runs)
    url = f"{runs.path}/{first['id']}/continue"
    assert (await runs.client.post(url, json={"instruction": "消息"},
                                  headers={"Idempotency-Key": "no-csrf"})).status_code == 403
    assert (await runs.client.post(url, json={"instruction": "消息", "approved": True},
                                  headers=runs.headers())).status_code == 422
    assert (await runs.client.post(url, json={"instruction": " "}, headers=runs.headers())).status_code == 422
    assert (await runs.client.post(f"{runs.path}/{uuid4()}/continue", json={"instruction": "消息"},
                                  headers=runs.headers())).status_code == 404
    async with runs.sessions() as session, session.begin():
        await session.execute(update(WorkspaceMembershipModel).where(
            WorkspaceMembershipModel.user_id == UUID(runs.user_id),
        ).values(status="inactive"))
    assert (await runs.client.post(url, json={"instruction": "撤权后的消息"},
                                  headers=runs.headers())).status_code in {401, 403}


@pytest.mark.anyio
async def test_new_run_has_history_but_never_inherits_approval(runs: RunsHarness) -> None:
    first = await create(runs)
    await finish(runs, first["id"])
    now = datetime.now(UTC)
    async with runs.sessions() as session, session.begin():
        session.add(DeliveryPlanModel(
            id=uuid4(), run_id=UUID(str(first["id"])), workspace_id=UUID(runs.workspace_id),
            created_by=UUID(runs.user_id), version=1, scope={}, scope_sha256="0" * 64,
            summary="旧批准仅供测试，不执行", risk="test", status="APPROVED", created_at=now,
            expires_at=now + timedelta(hours=1),
        ))
    item = await follow(runs, first["id"], "接着做，但必须重新审批")
    run_id = UUID(str(item["run_id"]))
    service = cast(RunService, runs.app.state.run_service)
    detail = await service.get(actor=actor(runs), workspace_id=UUID(runs.workspace_id), run_id=run_id)
    access = ModulesExecutionAccess(
        identity=cast(IdentityService, runs.app.state.identity_service),
        agents=cast(AgentRegistryService, runs.app.state.agent_registry_service),
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(runs.sessions),
    )
    payload = await access.load_input(detail.run)
    assert "青竹" in payload.instruction and "接着做" in payload.instruction
    assert "旧计划和旧批准不能用于本次 Run" in payload.instruction
    async with runs.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(DeliveryPlanModel).where(
            DeliveryPlanModel.run_id == run_id,
        )) == 0


@pytest.mark.anyio
async def test_task_retention_purges_pending_message_copies(runs: RunsHarness) -> None:
    first = await create(runs)
    queued = await follow(runs, first["id"], "不能在正文到期后保留这条消息")
    async with SqlAlchemyRunsUnitOfWork(runs.sessions) as uow:
        await uow.tasks.purge_content(UUID(runs.workspace_id), {UUID(str(first["task_id"]))})
        await uow.commit()
    async with runs.sessions() as session:
        row = await session.get(TaskInputModel, UUID(str(queued["input_id"])))
        assert row is not None and row.instruction == "[正文已过期]" and row.status == "REJECTED"


@pytest.mark.anyio
async def test_waiting_approval_keeps_followup_pending(runs: RunsHarness) -> None:
    first = await create(runs)
    # 等待人类审批仍占据原 Run；跟进不能绕开审批另开并行执行。
    async with runs.sessions() as session, session.begin():
        await session.execute(update(RunModel).where(RunModel.id == UUID(str(first["id"]))).values(
            status="WAITING_APPROVAL",
        ))
    item = await follow(runs, first["id"], "补充要求，稍后处理")
    service = cast(RunService, runs.app.state.run_service)
    assert await service.dispatch_task_inputs() == 0
    assert item["run_id"] is None and item["status"] == "PENDING"
    async with runs.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RunModel)) == 1


@pytest.mark.anyio
async def test_bounded_scan_does_not_starve_ready_task(runs: RunsHarness) -> None:
    busy = await create(runs)
    await follow(runs, busy["id"], "繁忙任务的输入", "busy-input")
    response = await runs.client.post(runs.path, json=runs.payload("独立任务"), headers=runs.headers("ready-task"))
    assert response.status_code == 201
    ready = response.json()
    await follow(runs, ready["id"], "可以处理的输入", "ready-input")
    await finish(runs, ready["id"])
    service = cast(RunService, runs.app.state.run_service)
    # 即使扫描窗口只有一项，检查繁忙项后也应将机会交给下一项。
    assert sum([await service.dispatch_task_inputs(limit=1) for _ in range(2)]) == 1


@pytest.mark.anyio
async def test_background_dispatch_rechecks_membership(runs: RunsHarness) -> None:
    first = await create(runs)
    item = await follow(runs, first["id"], "撤权前已接收的跟进")
    await finish(runs, first["id"])
    async with runs.sessions() as session, session.begin():
        await session.execute(update(WorkspaceMembershipModel).where(
            WorkspaceMembershipModel.user_id == UUID(runs.user_id),
        ).values(status="inactive"))
    service = cast(RunService, runs.app.state.run_service)
    assert await service.dispatch_task_inputs() == 0
    async with runs.sessions() as session:
        stored = await session.get(TaskInputModel, UUID(str(item["input_id"])))
        assert stored is not None and stored.status == "REJECTED"
        assert await session.scalar(select(func.count()).select_from(RunModel)) == 1


@pytest.mark.anyio
async def test_real_pi_loop_receives_restored_task_history(runs: RunsHarness) -> None:
    """Python → 真实 Node/Pi Agent → 脚本化提供方；不使用外部模型或真实密钥。"""
    first = await create(runs)
    await finish(runs, first["id"])
    item = await follow(runs, first["id"], "请根据上一轮回答给出代号")
    service = cast(RunService, runs.app.state.run_service)
    detail = await service.get(actor=actor(runs), workspace_id=UUID(runs.workspace_id),
                               run_id=UUID(str(item["run_id"])))
    access = ModulesExecutionAccess(
        identity=cast(IdentityService, runs.app.state.identity_service),
        agents=cast(AgentRegistryService, runs.app.state.agent_registry_service),
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(runs.sessions),
    )
    payload = await access.load_input(detail.run)
    node = shutil.which("node")
    assert node is not None, "本验收需要本机 Node，不跳过实际 Pi 链路"
    runtime = PiTextRuntime(node_executable=node,
                            entry=Path(__file__).parents[1] / "fixtures/pi_continuation.mjs", api_key="fixture-only")
    output = await runtime.run(payload, ExecutionProfile("pi-agent-core@1.3.0", "deepseek", "fixture", 128, 15))
    result = json.loads(output)
    assert result == {"history_seen": True, "request_seen": True, "answer": "青竹"}
