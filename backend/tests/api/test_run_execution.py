"""在真实事务上验证 Worker 领取、租约、取消和结果提交，不请求真实模型。"""

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, update

from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.agents.application.service import AgentRegistryService
from weora.modules.identity.application.service import IdentityService
from weora.modules.identity.infrastructure.models import WorkspaceMembershipModel
from weora.modules.runs.application.execution import RunExecutor, RunLease, RuntimeFailure, RuntimeInput
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.agent_catalog import AgentsModuleVersionCatalog
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.models import RunDispatchModel, RunEventModel, RunModel
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork

PROFILE = ExecutionProfile("pi-agent-core@1.3.0", "deepseek", "explicit-test-model", 128, 1)


@pytest.fixture
def configured(runs: RunsHarness) -> RunsHarness:
    """只注入非敏感配置；执行适配器由测试提供，不读取开发者密钥。"""
    runs.app.state.run_service = RunService(
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(runs.sessions),
        agent_catalog=AgentsModuleVersionCatalog(cast(AgentRegistryService, runs.app.state.agent_registry_service)),
        execution_profile=PROFILE,
    )
    return runs


class ScriptedRuntime:
    """脚本化 Runtime 仅验证平台生命周期，不称为真实模型联调。"""

    def __init__(self, *, hold: bool = False, fail: str | None = None) -> None:
        self.hold = hold
        self.fail = fail
        self.calls: list[RuntimeInput] = []
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
        self.calls.append(payload)
        self.started.set()
        try:
            if self.hold:
                await asyncio.Event().wait()
            if self.fail:
                raise RuntimeFailure(self.fail)
            return "脚本化回答，仅供平台测试。"
        finally:
            self.stopped.set()


@pytest.mark.anyio
async def test_executor_distinguishes_overall_deadline_from_model_budget(configured: RunsHarness) -> None:
    profile = replace(PROFILE, execution_timeout_seconds=10)
    configured.app.state.run_service._execution_profile = profile

    class SlowToolRuntime(ScriptedRuntime):
        async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
            await asyncio.sleep(1.2)
            return "工具等待超过模型预算，但没有超过整体预算。"

    run_id = await create(configured)
    assert await executor(configured, SlowToolRuntime(), profile=profile).execute(
        workspace_id=UUID(configured.workspace_id), run_id=run_id, owner="slow-tool-worker"
    )
    assert (await configured.client.get(f"{configured.path}/{run_id}")).json()["status"] == "SUCCEEDED"

    bounded = replace(PROFILE, timeout_seconds=5, execution_timeout_seconds=1)
    configured.app.state.run_service._execution_profile = bounded
    another = await configured.client.post(
        configured.path, json=configured.payload("整体时限"), headers=configured.headers("wall-limit")
    )
    assert another.status_code == 201
    held = ScriptedRuntime(hold=True)
    assert await executor(configured, held, profile=bounded).execute(
        workspace_id=UUID(configured.workspace_id), run_id=UUID(another.json()["id"]), owner="wall-worker"
    )
    result = (await configured.client.get(f"{configured.path}/{another.json()['id']}")).json()
    assert result["status"] == "FAILED" and result["failure_code"] == "RUN_TIMEOUT" and held.stopped.is_set()


def executor(runs: RunsHarness, runtime: ScriptedRuntime, *, profile: ExecutionProfile = PROFILE) -> RunExecutor:
    return RunExecutor(
        store=SqlAlchemyExecutionStore(runs.sessions),
        access=ModulesExecutionAccess(
            identity=cast(IdentityService, runs.app.state.identity_service),
            agents=cast(AgentRegistryService, runs.app.state.agent_registry_service),
            uow_factory=lambda: SqlAlchemyRunsUnitOfWork(runs.sessions),
        ),
        runtime=runtime,
        profile=profile,
        lease_seconds=3,
        heartbeat_seconds=0.05,
    )


async def create(runs: RunsHarness) -> UUID:
    response = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


@pytest.mark.anyio
async def test_worker_persists_real_result_and_duplicate_messages_do_not_execute(configured: RunsHarness) -> None:
    run_id = await create(configured)
    runtime = ScriptedRuntime()
    worker = executor(configured, runtime)
    results = await asyncio.gather(
        *(
            worker.execute(workspace_id=UUID(configured.workspace_id), run_id=run_id, owner=f"worker-{index}")
            for index in range(6)
        )
    )
    assert results.count(True) == 1
    assert len(runtime.calls) == 1
    detail = (await configured.client.get(f"{configured.path}/{run_id}")).json()
    assert detail["status"] == "SUCCEEDED"
    assert detail["output_text"] == "脚本化回答，仅供平台测试。"
    assert detail["execution_profile"]["runtime_id"] == "pi-agent-core@1.3.0"
    assert detail["execution_profile"]["model_id"] == "explicit-test-model"
    assert "api_key" not in str(detail)
    assert "lease_owner" not in detail
    assert "fencing_token" not in detail
    events = (await configured.client.get(f"{configured.path}/{run_id}/events")).json()
    assert [item["event_type"] for item in events["items"]] == ["run.queued", "run.started", "run.succeeded"]
    assert [item["sequence"] for item in events["items"]] == [1, 2, 3]
    assert "脚本化回答" not in str(events)
    assert "output_text" not in (await configured.client.get(configured.path)).json()["items"][0]


@pytest.mark.anyio
async def test_terminal_commit_ack_is_not_cancelled_by_model_deadline(
    configured: RunsHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库已提交但返回较慢时，不能因模型期限到达把真实成功改报为未完成。"""
    finish = SqlAlchemyExecutionStore.finish

    async def delayed_finish(
        self: SqlAlchemyExecutionStore,
        lease: RunLease,
        *,
        status: Literal["SUCCEEDED", "FAILED", "CANCELLED"],
        output: str | None = None,
        failure_code: str | None = None,
    ) -> bool:
        result = await finish(self, lease, status=status, output=output, failure_code=failure_code)
        await asyncio.sleep(1.1)
        return result

    monkeypatch.setattr(SqlAlchemyExecutionStore, "finish", delayed_finish)
    run_id = await create(configured)
    assert await executor(configured, ScriptedRuntime()).execute(
        workspace_id=UUID(configured.workspace_id), run_id=run_id, owner="late-commit-ack"
    )
    detail = (await configured.client.get(f"{configured.path}/{run_id}")).json()
    assert detail["status"] == "SUCCEEDED"


@pytest.mark.anyio
async def test_workspace_checkpoint_failure_prevents_false_completion(configured: RunsHarness) -> None:
    """文件保存失败须保留明确失败事实，不能只留下成功文本和已清理的容器。"""
    run_id = await create(configured)
    worker = executor(configured, ScriptedRuntime())

    async def capture(lease: RunLease) -> None:
        raise RuntimeFailure("WORKSPACE_CAPTURE_FAILED")

    worker._before_finish = capture
    assert await worker.execute(workspace_id=UUID(configured.workspace_id), run_id=run_id, owner="capture-failure")
    detail = (await configured.client.get(f"{configured.path}/{run_id}")).json()
    assert detail["status"] == "FAILED" and detail["failure_code"] == "WORKSPACE_CAPTURE_FAILED"
    assert detail["output_text"] is None


@dataclass
class Clock:
    """精确推进租约边界，不通过等待制造易抖动的过期测试。"""

    now: datetime

    def __call__(self) -> datetime:
        return self.now


@pytest.mark.anyio
async def test_expired_fenced_or_wrong_owner_cannot_commit_or_revive(configured: RunsHarness) -> None:
    run_id = await create(configured)
    clock = Clock(datetime.now(UTC))
    store = SqlAlchemyExecutionStore(configured.sessions, clock=clock)
    lease = await store.claim(
        workspace_id=UUID(configured.workspace_id), run_id=run_id, owner="worker-a", lease_seconds=3
    )
    assert lease is not None
    assert (
        await store.finish(replace(lease, fencing_token=lease.fencing_token + 1), status="SUCCEEDED", output="迟到")
        is False
    )
    assert await store.renew(replace(lease, owner="worker-b"), lease_seconds=3) is False
    clock.now += timedelta(seconds=3)
    assert await store.renew(lease, lease_seconds=3) is False
    assert await store.finish(lease, status="SUCCEEDED", output="迟到") is False
    assert await store.recover_expired() == 1
    assert await store.recover_expired() == 0
    assert await store.finish(lease, status="SUCCEEDED", output="迟到") is False
    assert (
        await store.claim(workspace_id=UUID(configured.workspace_id), run_id=run_id, owner="new", lease_seconds=3)
        is None
    )
    detail = (await configured.client.get(f"{configured.path}/{run_id}")).json()
    assert detail["status"] == "RECOVERY_REQUIRED"
    assert detail["failure_code"] == "LEASE_EXPIRED"
    assert detail["output_text"] is None


@pytest.mark.anyio
async def test_running_cancellation_waits_for_runtime_cleanup(configured: RunsHarness) -> None:
    run_id = await create(configured)
    runtime = ScriptedRuntime(hold=True)
    task = asyncio.create_task(
        executor(configured, runtime).execute(
            workspace_id=UUID(configured.workspace_id),
            run_id=run_id,
            owner="worker",
        )
    )
    await asyncio.wait_for(runtime.started.wait(), 3)
    path = f"{configured.path}/{run_id}"
    response = await configured.client.post(f"{path}/cancel", headers=configured.headers())
    assert response.status_code == 200
    assert response.json()["status"] == "RUNNING"
    assert response.json()["cancellation_requested_at"] is not None
    assert await asyncio.wait_for(task, 3) is True
    assert runtime.stopped.is_set()
    detail = (await configured.client.get(path)).json()
    assert detail["status"] == "CANCELLED"
    assert detail["failure_code"] == "REMOTE_COMPLETION_UNCONFIRMED"
    assert detail["output_text"] is None


@pytest.mark.anyio
async def test_cancel_request_wins_against_success_commit(configured: RunsHarness) -> None:
    run_id = await create(configured)
    store = SqlAlchemyExecutionStore(configured.sessions)
    lease = await store.claim(
        workspace_id=UUID(configured.workspace_id), run_id=run_id, owner="worker", lease_seconds=30
    )
    assert lease is not None
    path = f"{configured.path}/{run_id}"
    assert (await configured.client.post(f"{path}/cancel", headers=configured.headers())).status_code == 200
    assert await store.finish(lease, status="SUCCEEDED", output="已经算出的但未交付的回答") is True
    assert await store.finish(lease, status="SUCCEEDED", output="迟到再次提交") is False
    detail = (await configured.client.get(path)).json()
    assert detail["status"] == "CANCELLED"
    assert detail["output_text"] is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,hold,expected",
    [
        ("MODEL_FAILED", False, "MODEL_FAILED"),
        (None, True, "MODEL_TIMEOUT"),
    ],
)
async def test_model_failure_and_timeout_are_not_success(
    configured: RunsHarness,
    failure: str | None,
    hold: bool,
    expected: str,
) -> None:
    profile = PROFILE
    if not hold:
        # 模型报错用例不检验超时；为材料装配留出余量，避免繁忙机器先触发一秒总时限。
        profile = replace(PROFILE, timeout_seconds=10)
        configured.app.state.run_service._execution_profile = profile
    run_id = await create(configured)
    runtime = ScriptedRuntime(hold=hold, fail=failure)
    assert (
        await executor(configured, runtime, profile=profile).execute(
            workspace_id=UUID(configured.workspace_id),
            run_id=run_id,
            owner="worker",
        )
        is True
    )
    assert runtime.stopped.is_set()
    detail = (await configured.client.get(f"{configured.path}/{run_id}")).json()
    assert detail["status"] == "FAILED"
    assert detail["failure_code"] == expected
    assert detail["output_text"] is None


@pytest.mark.anyio
async def test_changed_model_configuration_fails_closed(configured: RunsHarness) -> None:
    run_id = await create(configured)
    runtime = ScriptedRuntime()
    assert (
        await executor(configured, runtime, profile=replace(PROFILE, model_id="different-model")).execute(
            workspace_id=UUID(configured.workspace_id),
            run_id=run_id,
            owner="worker",
        )
        is True
    )
    assert runtime.calls == []
    detail = (await configured.client.get(f"{configured.path}/{run_id}")).json()
    assert detail["failure_code"] == "MODEL_CONFIGURATION_MISMATCH"


@pytest.mark.anyio
async def test_revoked_member_cannot_execute_or_keep_generated_output(configured: RunsHarness) -> None:
    run_id = await create(configured)
    runtime = ScriptedRuntime(hold=True)
    task = asyncio.create_task(
        executor(configured, runtime).execute(
            workspace_id=UUID(configured.workspace_id),
            run_id=run_id,
            owner="worker",
        )
    )
    await asyncio.wait_for(runtime.started.wait(), 3)
    async with configured.sessions() as session:
        await session.execute(
            update(WorkspaceMembershipModel)
            .where(
                WorkspaceMembershipModel.user_id == UUID(configured.user_id),
            )
            .values(status="inactive")
        )
        await session.commit()
    assert await asyncio.wait_for(task, 3) is True
    assert runtime.stopped.is_set()
    async with configured.sessions() as session:
        model = await session.get(RunModel, run_id)
        assert model is not None
        assert model.status == "FAILED"
        assert model.failure_code == "ACCESS_REVOKED"
        assert model.output_text is None
    assert (await configured.client.get(f"{configured.path}/{run_id}")).status_code == 401


@pytest.mark.anyio
async def test_fixed_version_input_does_not_follow_draft(configured: RunsHarness) -> None:
    run_id = await create(configured)
    response = await configured.client.patch(
        f"/api/v1/workspaces/{configured.workspace_id}/agents/{configured.agent_id}/draft",
        json={"system_prompt": "不应进入历史运行的草稿", "skill_version_ids": []},
        headers=configured.headers(),
    )
    assert response.status_code == 200
    runtime = ScriptedRuntime()
    await executor(configured, runtime).execute(
        workspace_id=UUID(configured.workspace_id), run_id=run_id, owner="worker"
    )
    assert runtime.calls[0].system_prompt == "你是研发助手。"


@pytest.mark.anyio
async def test_failed_terminal_event_rolls_back_output_and_status(configured: RunsHarness) -> None:
    run_id = await create(configured)
    store = SqlAlchemyExecutionStore(configured.sessions)
    lease = await store.claim(
        workspace_id=UUID(configured.workspace_id), run_id=run_id, owner="worker", lease_seconds=30
    )
    assert lease is not None

    def fail(_: object, __: object, target: RunEventModel) -> None:
        if target.event_type == "run.succeeded":
            raise RuntimeError("injected event storage failure")

    event.listen(RunEventModel, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="injected event storage failure"):
            await store.finish(lease, status="SUCCEEDED", output="不应部分保存")
    finally:
        event.remove(RunEventModel, "before_insert", fail)
    detail = (await configured.client.get(f"{configured.path}/{run_id}")).json()
    assert detail["status"] == "RUNNING"
    assert detail["output_text"] is None
    assert detail["last_sequence"] == 2


@pytest.mark.anyio
async def test_lost_queue_delivery_can_be_resent_and_cancelled_dispatch_never_claimed(configured: RunsHarness) -> None:
    run_id = await create(configured)
    clock = Clock(datetime.now(UTC))
    store = SqlAlchemyExecutionStore(configured.sessions, clock=clock)
    assert await store.pending_dispatches() == [(UUID(configured.workspace_id), run_id)]
    await store.mark_dispatched(workspace_id=UUID(configured.workspace_id), run_id=run_id)
    assert await store.pending_dispatches() == []
    clock.now += timedelta(seconds=31)
    assert await store.pending_dispatches() == [(UUID(configured.workspace_id), run_id)]
    await configured.client.post(f"{configured.path}/{run_id}/cancel", headers=configured.headers())
    assert await store.pending_dispatches() == []
    assert (
        await store.claim(workspace_id=UUID(configured.workspace_id), run_id=run_id, owner="worker", lease_seconds=3)
        is None
    )
    async with configured.sessions() as session:
        # 已发送的队列消息不能召回，保留投递事实；Run 终态阻止它再次领取。
        assert await session.scalar(select(RunDispatchModel.status)) == "DISPATCHED"


@pytest.mark.anyio
async def test_wrong_workspace_cannot_claim_queued_run(configured: RunsHarness) -> None:
    run_id = await create(configured)
    store = SqlAlchemyExecutionStore(configured.sessions)
    assert await store.claim(workspace_id=uuid4(), run_id=run_id, owner="worker", lease_seconds=3) is None
