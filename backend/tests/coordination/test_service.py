"""真实 SQL、HTTP、Inbox 和 Runs 集成；飞书和模型均使用明确的夹具。"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from tests.api.test_runs import runs as runs
from tests.context.test_context import inbound
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.coordination.test_engine import result
from weora.bootstrap import worker as worker_bootstrap
from weora.bootstrap.coordination import dispatch
from weora.bootstrap.worker import _pump_context
from weora.modules.computers.openbot import ComputerError, OpenBot
from weora.modules.computers.policy import ComputerMode
from weora.modules.computers.tools import register_computer_tools
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextError, Json, Message, MessagePage
from weora.modules.coordination.domain import CoordinationError, DeferredEffect, Report, State
from weora.modules.coordination.engine import Engine
from weora.modules.coordination.polling import compensate
from weora.modules.coordination.service import CoordinationService
from weora.modules.coordination.store import CoordinationScope, SqlCoordinationStore
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.module_access import ContextModuleAccess, GatewayModuleAccess
from weora.modules.tools.application.gateway import ToolGateway, ToolScope


def after_native_commit[**P, T](
    original: Callable[P, Awaitable[T]], after_commit: Callable[[], Awaitable[None]]
) -> Callable[P, Awaitable[T]]:
    """在真实原生提交后注入中断，保留原方法签名与返回类型，覆盖跨模块提交窗口。"""

    async def interrupted(*args: P.args, **kwargs: P.kwargs) -> T:
        result = await original(*args, **kwargs)
        await after_commit()
        return result

    return interrupted


async def configure(h: ContextRuns, mode: str = "auto") -> tuple[CoordinationService, str]:
    await h.context.probe(h.context_actor, h.connection_id)
    await h.context.bind_identity(
        h.context_actor, connection_id=h.connection_id, open_id="ou_user", user_id=h.context_actor.user_id
    )
    await FeishuChannels(h.context).save_route(
        h.context_actor, source_id=h.source_id, agent_id=UUID(h.original.agent_id), agent_version="0.1.0"
    )
    payload = {
        "source_id": str(h.source_id),
        "route_key": f"{h.connection_id}:oc_project:-",
        "main_agent_id": h.original.agent_id,
        "main_agent_version": "0.1.0",
        "worker_agent_id": h.original.agent_id,
        "worker_agent_version": "0.1.0",
        "allowed_actors": ["ou_user"],
        "rules": [{"id": "fix", "name": "修复", "instruction": "明确修复"}],
        "expected_revision": 0,
        "mode": mode,
        "debounce_ms": 0,
    }
    response = await h.client.put(h.base + "/coordination/config", json=payload, headers=h.headers)
    assert response.status_code == 200, response.text
    scope_id = response.json()["config"]["id"]
    conflict = await h.client.put(h.base + "/coordination/config", json=payload, headers=h.headers)
    assert conflict.status_code == 409
    return cast(CoordinationService, h.app.state.coordination_service), scope_id


async def receive(
    h: ContextRuns,
    coordinator: CoordinationService,
    *,
    identity: str = "om_trigger",
    root: str | None = None,
    instruction: str = "请修复登录，沿用现有交互",
    topic: str | None = None,
) -> None:
    payload = inbound(identity, identity)
    raw = cast(Json, cast(Json, payload["event"])["message"])
    raw["mentions"] = []
    raw["content"] = json.dumps({"text": instruction}, ensure_ascii=False)
    if root:
        raw["root_id"] = root
    if topic:
        raw["thread_id"] = topic
    receipt = await FeishuChannels(h.context, coordinator.candidate_admission).receive(
        h.context_actor.workspace_id, h.connection_id, payload
    )
    assert receipt["status"] == "persisted"
    await _pump_context(h.app)


async def decide(h: ContextRuns, coordinator: CoordinationService, scope_id: str) -> str:
    workspace_id = h.context_actor.workspace_id
    evaluations = await coordinator.store.transact(workspace_id, scope_id, lambda engine, _: engine.pass_due())
    evaluation = evaluations[0]
    read = await coordinator.tool(workspace_id, scope_id, "read", "orchestrator_eval_read", {"eval_id": evaluation.id})
    assert read["ok"] is True, read
    value = dict(result(), eval_id=evaluation.id)
    decision = await coordinator.tool(workspace_id, scope_id, "decide", "orchestrator_eval_decide", value)
    assert decision["ok"] is True, decision
    return str(cast(Json, decision["decision"]).get("ticket_id"))


@pytest.mark.anyio
async def test_batch_invalid_parameters_refuse_only_that_item(contextual: ContextRuns) -> None:
    """对应上游 tools.spec 的 FIELDS 用例：参数错误与事务错误一样逐项隔离。"""
    h = contextual
    coordinator, scope_id = await configure(h)
    await receive(h, coordinator)

    def addressed(_: Engine, current: State) -> None:
        current.triggers[0].addressed = True

    await coordinator.store.transact(h.context_actor.workspace_id, scope_id, addressed)
    value = await coordinator.tool(
        h.context_actor.workspace_id,
        scope_id,
        "batch",
        "orchestrator_route",
        {
            "decisions": [
                {"trigger_id": "om_trigger", "decision": "attach", "reason": ""},
                {"trigger_id": "om_trigger", "decision": "ignore", "reason": "只是询问信息"},
            ]
        },
    )
    assert value["ok"] is True
    results = cast(list[Json], value["results"])
    assert results[0] == {"ok": False, "code": "INVALID_TOOL_PARAMETERS"}
    assert results[1]["ok"] is True
    assert len((await coordinator.store.snapshot(h.context_actor.workspace_id, scope_id)).decisions) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("progress_report", [True, False])
async def test_successful_model_turn_without_ready_keeps_ticket_waiting(
    contextual: ContextRuns,
    progress_report: bool,
) -> None:
    """无报告或仅有进度时，正常返回只说明本轮空闲，不说明持续目标完成。"""
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.context_actor.workspace_id
    await receive(h, coordinator)
    await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    await coordinator.launch(workspace_id, scope_id, next(item for item in state.effects if item.kind == "worker"))
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    run_id = UUID(str(state.tickets[0].assignments[0].run_id))
    if progress_report:
        await coordinator.store.transact(
            workspace_id,
            scope_id,
            lambda _, current: current.checkpoints.update(
                {f"report:{run_id}": asdict(Report("progress", "仍在进行", datetime.now(UTC).isoformat()))}
            ),
        )
    store = SqlAlchemyExecutionStore(h.original.sessions)
    lease = await store.claim(workspace_id=workspace_id, run_id=run_id, owner="test", lease_seconds=30)
    assert lease
    assert await store.finish(lease, status="SUCCEEDED", output="当前结果" * 6000)
    await coordinator.reconcile(workspace_id, scope_id)
    first = await coordinator.store.snapshot(workspace_id, scope_id)
    ticket = first.tickets[0]
    assert not ticket.ended_at
    assert ticket.assignments[-1].report and ticket.assignments[-1].report.state == "waiting"
    assert "完整结果见 Weora Run" in ticket.assignments[-1].report.summary
    await coordinator.reconcile(workspace_id, scope_id)
    assert len((await coordinator.store.snapshot(workspace_id, scope_id)).notices) == len(first.notices)


@pytest.mark.anyio
@pytest.mark.parametrize("root_present", [True, False])
async def test_unmentioned_inbox_decision_worker_run_report_reply_and_task_continuation(
    contextual: ContextRuns,
    root_present: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.context_actor.workspace_id
    await receive(h, coordinator, topic="omt_shared")
    inbox = (await h.context.store.list(workspace_id, "inbox"))[0]
    assert inbox["status"] == "COORDINATING" and "run_id" not in inbox
    ticket_id = await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    worker_effect = next(item for item in state.effects if item.kind == "worker")
    await coordinator.launch(workspace_id, scope_id, worker_effect)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    ticket = state.tickets[0]
    assert ticket.id == ticket_id and ticket.task_id and ticket.assignments[0].run_id
    first_id = UUID(ticket.assignments[0].run_id)
    store = SqlAlchemyExecutionStore(h.original.sessions)
    lease = await store.claim(workspace_id=workspace_id, run_id=first_id, owner="test", lease_seconds=30)
    assert lease
    delivery = cast(DeliveryService, h.app.state.delivery_service)
    screenshot = await delivery.record_computer_screenshot(
        h.actor,
        first_id,
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7uoAAAAASUVORK5CYII=",
    )
    for item in ticket.inputs:

        def mark_read(engine: Engine, _: State, identity: str = item.id) -> None:
            engine.mark_input(ticket.id, ticket.assignments[0].id, identity, read=True)

        await coordinator.store.transact(
            workspace_id,
            scope_id,
            mark_read,
        )
    # 完成报告与模型回合成功是不同事实；只有显式 ready 可以结束持续 Ticket。
    await coordinator.store.transact(
        workspace_id,
        scope_id,
        lambda _, current: current.checkpoints.update(
            {f"report:{first_id}": asdict(Report("ready", "只读验证通过", datetime.now(UTC).isoformat()))}
        ),
    )
    assert await store.finish(lease, status="SUCCEEDED", output="只读验证通过" * 4000)
    await coordinator.reconcile(workspace_id, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    assert state.tickets[0].outcome == "ready"
    assert state.tickets[0].assignments[0].report
    assert len(state.tickets[0].assignments[0].report.summary) <= 16000
    assert state.tickets[0].assignments[0].report.summary == "只读验证通过"
    answer = await coordinator.tool(
        workspace_id,
        scope_id,
        "reply",
        "orchestrator_reply",
        {
            "trigger_id": "om_trigger", "ticket_id": ticket_id, "purpose": "result", "text": "只读验证通过",
            "artifact_ids": [str(screenshot.id)],
        },
    )
    assert answer["ok"] is True
    reply_effect = next(
        item for item in (await coordinator.store.snapshot(workspace_id, scope_id)).effects if item.kind == "reply"
    )
    # 图片准备失败尚未发送消息，允许安全重试；成功后重复消费也不得重复上传、发消息。
    original_upload = h.reader.upload_image
    monkeypatch.setattr(
        h.reader, "upload_image", AsyncMock(side_effect=ContextError("PROVIDER_UNAVAILABLE", "临时故障"))
    )
    with pytest.raises(ContextError):
        await coordinator.deliver(workspace_id, scope_id, reply_effect)
    await coordinator.effect_failed(workspace_id, scope_id, reply_effect)
    pending = await coordinator.store.snapshot(workspace_id, scope_id)
    assert pending.replies[0].state == "pending" and h.reader.replies == 0
    assert pending.tickets[0].delivery_error == "DELIVERY_PREPARATION_FAILED"
    monkeypatch.setattr(h.reader, "upload_image", original_upload)
    assert await coordinator.deliver(workspace_id, scope_id, reply_effect)
    assert not await coordinator.deliver(workspace_id, scope_id, reply_effect)
    assert h.reader.replies == 1
    assert len(h.reader.uploads) == 1 and h.reader.sent_images == ("img_test_1",)
    assert (await coordinator.store.snapshot(workspace_id, scope_id)).tickets[0].delivery_error is None
    status = await h.client.get(h.base + f"/context/runs/{first_id}/channel-status", headers=h.headers)
    assert status.status_code == 200, status.text
    assert status.json()["inbox"]["addressed"] is False
    assert status.json()["deliveries"][0]["status"] == "SENT"
    await receive(
        h, coordinator, identity="om_followup", root="om_trigger" if root_present else None, topic="omt_shared"
    )
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    assert len(state.tickets) == 1 and len(state.tickets[0].assignments) == 2
    worker_effect = next(item for item in state.effects if item.kind == "worker")
    await coordinator.launch(workspace_id, scope_id, worker_effect)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    latest = state.tickets[0]
    assert latest.task_id == ticket.task_id
    assert latest.assignments[-1].run_id != str(first_id)
    # 同一 Task 的后续汇总可以使用前一轮截图，不必重新操作网站。
    inherited = await delivery.reply_screenshots(
        h.actor, UUID(str(latest.assignments[-1].run_id)), artifact_ids=(str(screenshot.id),)
    )
    assert [item.id for item in inherited] == [screenshot.id]
    # 第一条消息到期时，其派生的所有 Run 都必须进入原生正文清理范围。
    original_inbox = next(
        item for item in await h.context.store.list(workspace_id, "inbox") if item["message_id"] == "om_trigger"
    )
    await h.context.store.change(
        workspace_id,
        "inbox",
        f"{original_inbox['connection_id']}:{original_inbox['message_id']}",
        lambda old: dict(old or {}, expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()),
    )
    assert {first_id, UUID(str(latest.assignments[-1].run_id))} <= await h.context.expired_run_ids(workspace_id)


@pytest.mark.anyio
async def test_failed_native_run_requests_explicit_retry_in_original_channel(contextual: ContextRuns) -> None:
    """执行器错误必须成为可发送的问题，不能只留在运行页或假装业务复现完成。"""
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace = h.actor.workspace_id
    await receive(h, coordinator)
    await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace, scope_id)
    await coordinator.launch(workspace, scope_id, next(e for e in state.effects if e.kind == "worker"))
    state = await coordinator.store.snapshot(workspace, scope_id)
    run_id = UUID(str(state.tickets[0].assignments[0].run_id))
    execution = SqlAlchemyExecutionStore(h.original.sessions)
    lease = await execution.claim(workspace_id=workspace, run_id=run_id, owner="failure-test", lease_seconds=30)
    assert lease
    assert await execution.finish(lease, status="FAILED", failure_code="MODEL_FAILED")
    await coordinator.reconcile(workspace, scope_id)
    current = await coordinator.store.snapshot(workspace, scope_id)
    ticket = current.tickets[0]
    assert ticket.attention and "MODEL_FAILED" in ticket.attention and "重试" in ticket.attention
    assert ticket.outcome is None and ticket.ended_at is None


@pytest.mark.anyio
async def test_feishu_run_computer_requires_registered_scope_and_live_source(
    contextual: ContextRuns, monkeypatch: pytest.MonkeyPatch
) -> None:
    """沿真实 Inbox 和原生租约调用电脑工具，飞书消息和 OpenBot HTTP 使用夹具。"""
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace = h.actor.workspace_id
    await receive(h, coordinator, instruction="请检查公开测试站点")
    await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace, scope_id)
    await coordinator.launch(workspace, scope_id, next(item for item in state.effects if item.kind == "worker"))
    state = await coordinator.store.snapshot(workspace, scope_id)
    run_id = UUID(str(state.tickets[0].assignments[0].run_id))
    execution = cast(SqlAlchemyExecutionStore, h.app.state.execution_store)
    lease = await execution.claim(workspace_id=workspace, run_id=run_id, owner="computer-test", lease_seconds=30)
    assert lease

    gateway = cast(ToolGateway, h.app.state.tool_gateway)
    context_access = cast(ContextModuleAccess, h.app.state.context_access)
    access = GatewayModuleAccess(context_access, execution, h.context)
    bot = OpenBot("http://supervisor:4300", "supervisor-test", "master-test", "weora")
    supervisor = AsyncMock(return_value={})
    monkeypatch.setattr(bot, "supervisor", supervisor)
    monkeypatch.setattr(bot, "state", AsyncMock(side_effect=[None, "running"]))
    screenshot = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7uoAAAAASUVORK5CYII="
    call = AsyncMock(
        side_effect=[
            {"holder": "bot"},
            {"title": "真实工具边界夹具"},
            {"holder": "bot"},
            {"requests": [{"url": "https://fixture.example/", "status": 200}], "screenshot": screenshot},
        ]
    )
    monkeypatch.setattr(bot, "call", call)
    scopes: dict[str, ComputerMode] = {}
    register_computer_tools(
        gateway,
        access,
        cast(RunService, h.app.state.run_service),
        bot,
        ("https://fixture.example",),
        scopes,
        cast(DeliveryService, h.app.state.delivery_service),
    )
    tool_scope = ToolScope(run_id, h.actor, lease.owner, lease.fencing_token)
    target: Json = {"url": "https://fixture.example/"}
    assert await gateway.invoke(tool_scope, "no_scope", "browser.navigate", target) == {
        "ok": False,
        "error": {"code": "COMPUTER_NOT_ALLOWED"},
    }
    call.assert_not_awaited()
    supervisor.assert_not_awaited()

    scopes[scope_id] = "observe"
    assert await gateway.invoke(tool_scope, "click", "browser.click", {"ref": "e1", "snapshotId": 1}) == {
        "ok": False,
        "error": {"code": "COMPUTER_OBSERVE_ONLY"},
    }
    call.assert_not_awaited()
    assert (await gateway.invoke(tool_scope, "go", "browser.navigate", target))["ok"] is True
    identity = bot.computer_id(workspace, h.actor.user_id, UUID(str(state.tickets[0].task_id)), observe=True)
    supervisor.assert_awaited_once_with(f"/computers/{identity}/ensure", {})
    call.assert_any_await(
        identity, "/navigate", {**target, "observeOrigins": ["https://fixture.example"]}, deadline_seconds=40
    )
    result = await gateway.invoke(tool_scope, "capture", "browser.evidence", {})
    assert result["ok"] is True
    data = cast(Json, result["data"])
    assert "screenshot" not in data and data["screenshot_available"] is True
    artifacts = await h.client.get(h.base + f"/runs/{run_id}/artifacts")
    assert artifacts.status_code == 200
    assert [item["id"] for item in artifacts.json()["items"] if item["kind"] == "computer_screenshot"] == [
        data["screenshot_artifact_id"]
    ]

    # 导航已明确失败时仍可取快照，运输超时则保留 UNKNOWN 且不自动重放。
    monkeypatch.setattr(bot, "state", AsyncMock(return_value="running"))
    call.side_effect = [{"holder": "bot"}, ComputerError(502, "COMPUTER_UPSTREAM_FAILED", "导航失败")]
    assert await gateway.invoke(tool_scope, "known_failure", "browser.navigate", target) == {
        "ok": False,
        "error": {"code": "BROWSER_NAVIGATION_FAILED"},
    }
    call.side_effect = [{"holder": "bot"}, ComputerError(503, "COMPUTER_UNAVAILABLE", "连接超时")]
    assert (await gateway.invoke(tool_scope, "unknown_navigation", "browser.navigate", target))["ok"] is False
    before_retry = call.await_count
    assert (await gateway.invoke(tool_scope, "unknown_navigation", "browser.navigate", target))["ok"] is False
    assert call.await_count == before_retry
    invocations = (await h.client.get(h.base + f"/runs/{run_id}/tools")).json()["items"]
    statuses = {item["call_id"]: item["status"] for item in invocations}
    assert statuses["known_failure"] == "FAILED"
    assert statuses["unknown_navigation"] == "UNKNOWN"

    await h.context.set_grant(h.context_actor, h.source_id, h.context_actor.user_id, False)
    assert await gateway.invoke(tool_scope, "capture", "browser.evidence", {}) == {
        "ok": False,
        "error": {"code": "ACCESS_REVOKED"},
    }
    assert call.await_count == 8


@pytest.mark.anyio
async def test_worker_capacity_reserves_only_one_extra_human_slot(contextual: ContextRuns) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.context_actor.workspace_id

    def limit(_: Engine, state: State) -> None:
        state.config = replace(state.config, max_workers=1)

    await coordinator.store.transact(workspace_id, scope_id, limit)
    for index in range(3):
        await receive(h, coordinator, identity=f"om_capacity_{index}")
        await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    effects = [item for item in state.effects if item.kind == "worker"]
    assert len(effects) == 3
    await coordinator.launch(workspace_id, scope_id, effects[0])
    with pytest.raises(DeferredEffect, match="WORKER_CAPACITY_WAIT"):
        await coordinator.launch(workspace_id, scope_id, effects[1])
    # 人工跟进允许一个保留槽；第二个人工唤醒也必须等待，不能无限越过并发上限。
    await coordinator.launch(workspace_id, scope_id, replace(effects[1], priority=1))
    with pytest.raises(DeferredEffect, match="WORKER_CAPACITY_WAIT"):
        await coordinator.launch(workspace_id, scope_id, replace(effects[2], priority=1))


@pytest.mark.anyio
async def test_lost_native_ack_replays_frozen_request_after_new_human_input(
    contextual: ContextRuns, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.context_actor.workspace_id
    await receive(h, coordinator)
    await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    effect = next(item for item in state.effects if item.kind == "worker")
    native_create = coordinator.runs.create

    async def lose_ack() -> None:
        raise RuntimeError("模拟原生提交成功，但协调器未收到返回")

    with monkeypatch.context() as patch:
        patch.setattr(coordinator.runs, "create", after_native_commit(native_create, lose_ack))
        with pytest.raises(RuntimeError, match="未收到返回"):
            await coordinator.launch(workspace_id, scope_id, effect)

    await receive(h, coordinator, identity="om_after_commit", root="om_trigger")
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    followup = next(item for item in state.effects if item.kind == "worker")
    assert followup.priority > effect.priority
    await coordinator.launch(workspace_id, scope_id, followup)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    response = await h.client.get(h.base + "/runs", headers=h.headers)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    ticket = state.tickets[0]
    assert ticket.task_id and ticket.assignments[-1].run_id
    assert len(ticket.inputs) == 2 and not ticket.inputs[-1].read_at


@pytest.mark.anyio
async def test_human_stop_during_native_commit_still_persists_cancel_compensation(
    contextual: ContextRuns, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.context_actor.workspace_id
    await receive(h, coordinator)
    ticket_id = await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    pending = next(item for item in state.effects if item.kind == "worker")
    effect = await coordinator.store.claim(workspace_id, scope_id, pending.id, "launch-worker")
    assert effect
    native_create = coordinator.runs.create

    async def stop_after_commit() -> None:
        await receive(h, coordinator, identity="om_human_stop", root="om_trigger")
        await coordinator.store.transact(
            workspace_id, scope_id, lambda engine, _: engine.stop(ticket_id, "om_human_stop")
        )

    with monkeypatch.context() as patch:
        patch.setattr(coordinator.runs, "create", after_native_commit(native_create, stop_after_commit))
        await coordinator.launch(workspace_id, scope_id, effect)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    assert state.tickets[0].ended_by == "human"
    assert state.tickets[0].assignments[-1].run_id is None
    cancel = [item for item in state.effects if item.kind == "cancel"]
    assert len(cancel) == 1 and cancel[0].data["run_id"]


@pytest.mark.anyio
async def test_observe_has_no_task_and_restart_recovers_encrypted_decision(contextual: ContextRuns) -> None:
    h = contextual
    coordinator, scope_id = await configure(h, "observe")
    await receive(h, coordinator)
    await decide(h, coordinator, scope_id)
    fresh = SqlCoordinationStore(h.original.sessions, h.context.cipher)
    state = await fresh.snapshot(h.context_actor.workspace_id, scope_id)
    assert not state.tickets and state.decisions[0].audit["would_accept"] is True
    async with h.original.sessions() as session:
        row = await session.scalar(select(CoordinationScope))
        assert row and "修复登录" not in str(row.payload) and "intent_evidence" not in str(row.payload)


@pytest.mark.anyio
async def test_application_owns_due_effects_and_admin_status_projection(contextual: ContextRuns) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.context_actor.workspace_id
    due = await coordinator.pending_effects(workspace_id, scope_id)
    assert {item.kind for item in due} == {"main", "poll"}
    main = next(item for item in due if item.kind == "main")
    claimed = await coordinator.store.claim(workspace_id, scope_id, main.id, "running-main")
    assert claimed
    assert main.id not in {item.id for item in await coordinator.pending_effects(workspace_id, scope_id)}
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    await coordinator.save(h.context_actor, replace(state.config, paused=True), expected_revision=state.config.revision)
    assert not await coordinator.pending_effects(workspace_id, scope_id)
    response = await h.client.get(h.base + "/coordination", headers=h.headers)
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["config"]["paused"] and item["status"]["paused"]
    assert set(item) == {"config", "status", "effects", "deliveries"}


@pytest.mark.anyio
async def test_control_ingests_without_waiting_for_maintenance_io(
    contextual: ContextRuns, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    event = inbound()
    cast(Json, cast(Json, event["event"])["message"])["mentions"] = []
    await FeishuChannels(h.context, coordinator.candidate_admission).receive(
        h.context_actor.workspace_id, h.connection_id, event
    )
    notified: list[str] = []

    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("控制快路径不能执行容器清理或渠道发送")

    monkeypatch.setattr(h.app.state.sandbox, "reap_orphans", forbidden)
    monkeypatch.setattr(h.reader, "reply", forbidden)
    monkeypatch.setattr(worker_bootstrap.celery_app, "send_task", lambda name, **kwargs: notified.append(name))
    assert await dispatch(h.app) >= 1
    state = await coordinator.store.snapshot(h.context_actor.workspace_id, scope_id)
    assert len(state.triggers) == 1
    assert notified and set(notified) == {"weora.coordination.execute"}


@pytest.mark.anyio
async def test_unknown_outbox_requires_reconciliation_and_never_sends_again(contextual: ContextRuns) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.context_actor.workspace_id
    await receive(h, coordinator)
    ticket_id = await decide(h, coordinator, scope_id)
    reply = await coordinator.tool(
        workspace_id,
        scope_id,
        "ack",
        "orchestrator_reply",
        {"trigger_id": "om_trigger", "ticket_id": ticket_id, "purpose": "ack", "text": "收到"},
    )
    assert reply["ok"] is True

    def interrupted(_: Engine, state: State) -> None:
        state.replies[0].state = "unknown"

    await coordinator.store.transact(workspace_id, scope_id, interrupted)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    effect = next(item for item in state.effects if item.kind == "reply")
    with pytest.raises(CoordinationError, match="CHANNEL_RECONCILIATION_REQUIRED"):
        await coordinator.deliver(workspace_id, scope_id, effect)
    await coordinator.effect_failed(workspace_id, scope_id, effect)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    assert state.tickets[0].delivery_error == "DELIVERY_RESULT_UNKNOWN"
    assert any(item.kind == "ticket.delivery_failed" for item in state.notices)
    assert h.reader.replies == 0


@pytest.mark.anyio
async def test_revoke_clears_session_and_denies_old_decision_and_outbound(contextual: ContextRuns) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    await receive(h, coordinator)
    ticket_id = await decide(h, coordinator, scope_id)
    ack = await coordinator.tool(
        h.context_actor.workspace_id,
        scope_id,
        "ack",
        "orchestrator_reply",
        {"trigger_id": "om_trigger", "ticket_id": ticket_id, "purpose": "ack", "text": "收到"},
    )
    assert ack["ok"] is True

    def derived_content(_: Engine, state: State) -> None:
        state.triggers[0].handbacks[ticket_id] = "过期派生机密"
        # Run 已被后续尝试替换时，其 provisional report 不能变成无法清理的正文孤儿。
        state.checkpoints["report:replaced-run"] = {"summary": "过期派生机密"}

    await coordinator.store.transact(h.context_actor.workspace_id, scope_id, derived_content)
    await h.context.set_grant(h.context_actor, h.source_id, h.context_actor.user_id, False)
    state = await coordinator.authorized_state(h.context_actor.workspace_id, scope_id)
    assert not state.triggers[0].authorized and not state.triggers[0].text
    assert not any(item.messages for item in state.sessions)
    assert state.tickets[0].mandate.scope == "来源已失效"
    assert state.triggers[0].handbacks[ticket_id] == "来源已失效"
    assert "report:replaced-run" not in state.checkpoints
    reply = next(item for item in state.effects if item.kind == "reply")
    assert not await coordinator.deliver(h.context_actor.workspace_id, scope_id, reply)
    assert h.reader.replies == 0


@pytest.mark.anyio
async def test_transaction_failure_rolls_back_effects_and_ticket_together(contextual: ContextRuns) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)

    def fail(engine: Engine, state: State) -> None:
        engine.record_notice("injected", "rollback", {"fact": "不得提交"})
        raise CoordinationError("INJECTED_FAILURE")

    before = await coordinator.store.snapshot(h.context_actor.workspace_id, scope_id)
    with pytest.raises(CoordinationError, match="INJECTED_FAILURE"):
        await coordinator.store.transact(h.context_actor.workspace_id, scope_id, fail)
    after = await coordinator.store.snapshot(h.context_actor.workspace_id, scope_id)
    assert after == before


@pytest.mark.anyio
async def test_compensation_cursor_and_dedup(contextual: ContextRuns) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.context_actor.workspace_id
    assert await compensate(coordinator, workspace_id, scope_id) == 0
    assert h.reader.reads == 0
    now = datetime.now(UTC)
    item = Message("om_polled", "oc_project", None, "ou_user", now - timedelta(seconds=2), "请修复登录")
    h.reader.pages = {None: MessagePage((item,))}

    def due(_: Engine, state: State) -> None:
        checkpoint = cast(Json, state.checkpoints["poll:-"])
        checkpoint.update(
            since=(now - timedelta(seconds=10)).isoformat(),
            next_at=now.isoformat(),
            initialized_at=(now - timedelta(seconds=10)).isoformat(),
        )

    await coordinator.store.transact(workspace_id, scope_id, due)
    assert await compensate(coordinator, workspace_id, scope_id) == 1
    await _pump_context(h.app)
    assert len((await coordinator.store.snapshot(workspace_id, scope_id)).triggers) == 1
    await coordinator.store.transact(workspace_id, scope_id, due)
    await compensate(coordinator, workspace_id, scope_id)
    await _pump_context(h.app)
    assert len((await coordinator.store.snapshot(workspace_id, scope_id)).triggers) == 1
    await coordinator.store.transact(workspace_id, scope_id, due)
    h.reader.pages = {None: MessagePage((item,), "stuck"), "stuck": MessagePage((), "stuck")}
    before = await coordinator.store.snapshot(workspace_id, scope_id)
    with pytest.raises(ContextError, match="分页未前进"):
        await compensate(coordinator, workspace_id, scope_id)
    after = await coordinator.store.snapshot(workspace_id, scope_id)
    assert after.checkpoints["poll:-"] == before.checkpoints["poll:-"]


@pytest.mark.anyio
async def test_eval_reference_offsets_budget_and_finished_cache(contextual: ContextRuns) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.context_actor.workspace_id
    event = inbound()
    message = cast(Json, cast(Json, event["event"])["message"])
    message["mentions"] = []
    message["parent_id"] = "om_parent"
    h.reader.single = Message(
        "om_parent", "oc_project", None, "ou_other", datetime.now(UTC) - timedelta(minutes=1), "a" * 5000
    )
    await FeishuChannels(h.context, coordinator.candidate_admission).receive(workspace_id, h.connection_id, event)
    await _pump_context(h.app)
    evaluation = (await coordinator.store.transact(workspace_id, scope_id, lambda engine, _: engine.pass_due()))[0]
    first = await coordinator.tool(
        workspace_id, scope_id, "read", "orchestrator_eval_read", {"eval_id": evaluation.id, "resource": "parent"}
    )
    assert first["ok"] is True and len(str(cast(Json, first["context"])["text"])) == 4000
    second = await coordinator.tool(
        workspace_id,
        scope_id,
        "read2",
        "orchestrator_eval_read",
        {"eval_id": evaluation.id, "resource": "parent", "offset": 4000},
    )
    assert len(str(cast(Json, second["context"])["text"])) == 1000
    reads = h.reader.reads
    refused = await coordinator.tool(
        workspace_id,
        scope_id,
        "invented",
        "orchestrator_eval_read",
        {"eval_id": evaluation.id, "resource": "message", "id": "om_other_group"},
    )
    assert refused["code"] == "EVIDENCE_NOT_DELIVERED" and h.reader.reads == reads
    for index in range(9):
        assert (
            await coordinator.tool(
                workspace_id,
                scope_id,
                str(index),
                "orchestrator_eval_read",
                {"eval_id": evaluation.id, "resource": "trigger"},
            )
        )["ok"] is True
    exhausted = await coordinator.tool(
        workspace_id, scope_id, "budget", "orchestrator_eval_read", {"eval_id": evaluation.id, "resource": "trigger"}
    )
    assert exhausted["code"] == "EVAL_READ_BUDGET_EXCEEDED"
    await coordinator.tool(
        workspace_id, scope_id, "done", "orchestrator_eval_decide", dict(result(), eval_id=evaluation.id)
    )
    finished = await coordinator.tool(
        workspace_id, scope_id, "late", "orchestrator_eval_read", {"eval_id": evaluation.id, "resource": "parent"}
    )
    assert finished["code"] == "EVAL_FINISHED"
