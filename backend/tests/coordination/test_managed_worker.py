"""真实协调状态与原生 Run 的 Worker 生命周期；Pi 端口是脚本夹具，审批不在夹具内伪造。"""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest

from tests.api.test_runs import runs as runs
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.coordination.test_service import configure, decide, receive
from weora.modules.coordination.domain import Config, Json, State
from weora.modules.coordination.engine import Engine
from weora.modules.coordination.store import SqlCoordinationStore
from weora.modules.coordination.worker_runtime import CoordinatedRuntime
from weora.modules.runs.application.execution import RuntimeInput
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.pi_runtime import PiSession
from weora.modules.tools.application.gateway import ToolSpec


@pytest.mark.anyio
@pytest.mark.parametrize("handoff", [False, True])
async def test_child_steering_completion_keeps_run_slot_and_handoff_cancels_descendants(
    contextual: ContextRuns,
    handoff: bool,
) -> None:
    h = contextual
    service, scope_id = await configure(h)
    if h.original.sessions.kw["bind"].dialect.name == "sqlite":
        # SQLite 没有 SELECT FOR UPDATE；仅在测试端口补同范围串行，真实并发另由 PostgreSQL 用例证明。
        class SerializedStore(SqlCoordinationStore):
            lock = asyncio.Lock()

            async def _transact[T](
                self,
                workspace_id: UUID,
                scope_id: str,
                action: Callable[[Engine, State], T],
                *,
                create: Config | None = None,
            ) -> T:
                async with self.lock:
                    return await super()._transact(workspace_id, scope_id, action, create=create)

        service.store = SerializedStore(h.original.sessions, h.context.cipher)
    await receive(h, service)
    ticket_id = await decide(h, service, scope_id)
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    await service.launch(h.actor.workspace_id, scope_id, next(item for item in state.effects if item.kind == "worker"))
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    ticket = state.tickets[0]
    run_id, root_session = UUID(str(ticket.assignments[-1].run_id)), ticket.session_id
    store = SqlAlchemyExecutionStore(h.original.sessions)
    lease = await store.claim(workspace_id=h.actor.workspace_id, run_id=run_id, owner="worker", lease_seconds=30)
    assert lease
    child_started, parent_ready, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    root_turns, child_cancelled, gateway_calls = [], [], []

    class ScriptedPi:
        def __init__(
            self,
            *,
            call_tool: Callable[[str, str, Json], Awaitable[Json]],
            session: PiSession,
        ) -> None:
            self.call, self.session = call_tool, session

        async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
            assert profile.model_id == "worker-model"
            if self.session.id != root_session:
                child_started.set()
                try:
                    await release.wait()
                    assert self.session.pull and self.session.checkpoint
                    pending = await self.session.pull()
                    assert len(pending) == 1 and "只核对原因" in pending[0][1]
                    # 即使夹具恶意调用，子 Agent 也不能自行提案，原生控制权仍归父会话/平台。
                    assert (await self.call("bad-control", "plan.propose", {}))["code"] == "CHILD_CONTROL_DENIED"
                    result = await self.call("child-read", "read", {})
                    assert result["ok"] is True
                    await self.session.checkpoint([{"role": "user", "content": "子会话记录"}], [pending[0][0]])
                    return "子会话已核对原因"
                except asyncio.CancelledError:
                    child_cancelled.append(True)
                    raise
            root_turns.append(payload.instruction)
            if len(root_turns) == 1:
                child = await self.call("delegate", "orchestrator_delegate", {"instruction": "核对错误原因"})
                assert child["state"] == "running"
                assert (
                    await self.call(
                        "steer-child",
                        "orchestrator_activity_message",
                        {"activity_id": child["activity_id"], "text": "只核对原因"},
                    )
                )["ok"] is True
                assert (
                    await self.call("premature", "orchestrator_report", {"state": "ready", "summary": "尚不能完成"})
                )["code"] == "BACKGROUND_WORK_RUNNING"
                parent_ready.set()
                if handoff:
                    await child_started.wait()
                    answer = await self.call(
                        "handoff", "orchestrator_report", {"state": "blocked", "summary": "需要人类处理"}
                    )
                    assert answer["ok"] is True
                return "当前回合空闲"
            assert "子会话已核对原因" in payload.instruction
            assert self.session.checkpoint
            await self.session.checkpoint([{"role": "user", "content": "主会话记录"}], self.session.initial_input_ids)
            assert (await self.call("ready", "orchestrator_report", {"state": "ready", "summary": "只读核对完成"}))[
                "ok"
            ] is True
            return "已结合子会话结果完成"

    async def gateway(identity: str, name: str, arguments: Json) -> Json:
        gateway_calls.append((identity, name))
        return {"ok": True}

    async def authorized() -> bool:
        return await store.authorize_token(h.actor, run_id, lease.fencing_token, lease.owner)

    runtime = CoordinatedRuntime(
        service,
        lease,
        runtime_factory=lambda session, call: ScriptedPi(session=session, call_tool=call),
        call_tool=gateway,
        authorize=authorized,
    )
    task = asyncio.create_task(
        runtime.run(
            RuntimeInput(
                "固定 Worker 指南",
                "处理目标",
                (ToolSpec("read", "读取", {}), ToolSpec("plan.propose", "申请审批", {}, "control")),
            ),
            ExecutionProfile("pi-agent-core@1.3.0", "deepseek", "worker-model", 1024, 30),
        )
    )
    try:
        await asyncio.wait_for(child_started.wait(), 10)
        if not handoff:
            # 等父指令真实入库再放行子会话，不能用固定 sleep 把数据库速度误当消息竞态。
            await asyncio.wait_for(parent_ready.wait(), 10)
            assert not task.done(), "子会话仍运行时必须保留父 Run 槽位"
            detail = await service.runs.access_metadata(h.actor, run_id)
            assert detail and detail.run.status == "RUNNING"
            release.set()
        text = await asyncio.wait_for(task, 10)
        state = await service.store.snapshot(h.actor.workspace_id, scope_id)
        assert state.tickets[0].id == ticket_id
        assert len(state.activities) == 1
        if handoff:
            assert child_cancelled and state.activities[0].state == "cancelled" and len(root_turns) == 1
        else:
            assert text == "已结合子会话结果完成" and state.activities[0].state == "completed"
            assert state.activities[0].read and len(root_turns) == 2 and len(gateway_calls) == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
