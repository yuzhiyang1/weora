"""真实协调和 Run 事务复现停止队列滞后；模型替身不作为外部验收。"""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest

from tests.api.test_runs import runs as runs
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.coordination.test_service import configure, decide, receive
from weora.modules.coordination.domain import Json, State
from weora.modules.coordination.engine import Engine
from weora.modules.coordination.worker_runtime import CoordinatedRuntime
from weora.modules.runs.application.execution import RunExecutor, RunLease, RuntimeInput, RuntimeSession
from weora.modules.runs.application.harness import OMP_RPC_RUNTIME, PI_RPC_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile, RunActor, RunRecord
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore


@pytest.mark.anyio
@pytest.mark.parametrize("runtime_id", [PI_RPC_RUNTIME, OMP_RPC_RUNTIME])
@pytest.mark.parametrize("cause", ["human_stop", "revoked"])
async def test_stop_beats_pending_cancel_effect_but_revocation_remains_failure(
    contextual: ContextRuns, runtime_id: str, cause: str,
) -> None:
    h = contextual
    service, scope_id = await configure(h)
    profile = ExecutionProfile(runtime_id, "deepseek", "test-native-stop", 1024, 30)
    # 只注入冻结的测试模型配置；不调用真实供应商或更换应用授权规则。
    service.runs._execution_profile = profile
    service.runs._profile_provider = None
    await receive(h, service, topic="omt_stop_race")
    ticket_id = await decide(h, service, scope_id)
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    await service.launch(h.actor.workspace_id, scope_id, next(item for item in state.effects if item.kind == "worker"))
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    run_id = UUID(str(state.tickets[0].assignments[-1].run_id))
    store = SqlAlchemyExecutionStore(h.original.sessions)
    process_closed: list[bool] = []
    leases: list[RunLease] = []

    class Access:
        async def is_active(self, actor: RunActor) -> bool:
            return True

        async def load_input(self, run: RunRecord) -> RuntimeInput:
            return RuntimeInput("", "保留原目标与会话")

    class SessionWorker:
        def __init__(
            self, session: RuntimeSession | None, call: Callable[[str, str, Json], Awaitable[Json]] | None,
        ) -> None:
            assert session
            self.session = session

        async def run(self, payload: RuntimeInput, selected: ExecutionProfile) -> str:
            assert selected == profile and self.session.pull
            try:
                if cause == "human_stop":
                    await receive(h, service, identity="om_stop_race", topic="omt_stop_race",
                                  addressed=True, instruction="停止当前执行")
                    await service.store.transact(h.actor.workspace_id, scope_id,
                                                 lambda engine, _: engine.stop(ticket_id, "om_stop_race"))
                else:
                    def revoke(_: Engine, current: State) -> None:
                        current.triggers[0].authorized = False
                    await service.store.transact(h.actor.workspace_id, scope_id, revoke)
                # 刻意不消费 cancel 效果：会话检查先看到停止/撤权，精确复现线上窗口。
                current = await service.runs.access_metadata(h.actor, run_id)
                assert current and current.run.status == "RUNNING" and current.run.cancellation_requested_at is None
                await self.session.pull()
                pytest.fail("停止或撤权后不得继续执行")
            finally:
                process_closed.append(True)

    async def gateway(identity: str, name: str, arguments: Json) -> Json:
        return {"ok": True}

    def factory(lease: RunLease) -> CoordinatedRuntime:
        leases.append(lease)
        async def authorize() -> bool:
            return await store.authorize_token(h.actor, run_id, lease.fencing_token, lease.owner)
        return CoordinatedRuntime(service, lease, runtime_factory=SessionWorker, call_tool=gateway, authorize=authorize)

    async def before_finish(lease: RunLease) -> None:
        assert process_closed, "取消终态必须晚于受控进程退出"
        await service.confirm_run_stop(lease)

    executor = RunExecutor(store=store, access=Access(), runtime=cast(SessionWorker, None), profile=profile,
                           runtime_factory=factory, before_finish=before_finish, heartbeat_seconds=0.1)
    assert await executor.execute(workspace_id=h.actor.workspace_id, run_id=run_id, owner="native-stop-race")
    detail = await service.runs.access_metadata(h.actor, run_id)
    assert detail
    if cause == "human_stop":
        assert detail.run.status == "CANCELLED" and detail.run.failure_code is None
        assert detail.run.cancellation_requested_at is not None
        pending = await service.store.snapshot(h.actor.workspace_id, scope_id)
        assert any(item.kind == "cancel" and item.state == "pending" for item in pending.effects)
        sequence = detail.run.last_sequence
        await service.confirm_run_stop(leases[0])
        again = await service.runs.access_metadata(h.actor, run_id)
        assert again and again.run.last_sequence == sequence
    else:
        assert detail.run.status == "FAILED" and detail.run.failure_code == "ACCESS_REVOKED"
        assert detail.run.cancellation_requested_at is None


@pytest.mark.anyio
async def test_old_run_lease_cannot_cancel_a_new_assignment(contextual: ContextRuns) -> None:
    h = contextual
    service, scope_id = await configure(h)
    await receive(h, service, topic="omt_stop_binding")
    ticket_id = await decide(h, service, scope_id)
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    await service.launch(h.actor.workspace_id, scope_id, next(item for item in state.effects if item.kind == "worker"))
    ticket = (await service.store.snapshot(h.actor.workspace_id, scope_id)).tickets[0]
    run_id = UUID(str(ticket.assignments[-1].run_id))
    store = SqlAlchemyExecutionStore(h.original.sessions)
    lease = await store.claim(workspace_id=h.actor.workspace_id, run_id=run_id, owner="stop-binding", lease_seconds=30)
    assert lease
    await receive(h, service, identity="om_stop_binding", topic="omt_stop_binding",
                  addressed=True, instruction="停止当前执行")
    await service.store.transact(h.actor.workspace_id, scope_id,
                                 lambda engine, _: engine.stop(ticket_id, "om_stop_binding"))
    await service.confirm_run_stop(replace(lease, fencing_token=lease.fencing_token - 1))
    detail = await service.runs.access_metadata(h.actor, run_id)
    assert detail and detail.run.cancellation_requested_at is None
    def rebind(_: Engine, current: State) -> None:
        current.tickets[0].assignments[-1].run_id = "00000000-0000-4000-8000-000000000001"
    await service.store.transact(h.actor.workspace_id, scope_id, rebind)
    await service.confirm_run_stop(lease)
    detail = await service.runs.access_metadata(h.actor, run_id)
    assert detail and detail.run.status == "RUNNING" and detail.run.cancellation_requested_at is None
