"""原生飞书续接遇到长话题时保留原生历史，并独立复核历史来源权限。"""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import update

from tests.api.test_runs import runs as runs
from tests.api.test_task_continuations import finish
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.context.test_task_threads import send, setup
from tests.coordination.test_service import configure, decide, receive
from weora.modules.context.domain import ContextError, Json, Message, MessagePage
from weora.modules.coordination.domain import DeferredEffect
from weora.modules.runs.application.execution import RuntimeFailure
from weora.modules.runs.application.harness import OMP_RPC_RUNTIME, PI_RPC_RUNTIME
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.models import RunModel
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork


async def select_runtime(h: ContextRuns, run_id: UUID, runtime_id: str) -> None:
    """夹具仅固定执行引擎，消息接收、Task 续接、上下文与授权仍走真实应用。"""
    async with h.original.sessions() as session, session.begin():
        await session.execute(update(RunModel).where(RunModel.id == run_id).values(execution_profile={
            "runtime_id": runtime_id, "provider": "deepseek", "model_id": "test-native-model",
            "timeout_seconds": 30, "max_tokens": None,
        }))


@pytest.mark.anyio
@pytest.mark.parametrize("runtime_id", [PI_RPC_RUNTIME, OMP_RPC_RUNTIME])
@pytest.mark.parametrize("problem", [
    None, "has_more", "quote_unavailable", "expired_history", "initial",
    "cancelled_history", "failed_history", "unstarted_history", "different_runtime",
])
async def test_native_long_thread_retains_authorized_session_without_sdk_summary(
    contextual: ContextRuns, runtime_id: str, problem: str | None,
) -> None:
    h = contextual
    channels = await setup(h)
    service = cast(RunService, h.app.state.run_service)
    access = ModulesExecutionAccess(
        identity=h.app.state.identity_service, agents=h.app.state.agent_registry_service,
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(h.original.sessions),
        context=h.context, executions=h.app.state.execution_store,
    )
    root = await send(h, channels, "om_native_root", "记住任务代号青竹")
    root_id = UUID(str(root["run_id"]))
    await select_runtime(h, root_id, runtime_id)
    initial = await service.access_metadata(h.actor, root_id)
    assert initial is not None
    target = initial
    if problem != "initial":
        # 根轮次先真实装配授权快照，不能只根据相同 Task ID 就放宽缺证据检查。
        await access.load_input(initial.run)
        if problem in {"cancelled_history", "failed_history", "unstarted_history"}:
            execution = h.app.state.execution_store
            lease = await execution.claim(
                workspace_id=h.actor.workspace_id, run_id=root_id, owner="native-history-test", lease_seconds=30,
            )
            assert lease
            await execution.native_progress(lease, "native.started")
            if problem != "unstarted_history":
                await execution.native_progress(lease, "native.tool.read.finished")
            if problem != "failed_history":
                # 取消必须先经过应用用例登记请求，不能在夹具直接伪造执行终态。
                await service.cancel(
                    actor=h.actor, workspace_id=h.actor.workspace_id, run_id=root_id,
                    request_id="native-history-cancel",
                )
            assert await execution.finish(
                lease, status="FAILED" if problem == "failed_history" else "CANCELLED",
            )
        else:
            await finish(h.original, root_id, "旧 SDK 摘要：已记住青竹，不得重复注入原生输入")
        followup = await send(h, channels, "om_native_followup", "继续原任务并回答代号", quote="om_native_root")
        follow_id = UUID(str(followup["run_id"]))
        selected = (OMP_RPC_RUNTIME if runtime_id == PI_RPC_RUNTIME else PI_RPC_RUNTIME) if (
            problem == "different_runtime"
        ) else runtime_id
        await select_runtime(h, follow_id, selected)
        target = await service.access_metadata(h.actor, follow_id)
        assert target is not None and target.task.id == initial.task.id
    stamp = datetime.now(UTC) - timedelta(minutes=1)
    body = "长讨论" * 400 if problem == "quote_unavailable" else "讨论"
    # 真正超过默认十条窗口，保留 partial 与缺失原因，不伪造 complete 快照。
    h.reader.pages[None] = MessagePage(tuple(
        Message(f"om_native_history_{i}", "oc_project", "omt_task", "ou_user", stamp, f"{body} {i}")
        for i in range(15)
    ), "next-page" if problem == "has_more" else None)
    h.reader.single = None if problem == "quote_unavailable" else Message(
        "om_native_root", "oc_project", "omt_task", "ou_user", stamp, "任务初始请求",
    )
    if problem == "expired_history":
        rows = await h.context.store.list(h.actor.workspace_id, "snapshot")
        old = next(row for row in rows if row["run_id"] == str(root_id))
        await h.context.store.change(
            h.actor.workspace_id, "snapshot", str(old["snapshot_id"]),
            lambda row: dict(row or {}, expires_at=(stamp - timedelta(days=8)).isoformat()),
        )
    if problem in {"initial", "quote_unavailable", "expired_history", "unstarted_history", "different_runtime"}:
        for _ in range(2):
            with pytest.raises(RuntimeFailure, match="WAITING_CONTEXT"):
                await access.load_input(target.run)
        return
    payload = await access.load_input(target.run)
    assert "外部话题仅部分可见" in payload.instruction
    assert "原生会话恢复完整历史" in payload.instruction
    assert "旧 SDK 摘要" not in payload.instruction
    assert "任何写入仍须本 Run 单独审批" not in payload.instruction
    snapshots = [row for row in await h.context.store.list(h.actor.workspace_id, "snapshot")
                 if row["run_id"] == str(target.run.id)]
    assert snapshots and snapshots[0]["status"] == "partial"
    assert len(cast(list[Json], snapshots[0]["messages"])) <= 10
    assert "budget_exceeded" in str(cast(Json, snapshots[0]["coverage"])["reason"])
    assert "旧 SDK 摘要" not in (await access.load_input(target.run)).instruction
    await h.context.set_grant(h.context_actor, h.source_id, h.actor.user_id, False)
    with pytest.raises((RuntimeFailure, ContextError)):
        await access.load_input(target.run)

@pytest.mark.anyio
@pytest.mark.parametrize("runtime_id", [PI_RPC_RUNTIME, OMP_RPC_RUNTIME])
@pytest.mark.parametrize("waiting_state", ["WAITING_CONTEXT", "RECOVERY_REQUIRED"])
async def test_feishu_explicit_retry_replaces_only_context_wait(
    contextual: ContextRuns, runtime_id: str, waiting_state: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通过飞书收件和 Main 工具重试，核验旧等待不占槽、未知写入仍不重放。"""
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.actor.workspace_id
    await receive(h, coordinator, topic="omt_retry_context")
    ticket_id = await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    initial_effect = next(effect for effect in state.effects if effect.kind == "worker")
    await coordinator.launch(workspace_id, scope_id, initial_effect)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    original_ticket = state.tickets[0]
    original_run = UUID(str(original_ticket.assignments[-1].run_id))
    await select_runtime(h, original_run, runtime_id)
    detail = await coordinator.runs.access_metadata(h.actor, original_run)
    assert detail and detail.run.execution_profile
    monkeypatch.setattr(coordinator.runs, "_profile_provider", None)
    monkeypatch.setattr(coordinator.runs, "_execution_profile", detail.run.execution_profile)
    store = h.app.state.execution_store
    lease = await store.claim(workspace_id=workspace_id, run_id=original_run, owner="context-wait", lease_seconds=30)
    assert lease
    assert await store.transition(h.actor, original_run, waiting_state, expected_lease=lease)
    await coordinator.reconcile(workspace_id, scope_id)
    await receive(
        h, coordinator, identity="om_human_retry_wait", topic="omt_retry_context", addressed=True,
        instruction="请重试原任务，继续处理尚未完成的原要求",
    )
    args: Json = {"ticket_id": ticket_id, "trigger_id": "om_human_retry_wait"}
    result = await coordinator.tool(workspace_id, scope_id, "human-retry", "orchestrator_ticket_retry", args)
    assert result["ok"] is True, result
    current = await coordinator.store.snapshot(workspace_id, scope_id)
    effect = next(
        item for item in current.effects
        if item.kind == "worker" and item.data["assignment_id"] == current.tickets[0].assignments[-1].id
    )
    if waiting_state == "RECOVERY_REQUIRED":
        with pytest.raises(DeferredEffect, match="TASK_INPUT_WAIT"):
            await coordinator.launch(workspace_id, scope_id, effect)
        held = await coordinator.runs.access_metadata(h.actor, original_run)
        assert held and held.run.status == "RECOVERY_REQUIRED"
        return
    await coordinator.launch(workspace_id, scope_id, effect)
    finished = await coordinator.store.snapshot(workspace_id, scope_id)
    ticket = finished.tickets[0]
    run_id = UUID(str(ticket.assignments[-1].run_id))
    old = await coordinator.runs.access_metadata(h.actor, original_run)
    new = await coordinator.runs.access_metadata(h.actor, run_id)
    assert old and old.run.status == "CANCELLED"
    assert new and new.run.status == "QUEUED" and run_id != original_run
    assert new.run.execution_profile and new.run.execution_profile.runtime_id == runtime_id
    assert ticket.task_id == original_ticket.task_id == str(new.task.id)
    assert ticket.session_id == original_ticket.session_id
    assert "请修复登录" in new.task.instruction
    assert await coordinator.tool(workspace_id, scope_id, "human-retry", "orchestrator_ticket_retry", args) == result
