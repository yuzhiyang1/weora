"""人工停止后的显式恢复必须保留原 Task，并隔离旧执行者及自动唤醒。"""

from copy import deepcopy
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest

from tests.api.test_runs import runs as runs
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.coordination.test_engine import NOW, accept, message, setup
from tests.coordination.test_service import configure, decide, receive
from weora.modules.coordination.domain import CoordinationError, DeferredEffect, Json
from weora.modules.coordination.engine import key, later
from weora.modules.coordination.objectives import objective_inputs
from weora.modules.coordination.worker_sessions import WorkerBinding, WorkerSession
from weora.modules.runs.application.execution import RuntimeFailure
from weora.modules.runs.application.harness import OMP_RPC_RUNTIME, PI_RPC_RUNTIME
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore


def test_numbered_stopped_task_waits_for_explicit_main_resume_and_replays_once() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    ticket.task_id = "abcdef01-0000-4000-8000-000000000001"
    ticket.assignments[0].run_id = "12345678-0000-4000-8000-000000000001"
    engine.receive(replace(message("stop", addressed=True), text="#ABCDEF01 停止当前执行"))
    engine.route("stop", {"decision": "stop", "ticket_id": identity, "reason": "本人明确停止"})
    previous = ticket.assignments[-1]
    engine.now = lambda: later(NOW, 2)
    engine.receive(replace(message("chat", addressed=True), at=later(NOW, 1), text="#ABCDEF01 查一下当前状态"))
    engine.pass_due()
    assert ticket.ended_by == "human" and ticket.assignments == [previous]
    request = replace(message("resume", addressed=True), at=later(NOW, 2), text="#ABCDEF01 继续同一任务")
    assert engine.receive(request)
    assert ticket.ended_by == "human"
    result: Json = {"decision": "resume", "ticket_id": identity, "reason": "停止后本人明确恢复"}
    first = engine.route(request.id, result)
    assert engine.route(request.id, result).id == first.id
    assert not engine.receive(request)
    assert len(ticket.assignments) == 2 and ticket.task_id.startswith("abcdef01")
    assert ticket.ended_at is None and ticket.ended_by is None
    assert [source.id for _, source in objective_inputs(ticket, engine.r.triggers)] == ["m1", "resume"]
    assert previous.ended_at and previous.end_reason == "human_stopped"


@pytest.mark.parametrize("invalid", ["stale", "peer", "bot", "other", "revoked", "expired", "inferred"])
def test_resume_does_not_borrow_old_or_unauthorized_human_intent(invalid: str) -> None:
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    engine.receive(message("stop", addressed=True))
    engine.stop(identity, "stop")
    engine.now = lambda: later(NOW, 2)
    request = replace(message("resume", addressed=True), at=later(NOW, 1), text="继续同一任务")
    if invalid == "stale":
        request.at = NOW
    elif invalid == "peer":
        request.peer_evidence = True
    elif invalid == "bot":
        request.human = False
    elif invalid == "other":
        request.actor_id = "other"
    elif invalid == "revoked":
        state.triggers[0].authorized = False
    elif invalid == "expired":
        state.triggers[0].expires_at = later(NOW, 1)
    else:
        request.addressed = False
    engine.r.triggers.save(request)
    with pytest.raises(CoordinationError):
        engine.resume(identity, request.id)
    assert ticket.ended_by == "human" and len(ticket.assignments) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("numbered", [False, True])
@pytest.mark.parametrize("runtime_id", [PI_RPC_RUNTIME, OMP_RPC_RUNTIME])
async def test_feishu_main_route_stops_and_resumes_same_native_task_after_old_run_finishes(
    contextual: ContextRuns, numbered: bool, runtime_id: str,
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.actor.workspace_id
    await receive(h, coordinator, topic="omt_resume")
    ticket_id = await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    await coordinator.launch(workspace_id, scope_id, next(item for item in state.effects if item.kind == "worker"))
    ticket = (await coordinator.store.snapshot(workspace_id, scope_id)).tickets[0]
    previous = ticket.assignments[-1]
    run_id = UUID(str(previous.run_id))
    store = SqlAlchemyExecutionStore(h.original.sessions)
    lease = await store.claim(workspace_id=workspace_id, run_id=run_id, owner="before-stop", lease_seconds=30)
    assert lease
    session_id = key("worker-native-session", ticket.session_id, runtime_id)

    async def authorized() -> bool:
        return await store.authorize_token(h.actor, run_id, lease.fencing_token, lease.owner)

    journal = WorkerSession(coordinator, WorkerBinding(
        lease, scope_id, ticket_id, previous.id, session_id, (ticket.inputs[0].id,),
    ), authorized)
    port = await journal.open("test-native", runtime_id)
    assert port.checkpoint
    await port.checkpoint([{"role": "user", "content": "停止前已保留的原生会话定位"}], port.initial_input_ids)
    prefix = f"#{str(ticket.task_id)[:8]} " if numbered else ""
    await receive(h, coordinator, identity="om_stop", topic="omt_resume", addressed=True,
                  instruction=prefix + "停止当前执行")
    stop = await coordinator.tool(workspace_id, scope_id, "stop", "orchestrator_route", {"decisions": [{
        "trigger_id": "om_stop", "decision": "stop", "ticket_id": ticket_id, "reason": "本人明确停止",
    }]})
    assert cast(list[Json], stop["results"])[0]["ok"] is True, stop
    stopped = (await coordinator.store.snapshot(workspace_id, scope_id)).tickets[0]
    assert stopped.ended_by == "human"
    # 原生 Run 尚未真正结束时到达恢复请求：可以可靠接收，但不能并行派发。
    await receive(h, coordinator, identity="om_resume", topic="omt_resume", addressed=True,
                  instruction=prefix + "继续同一任务，保留停止前进度，完成原来的全部要求")
    request: Json = {"decisions": [{
        "trigger_id": "om_resume", "decision": "resume", "ticket_id": ticket_id,
        "reason": "原发起人停止后的新明确恢复请求",
    }]}
    resumed = await coordinator.tool(workspace_id, scope_id, "resume", "orchestrator_route", request)
    assert cast(list[Json], resumed["results"])[0]["ok"] is True, resumed
    replay = await coordinator.tool(workspace_id, scope_id, "resume-again", "orchestrator_route", request)
    assert replay == resumed
    current = await coordinator.store.snapshot(workspace_id, scope_id)
    active = current.tickets[0]
    assert len(current.tickets) == 1 and len(active.assignments) == 2
    assert active.task_id == ticket.task_id and active.session_id == ticket.session_id
    effect = next(item for item in current.effects if item.kind == "worker" and item.state == "pending")
    with pytest.raises(DeferredEffect, match="WAITING_FOR_STOP"):
        await coordinator.launch(workspace_id, scope_id, effect)
    with pytest.raises(RuntimeFailure):
        await journal.fresh()
    await coordinator.runs.cancel(actor=h.actor, workspace_id=workspace_id, run_id=run_id, request_id="stop-native")
    assert await store.finish(lease, status="CANCELLED", failure_code="CANCELLED")
    await coordinator.launch(workspace_id, scope_id, effect)
    current = await coordinator.store.snapshot(workspace_id, scope_id)
    active = current.tickets[0]
    latest = active.assignments[-1]
    assert latest.run_id and latest.run_id != str(run_id) and active.task_id == ticket.task_id
    detail = await coordinator.runs.access_metadata(h.actor, UUID(latest.run_id))
    assert detail and "请修复登录" in detail.task.instruction and "继续同一任务" in detail.task.instruction
    assert "停止当前执行" not in detail.task.instruction
    following = await store.claim(
        workspace_id=workspace_id, run_id=UUID(latest.run_id), owner="after-resume", lease_seconds=30,
    )
    assert following

    async def following_authorized() -> bool:
        return await store.authorize_token(h.actor, following.run.id, following.fencing_token, following.owner)

    follow = WorkerSession(coordinator, WorkerBinding(
        following, scope_id, ticket_id, latest.id, session_id, tuple(active.undelivered),
    ), following_authorized)
    continued = await follow.open("test-native", runtime_id)
    assert continued.id == port.id
    assert continued.messages == [{"role": "user", "content": "停止前已保留的原生会话定位"}]
    assert continued.native_history_epoch == port.native_history_epoch
    assert latest.run_id != str(run_id) and latest.id != previous.id


@pytest.mark.parametrize("control", ["stop", "resume"])
@pytest.mark.parametrize("number_kind", ["ambiguous", "malformed"])
def test_main_cannot_choose_task_for_ambiguous_or_malformed_number(control: str, number_kind: str) -> None:
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    ticket.task_id = "abcdef01-0000-4000-8000-000000000001"
    ticket.assignments[0].run_id = "12345678-0000-4000-8000-000000000001"
    if control == "resume":
        engine.receive(message("earlier-stop", addressed=True))
        engine.stop(identity, "earlier-stop")
    if number_kind == "ambiguous":
        other = deepcopy(ticket)
        other.id = "another-ticket"
        other.task_id = "abcdef01-0000-4000-8000-000000000002"
        engine.r.tickets.save(other)
    number = "ABCDEF01" if number_kind == "ambiguous" else "ABC"
    engine.now = lambda: later(NOW, 2)
    request = replace(message("control", addressed=True), at=later(NOW, 1), text=f"#{number} 明确{control}任务")
    assert engine.receive(request)
    before = deepcopy(ticket)
    with pytest.raises(CoordinationError, match="TASK_REFERENCE_ALREADY_HANDLED"):
        engine.route(request.id, {"decision": control, "ticket_id": identity, "reason": "Main自行选择了一个编号"})
    assert ticket == before and len(ticket.assignments) == 1
