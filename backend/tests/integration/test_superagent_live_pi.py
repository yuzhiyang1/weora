"""真实 Pi/DeepSeek 协调验收；业务库隔离，渠道发送明确使用 Fake，不冒充飞书联调。"""

import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from tests.api.test_runs import runs as runs
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.coordination.test_service import configure, receive
from weora.bootstrap.run_profile import execution_profile
from weora.bootstrap.settings import AppSettings
from weora.modules.context.domain import Json, Message, MessagePage
from weora.modules.context.service import timestamp
from weora.modules.coordination.domain import Rule
from weora.modules.coordination.service import CoordinationService
from weora.modules.runs.infrastructure.pi_runtime import PiSession, PiTextRuntime

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def main_turn(coordinator: CoordinationService, workspace_id: UUID, scope_id: str) -> None:
    settings = AppSettings()
    profile = execution_profile(settings)
    assert profile and settings.deepseek_api_key and settings.pi_runtime_entry
    await coordinator.store.transact(workspace_id, scope_id, lambda engine, _: engine.pass_due())
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    pending = next(item for item in state.effects if item.kind == "main")
    effect = await coordinator.store.claim(
        workspace_id, scope_id, pending.id, "real-main", seconds=int(profile.wall_timeout_seconds) + 30
    )
    assert effect

    api_key = settings.deepseek_api_key.get_secret_value()

    def runtime(session: PiSession, invoke: Callable[[str, str, Json], Awaitable[Json]]) -> PiTextRuntime:
        return PiTextRuntime(
            node_executable=settings.pi_node_executable,
            entry=Path(settings.pi_runtime_entry or ""),
            api_key=api_key,
            session=session,
            call_tool=invoke,
        )

    await coordinator.main(workspace_id, scope_id, effect, runtime, profile)
    await coordinator.store.settle(workspace_id, scope_id, effect)


async def test_real_main_observe_auto_worker_and_result(
    contextual: ContextRuns, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("WEORA_TEST_LIVE_PI") != "1":
        pytest.skip("真实模型验收需要显式 WEORA_TEST_LIVE_PI=1")
    h = contextual
    coordinator, scope_id = await configure(h, "observe")
    workspace_id = h.context_actor.workspace_id
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    config = replace(
        state.config,
        rules=(Rule("code", "实现函数", "当人类明确要求实现一个目标和接口已确定的函数时接单；闲聊不接单。"),),
    )
    await coordinator.save(h.context_actor, config, expected_revision=state.config.revision)
    await receive(
        h,
        coordinator,
        identity="om_observe",
        instruction=(
            "请实现 Python 函数 normalize_code(raw: str) -> str，去掉全部空格并转成大写。"
            "请直接回复代码和自测样例，无需落盘。接口与行为已经确定。"
        ),
    )
    await main_turn(coordinator, workspace_id, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    assert not state.tickets
    assert any(item.audit.get("would_accept") for item in state.decisions), [
        (item.reason, item.kind) for item in state.decisions
    ]
    await coordinator.save(h.context_actor, replace(state.config, mode="auto"), expected_revision=state.config.revision)
    await receive(
        h,
        coordinator,
        identity="om_auto",
        instruction=(
            "请实现 Python 函数 normalize_name(raw: str) -> str，去掉首尾空格并转成小写。"
            "请直接回复代码和自测样例，无需落盘。接口与行为已经确定。"
        ),
    )
    await main_turn(coordinator, workspace_id, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    assert len(state.tickets) == 1
    settings = AppSettings()
    coordinator.runs._execution_profile = execution_profile(settings)
    worker = next(item for item in state.effects if item.kind == "worker")
    await coordinator.launch(workspace_id, scope_id, worker)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    ticket = state.tickets[0]
    origin = next(item for item in state.triggers if item.id == ticket.origin_id)
    h.reader.single = Message(origin.id, "oc_project", None, "ou_user", timestamp(origin.at), origin.text)
    h.reader.pages = {None: MessagePage((h.reader.single,))}
    from weora.bootstrap import worker as bootstrap_worker

    monkeypatch.setattr(bootstrap_worker, "create_app", lambda *args, **kwargs: h.app)
    assert await bootstrap_worker._execute(workspace_id, UUID(str(ticket.assignments[0].run_id)))
    await coordinator.reconcile(workspace_id, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    assert state.tickets[0].outcome == "ready", state.tickets[0].assignments[-1].report
    await main_turn(coordinator, workspace_id, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    assert any(item.purpose == "result" for item in state.replies)
    for effect in state.effects:
        if effect.kind == "reply":
            await coordinator.deliver(workspace_id, scope_id, effect)
    assert h.reader.replies >= 1
    first_task, first_run = ticket.task_id, ticket.assignments[0].run_id
    original_receipts = {item.id: item.delivered_to for item in state.tickets[0].inputs if item.read_at}
    await receive(
        h,
        coordinator,
        identity="om_auto_followup",
        root="om_auto",
        instruction="沿用刚才的规范化规则，新增可选参数 prefix: str = ''，在结果前原样拼接 prefix。请回复更新代码。",
    )
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    worker = next(item for item in state.effects if item.kind == "worker")
    await coordinator.launch(workspace_id, scope_id, worker)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    latest = state.tickets[0]
    assert latest.task_id == first_task and latest.assignments[-1].run_id != first_run
    assert await bootstrap_worker._execute(workspace_id, UUID(str(latest.assignments[-1].run_id)))
    await coordinator.reconcile(workspace_id, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    assert state.tickets[0].outcome == "ready", state.tickets[0].assignments[-1].report
    assert "prefix" in str(state.tickets[0].assignments[-1].report)
    assert all(
        item.delivered_to == original_receipts[item.id]
        for item in state.tickets[0].inputs
        if item.id in original_receipts
    )
