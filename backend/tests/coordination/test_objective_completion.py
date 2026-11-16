"""完整用户目标、部分交付和并发纠偏的状态边界；不把脚本模型当真实模型验收。"""

from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from tests.api.test_runs import runs as runs
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.coordination.test_engine import NOW, accept, message, peer_message, setup
from tests.coordination.test_service import configure, decide, receive
from weora.modules.agents.application.ports import RuntimeSkillPackage
from weora.modules.coordination.domain import CoordinationError, Input, Json, Report, Session, State
from weora.modules.coordination.engine import Engine, key
from weora.modules.coordination.objectives import objective_inputs
from weora.modules.coordination.worker_reads import worker_facts
from weora.modules.coordination.worker_runtime import WORKER_DELIVERY_GUIDANCE, CoordinatedRuntime
from weora.modules.coordination.worker_sessions import WorkerBinding, WorkerSession
from weora.modules.runs.application.execution import RuntimeFailure, RuntimeInput, RuntimeSession
from weora.modules.runs.application.harness import OMP_RPC_RUNTIME, PI_RPC_RUNTIME, PI_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore


def test_partial_keeps_goal_open_and_ready_rejects_remaining_or_missing_objectives() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    assignment = ticket.assignments[-1]
    original = ticket.inputs[0].id
    report = Report("partial", "已读表格，尚未核对代码", NOW, remaining_work=("核对后端代码",))
    engine.report(identity, assignment.id, report)
    assert not ticket.ended_at and not assignment.ended_at and ticket.outcome is None
    with pytest.raises(CoordinationError, match="OBJECTIVE_WORK_REMAINING"):
        engine.report(identity, assignment.id, replace(
            report, state="ready", verified=True, completed_input_ids=(original,),
        ))
    with pytest.raises(CoordinationError, match="OBJECTIVE_COVERAGE_INCOMPLETE"):
        engine.report(identity, assignment.id, Report("ready", "仅确认部分", NOW, verified=True))
    with pytest.raises(CoordinationError, match="OBJECTIVE_COVERAGE_INCOMPLETE"):
        engine.report(identity, assignment.id, Report(
            "ready", "伪造其他任务引用", NOW, verified=True, completed_input_ids=(original, "another-task"),
        ))
    assert ticket.assignments[-1].report is report and ticket.ended_at is None
    assert "全部完成才能报告 ready" in WORKER_DELIVERY_GUIDANCE


def test_original_goal_and_owner_correction_remain_authoritative_over_main_scope() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    original = state.triggers[0].text
    # Main 的摘要允许不完整，但不能成为执行和验收的目标来源。
    ticket.mandate = replace(ticket.mandate, scope="只查一项，其他排除")
    engine.receive(replace(message("owner-followup"), text="全部规则都要核对，不要抽样"))
    engine.receive(replace(message("other-person", actor="other"), text="顺便删除数据"))
    goals = objective_inputs(ticket, engine.r.triggers)
    assert [trigger.text for _, trigger in goals] == [original, "全部规则都要核对，不要抽样"]
    facts = worker_facts(state, identity, {})
    detail = cast(dict[str, object], facts["ticket"])
    assert detail["objective_input_ids"] == [item.id for item, _ in goals]
    with pytest.raises(CoordinationError, match="OBJECTIVE_COVERAGE_INCOMPLETE"):
        engine.report(identity, ticket.assignments[-1].id, Report(
            "ready", "只完成旧目标", NOW, verified=True, completed_input_ids=(ticket.inputs[0].id,),
        ))
    assert ticket.ended_at is None


@pytest.mark.anyio
@pytest.mark.parametrize("late_correction", [False, True])
async def test_partial_and_completion_race_continue_same_task_idempotently(
    contextual: ContextRuns, late_correction: bool,
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.actor.workspace_id
    original = "请核对表格全部三个工作表，以及后端全科、Mini 模考算分和组卷代码；不能抽样。"
    await receive(h, coordinator, instruction=original)
    await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    await coordinator.launch(workspace_id, scope_id, next(x for x in state.effects if x.kind == "worker"))
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    ticket = state.tickets[0]
    task_id, run_id = ticket.task_id, UUID(str(ticket.assignments[-1].run_id))
    detail = await coordinator.runs.access_metadata(h.actor, run_id)
    assert detail and original in detail.task.instruction
    store = SqlAlchemyExecutionStore(h.original.sessions)
    lease = await store.claim(workspace_id=workspace_id, run_id=run_id, owner="goal-check", lease_seconds=30)
    assert lease
    proposed = Report(
        "ready" if late_correction else "partial", "已核对三张表，代码核对尚需继续", datetime.now(UTC).isoformat(),
        completed_input_ids=(ticket.inputs[0].id,), remaining_work=() if late_correction else ("核对全部代码",),
    )

    def save(engine: Engine, current: State) -> None:
        engine.mark_input(ticket.id, ticket.assignments[-1].id, ticket.inputs[0].id, read=True)
        current.checkpoints[f"report:{run_id}"] = asdict(proposed)
        next(x for x in current.effects if x.kind == "worker").state = "done"

    await coordinator.store.transact(workspace_id, scope_id, save)
    assert await store.finish(lease, status="SUCCEEDED", output=proposed.summary)
    if late_correction:
        await receive(
            h, coordinator, identity="om_correction", root="om_trigger", instruction="还要核实自动组卷抽题规则",
        )
    await coordinator.reconcile(workspace_id, scope_id)
    first = await coordinator.store.snapshot(workspace_id, scope_id)
    active = first.tickets[0]
    assert active.task_id == task_id and active.ended_at is None
    assert active.assignments[-1].report and active.assignments[-1].report.state == "partial"
    assert not active.assignments[-1].report.verified
    effect = next(x for x in first.effects if x.kind == "worker")
    assert effect.state == "pending" and effect.data["continuation_run_id"] == str(run_id)
    await coordinator.reconcile(workspace_id, scope_id)
    second = await coordinator.store.snapshot(workspace_id, scope_id)
    assert second.checkpoints == first.checkpoints
    await coordinator.launch(workspace_id, scope_id, effect)
    continued = (await coordinator.store.snapshot(workspace_id, scope_id)).tickets[0]
    assert continued.task_id == task_id and continued.assignments[-1].run_id != str(run_id)
    assert continued.session_id == ticket.session_id and len(continued.assignments) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("observed_progress", [False, True])
async def test_partial_continuation_requires_actual_progress_and_keeps_goal_open(
    contextual: ContextRuns, observed_progress: bool,
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.actor.workspace_id
    await receive(h, coordinator)
    await decide(h, coordinator, scope_id)
    store = SqlAlchemyExecutionStore(h.original.sessions)
    task_id = None
    for index in range(3):
        state = await coordinator.store.snapshot(workspace_id, scope_id)
        await coordinator.launch(workspace_id, scope_id, next(x for x in state.effects if x.kind == "worker"))
        state = await coordinator.store.snapshot(workspace_id, scope_id)
        ticket = state.tickets[0]
        if task_id is None:
            task_id = ticket.task_id
        assert ticket.task_id == task_id
        run_id = UUID(str(ticket.assignments[-1].run_id))
        lease = await store.claim(workspace_id=workspace_id, run_id=run_id, owner="no-progress", lease_seconds=30)
        assert lease
        # 改写摘要不能被当作新进展。
        report = Report(
            "partial", f"第 {index} 轮仍未核对完", datetime.now(UTC).isoformat(), remaining_work=("核对代码",),
        )
        await coordinator.store.transact(
            workspace_id, scope_id,
            lambda _, current, identity=run_id, snapshot=report: current.checkpoints.update(
                {f"report:{identity}": asdict(snapshot)},
            ),
        )
        if observed_progress:
            await store.native_progress(lease, "native.tool.read.finished")
        assert await store.finish(lease, status="SUCCEEDED", output=report.summary)
        await coordinator.reconcile(workspace_id, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    ticket = state.tickets[0]
    assert ticket.ended_at is None and ticket.outcome is None
    assert ticket.assignments[-1].report
    assert ticket.assignments[-1].report.state == ("partial" if observed_progress else "blocked")
    assert ticket.assignments[-1].report.remaining_work == ("核对代码",)


def test_first_result_has_the_same_delivery_fallback_as_retries() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    assignment = ticket.assignments[-1]
    assignment.run_id = "first-run"
    engine.mark_input(identity, assignment.id, ticket.inputs[0].id, read=True)
    engine.report(identity, assignment.id, Report(
        "ready", "全部目标已核实并列出证据", NOW, verified=True,
        completed_input_ids=(ticket.inputs[0].id,),
    ))
    assert not assignment.refresh_model
    engine.now = lambda: "2026-11-03T10:00:21+00:00"
    engine.relay_retried_results("https://console.example")
    engine.relay_retried_results("https://console.example")
    results = [reply for reply in state.replies if reply.purpose == "result"]
    assert len(results) == 1 and "全部目标已核实" in results[0].text


@pytest.mark.anyio
async def test_late_ack_is_withdrawn_when_task_has_already_finished(contextual: ContextRuns) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.actor.workspace_id
    await receive(h, coordinator)
    await decide(h, coordinator, scope_id, confirm=False)

    def finish(engine: Engine, current: State) -> None:
        ticket = current.tickets[0]
        ticket.inputs[0].delivered_chars = len(current.triggers[0].text)
        engine.mark_input(ticket.id, ticket.assignments[-1].id, ticket.inputs[0].id, read=True)
        engine.report(ticket.id, ticket.assignments[-1].id, Report(
            "ready", "全部完成", datetime.now(UTC).isoformat(), verified=True,
            completed_input_ids=(ticket.inputs[0].id,),
        ))

    await coordinator.store.transact(workspace_id, scope_id, finish)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    effect = next(item for item in state.effects if item.kind == "reply")
    assert not await coordinator.deliver(workspace_id, scope_id, effect)
    assert h.reader.replies == 0
    current = await coordinator.store.snapshot(workspace_id, scope_id)
    assert current.replies[0].state == "withdrawn"


@pytest.mark.anyio
@pytest.mark.parametrize("runtime_id", [PI_RPC_RUNTIME, OMP_RPC_RUNTIME])
async def test_native_worker_keeps_skill_archive_and_separates_legacy_session(
    contextual: ContextRuns, runtime_id: str,
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.actor.workspace_id
    await receive(h, coordinator)
    await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    await coordinator.launch(workspace_id, scope_id, next(x for x in state.effects if x.kind == "worker"))
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    ticket = state.tickets[0]
    run_id = UUID(str(ticket.assignments[-1].run_id))
    store = SqlAlchemyExecutionStore(h.original.sessions)
    lease = await store.claim(workspace_id=workspace_id, run_id=run_id, owner="native-goal", lease_seconds=30)
    assert lease
    legacy = Session(ticket.session_id, "worker", messages=[{"role": "user", "content": "旧 SDK 历史"}],
                     runtime_id=PI_RUNTIME)
    await coordinator.store.transact(workspace_id, scope_id, lambda _, current: current.sessions.append(legacy))
    packages = (RuntimeSkillPackage("example-skill", b"fixture-package-content"),)

    class NativePort:
        def __init__(self, session: RuntimeSession, call: Callable[[str, str, Json], Awaitable[Json]]) -> None:
            self.session, self.call = session, call

        async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
            assert payload.skill_packages == packages
            assert payload.explicit_feishu_sheet_read == "保留纠正标记"
            assert self.session.id == key("worker-native-session", ticket.session_id, runtime_id)
            assert self.session.messages == []
            assert self.session.checkpoint
            await self.session.checkpoint([], self.session.initial_input_ids)
            rejected = await self.call("bad-ready", "orchestrator_report", {
                "state": "ready", "summary": "只核实部分", "completed_input_ids": [ticket.inputs[0].id],
                "remaining_work": ["完整核对代码"],
            })
            assert rejected["code"] == "OBJECTIVE_WORK_REMAINING"
            saved = await coordinator.store.snapshot(workspace_id, scope_id)
            assert f"report:{run_id}" not in saved.checkpoints
            assert (await self.call("partial", "orchestrator_report", {
                "state": "partial", "summary": "已保存已核实部分", "remaining_work": ["完整核对代码"],
            }))["ok"]
            return "部分结果，目标仍未完成"

    async def gateway(identity: str, name: str, arguments: Json) -> Json:
        raise AssertionError("本夹具不调用业务工具")

    async def authorize() -> bool:
        return await store.authorize_token(h.actor, run_id, lease.fencing_token, lease.owner)

    runtime = CoordinatedRuntime(
        coordinator, lease, runtime_factory=NativePort, call_tool=gateway, authorize=authorize,
    )
    await runtime.run(
        RuntimeInput("系统", "目标", explicit_feishu_sheet_read="保留纠正标记", skill_packages=packages),
        ExecutionProfile(runtime_id, "deepseek", "test-model", 1024, 30),
    )
    current = await coordinator.store.snapshot(workspace_id, scope_id)
    assert next(item for item in current.sessions if item.id == ticket.session_id).messages == legacy.messages
    native = next(item for item in current.sessions if item.id != ticket.session_id)
    assert native.runtime_id == runtime_id and ticket.inputs[0].id in native.read


@pytest.mark.anyio
@pytest.mark.parametrize("runtime_id", [PI_RPC_RUNTIME, OMP_RPC_RUNTIME])
async def test_native_main_loads_full_skill_package_and_uses_its_own_session(
    contextual: ContextRuns, runtime_id: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.actor.workspace_id
    await receive(h, coordinator)
    effects = await coordinator.pending_effects(workspace_id, scope_id)
    main = next(item for item in effects if item.kind == "main")
    effect = await coordinator.store.claim(workspace_id, scope_id, main.id, "native-main")
    assert effect
    packages = (RuntimeSkillPackage("complete-skill", b"archive-with-script-and-references"),)
    load = AsyncMock(return_value=("Agent 固定提示", packages))
    monkeypatch.setattr(coordinator.agents, "runtime_packages", load)
    monkeypatch.setattr(
        coordinator.agents, "build_runtime_context", AsyncMock(side_effect=AssertionError("不可文本化")),
    )

    class NativeMainPort:
        def __init__(self, session: RuntimeSession, call: Callable[[str, str, Json], Awaitable[Json]]) -> None:
            self.session = session

        async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
            assert self.session.id == key("main-session", scope_id, runtime_id)
            assert payload.skill_packages == packages and "Agent 固定提示" in payload.system_prompt
            assert self.session.checkpoint
            await self.session.checkpoint([], self.session.initial_input_ids)
            return "本轮协调完成"

    await coordinator.main(
        workspace_id, scope_id, effect, NativeMainPort,
        ExecutionProfile(runtime_id, "deepseek", "test-main", 1024, 30),
    )
    load.assert_awaited_once()
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    journal = next(item for item in state.sessions if item.role == "main")
    assert journal.runtime_id == runtime_id and journal.read


def test_read_receipt_and_last_page_cannot_replace_reading_full_goal() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    source = state.triggers[0]
    source.text = "完整用户要求" * 2000
    item = ticket.inputs[0]
    item.delivered_chars = 4000
    engine.mark_input(identity, ticket.assignments[-1].id, item.id, read=True)
    ready = Report("ready", "声称全部完成", NOW, verified=True, completed_input_ids=(item.id,))
    assert item.read_at
    with pytest.raises(CoordinationError, match="OBJECTIVE_INPUT_NOT_READ"):
        engine.report(identity, ticket.assignments[-1].id, ready)
    # 直接读取 EOF 仍有中间缺口，不能靠下一页为空冒充全文读取。
    worker_facts(state, identity, {"resource": "input", "id": item.id, "offset": 8000})
    with pytest.raises(CoordinationError, match="OBJECTIVE_INPUT_NOT_READ"):
        engine.report(identity, ticket.assignments[-1].id, ready)
    worker_facts(state, identity, {"resource": "input", "id": item.id, "offset": 4000})
    engine.report(identity, ticket.assignments[-1].id, ready)
    assert ticket.outcome == "ready"


def test_review_merge_cannot_finish_a_partial_goal() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    assignment = ticket.assignments[-1]
    engine.mark_input(identity, assignment.id, ticket.inputs[0].id, read=True)
    engine.bind_review(identity, "code:review", "run", "review")
    engine.report(identity, assignment.id, Report(
        "partial", "已有 PR，剩余目标未完成", NOW, remaining_work=("补齐剩余测试",),
    ))
    engine.observe_review(identity, "code:review", "merged", "2026-11-03T10:00:10+00:00",
                          closed_at="2026-11-03T10:00:05+00:00")
    assert ticket.ended_at is None and ticket.outcome is None


def test_peer_readonly_review_restores_existing_owner_authority_without_new_approval() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    original = ticket.mandate
    engine.receive(peer_message())
    assert Engine.peer_evidence_only(ticket)
    peer = ticket.inputs[-1]
    engine.mark_input(identity, ticket.assignments[-1].id, peer.id, read=True)
    engine.report(identity, ticket.assignments[-1].id, Report(
        "progress", "已只读核实同伴线索，继续原始修复目标", NOW, answers="peer",
    ))
    assert peer.peer_evidence_checked and not Engine.peer_evidence_only(ticket)
    assert ticket.mandate == original and ticket.attention is None and ticket.ended_at is None
    # 新的 peer 消息仍需独立只读核实，不能继承上一条处理标记。
    engine.receive(peer_message("another-peer"))
    assert Engine.peer_evidence_only(ticket)


@pytest.mark.anyio
@pytest.mark.parametrize("failure_code", ["SESSION_POLICY_CHANGED", "SESSION_HISTORY_REVOKED"])
async def test_native_session_policy_or_history_change_stops_and_continues_same_task(
    contextual: ContextRuns, failure_code: str,
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.actor.workspace_id
    await receive(h, coordinator)
    await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    await coordinator.launch(workspace_id, scope_id, next(x for x in state.effects if x.kind == "worker"))
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    ticket = state.tickets[0]
    run_id = UUID(str(ticket.assignments[-1].run_id))
    store = SqlAlchemyExecutionStore(h.original.sessions)
    lease = await store.claim(workspace_id=workspace_id, run_id=run_id, owner="policy-change", lease_seconds=30)
    assert lease
    session_id = key("worker-native-session", ticket.session_id, PI_RPC_RUNTIME)

    async def authorize() -> bool:
        return await store.authorize_token(h.actor, run_id, lease.fencing_token, lease.owner)

    journal = WorkerSession(coordinator, WorkerBinding(
        lease, scope_id, ticket.id, ticket.assignments[-1].id, session_id, (ticket.inputs[0].id,),
    ), authorize)
    port = await journal.open("test-model", PI_RPC_RUNTIME)
    assert not port.read_only and port.native_history_epoch is None

    def change(_: Engine, current: State) -> None:
        if failure_code == "SESSION_HISTORY_REVOKED":
            native = next(item for item in current.sessions if item.id == session_id)
            native.native_history_epoch += 1
            native.messages = []
        else:
            # 这里只模拟已验证的 peer 投影；来源授权入口另由 managed_worker 的成员测试覆盖。
            current.tickets[0].inputs.append(Input(
                "verified-peer", "trigger", datetime.now(UTC).isoformat(),
                text="同伴只读复现线索", human=True, peer_evidence=True,
            ))

    await coordinator.store.transact(workspace_id, scope_id, change)
    with pytest.raises(RuntimeFailure, match=failure_code):
        await journal.fresh()
    assert await store.finish(lease, status="FAILED", failure_code=failure_code)
    await coordinator.reconcile(workspace_id, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    active = state.tickets[0]
    assert active.mandate == ticket.mandate and active.task_id == ticket.task_id
    assert active.assignments[-1].report and active.assignments[-1].report.state == "partial"
    assert active.attention is None and active.ended_at is None
    await coordinator.launch(workspace_id, scope_id, next(x for x in state.effects if x.kind == "worker"))
    active = (await coordinator.store.snapshot(workspace_id, scope_id)).tickets[0]
    assert active.task_id == ticket.task_id and active.assignments[-1].run_id != str(run_id)


@pytest.mark.anyio
async def test_long_original_inputs_keep_full_references_without_exceeding_run_instruction_limit(
    contextual: ContextRuns,
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.actor.workspace_id
    original = ('"\\' * 6000)
    await receive(h, coordinator, instruction=original)
    await decide(h, coordinator, scope_id)
    await receive(h, coordinator, identity="om_long_correction", root="om_trigger", instruction=original)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    await coordinator.launch(workspace_id, scope_id, next(x for x in state.effects if x.kind == "worker"))
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    ticket = state.tickets[0]
    detail = await coordinator.runs.access_metadata(h.actor, UUID(str(ticket.assignments[-1].run_id)))
    assert detail and len(detail.task.instruction) <= 16000
    for item in ticket.inputs:
        assert item.id in detail.task.instruction
        assert 0 < item.delivered_chars < 4000
    assert "next_offset" in detail.task.instruction and "resource=input" in detail.task.instruction
    # 启动仅交付预览，完整目标仍存于来源，不能因为入口限长就变成缩减后的任务。
    assert state.triggers[0].text == original and ticket.ended_at is None
