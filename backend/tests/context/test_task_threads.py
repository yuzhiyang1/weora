"""飞书接入到同 Task 新 Run 的事务集成；消息运输与模型均为测试替身。"""

import json
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import select

from tests.api.test_runs import runs as runs
from tests.api.test_task_continuations import finish
from tests.context.test_context import inbound
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from weora.bootstrap.worker import _pump_context
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextError, Json, Message, MessagePage
from weora.modules.runs.application.execution import RuntimeFailure
from weora.modules.runs.application.service import RunService
from weora.modules.runs.domain.errors import RunNotFoundError
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.models import RunModel
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork


async def setup(h: ContextRuns) -> FeishuChannels:
    await h.context.probe(h.context_actor, h.connection_id)
    await h.context.bind_identity(h.context_actor, h.connection_id, "ou_user", h.actor.user_id)
    channels = FeishuChannels(h.context)
    await channels.save_route(h.context_actor, source_id=h.source_id,
                               agent_id=UUID(h.original.agent_id), agent_version="0.1.0")
    return channels


async def send(
    h: ContextRuns, channels: FeishuChannels, message_id: str, text: str,
    *, thread: str | None = "omt_task", quote: str | None = None,
) -> Json:
    payload = inbound(event_id="ev_" + message_id, message_id=message_id)
    message = cast(Json, cast(Json, payload["event"])["message"])
    message["content"] = json.dumps({"text": "@_user_1 " + text})
    if thread:
        message["thread_id"] = thread
    if quote:
        message["parent_id"] = quote
        message["root_id"] = quote
    received = await channels.receive(h.actor.workspace_id, h.connection_id, payload)
    assert received["status"] == "persisted"
    await _pump_context(h.app)
    return next(row for row in await h.context.store.list(h.actor.workspace_id, "inbox")
                if row["message_id"] == message_id)


@pytest.mark.anyio
async def test_numbered_message_without_coordination_never_falls_back_to_new_task(contextual: ContextRuns) -> None:
    h = contextual
    channels = await setup(h)
    receipt = await send(h, channels, "om_number", "#DEADBEEF 补充说明", thread=None)
    assert receipt["status"] == "REJECTED"
    assert receipt["rejection_code"] == "TASK_REFERENCE_REQUIRES_COORDINATION"
    assert "run_id" not in receipt
    async with h.original.sessions() as session:
        assert await session.scalar(select(RunModel.id)) is None


@pytest.mark.anyio
async def test_thread_followup_waits_and_then_replies_to_its_own_message(contextual: ContextRuns) -> None:
    h = contextual
    channels = await setup(h)
    first = await send(h, channels, "om_1", "分析登录问题")
    root_run = UUID(str(first["run_id"]))
    # 同一初始消息重投不能被已创建的线程关联误判成新的跟进。
    replay = await send(h, channels, "om_1", "分析登录问题")
    assert replay["run_id"] == str(root_run)
    queued = await send(h, channels, "om_2", "补充：只需要解释原因", quote="om_1")
    assert queued["status"] == "WAITING_RUN" and queued.get("run_id") is None
    service = cast(RunService, h.app.state.run_service)
    await finish(h.original, root_run, "登录问题的第一轮分析")
    assert await service.dispatch_task_inputs() == 1
    await _pump_context(h.app)
    rows = await h.context.store.list(h.actor.workspace_id, "inbox")
    second = next(row for row in rows if row["message_id"] == "om_2")
    continued = UUID(str(second["run_id"]))
    assert second["status"] == "SUBMITTED" and continued != root_run
    a = await service.access_metadata(h.actor, root_run)
    b = await service.access_metadata(h.actor, continued)
    assert a is not None and b is not None and a.task.id == b.task.id
    assert b.task.origin_metadata and b.task.origin_metadata["message_id"] == "om_2"
    assert b.task.instruction == "补充：只需要解释原因"
    await finish(h.original, continued, "已结合补充完成原因说明")
    await _pump_context(h.app)
    deliveries = await h.context.store.list(h.actor.workspace_id, "delivery")
    receipt = next(row for row in deliveries if row["run_id"] == str(continued))
    assert receipt["message_id"] == "om_2" and receipt["status"] == "SENT"


@pytest.mark.anyio
async def test_unthreaded_group_messages_stay_separate_but_quote_can_continue(contextual: ContextRuns) -> None:
    h = contextual
    channels = await setup(h)
    first = await send(h, channels, "om_1", "第一个独立目标", thread=None)
    other = await send(h, channels, "om_2", "第二个独立目标", thread=None)
    service = cast(RunService, h.app.state.run_service)
    a = await service.access_metadata(h.actor, UUID(str(first["run_id"])))
    b = await service.access_metadata(h.actor, UUID(str(other["run_id"])))
    assert a is not None and b is not None and a.task.id != b.task.id
    quoted = await send(h, channels, "om_3", "继续第一个目标", thread=None, quote="om_1")
    assert quoted["status"] == "WAITING_RUN"
    assert str(a.task.id) == quoted["task_id"]


@pytest.mark.anyio
async def test_explicit_new_marker_does_not_reuse_old_task(contextual: ContextRuns) -> None:
    h = contextual
    channels = await setup(h)
    first = await send(h, channels, "om_1", "旧目标")
    separate = await send(h, channels, "om_2", "/new 明确的新目标", quote="om_1")
    service = cast(RunService, h.app.state.run_service)
    a = await service.access_metadata(h.actor, UUID(str(first["run_id"])))
    b = await service.access_metadata(h.actor, UUID(str(separate["run_id"])))
    assert a is not None and b is not None and a.task.id != b.task.id
    assert b.task.instruction == "明确的新目标"


@pytest.mark.anyio
async def test_changed_agent_route_does_not_reuse_old_thread_task(contextual: ContextRuns) -> None:
    h = contextual
    channels = await setup(h)
    first = await send(h, channels, "om_1", "旧版本的任务")
    published = await h.client.post(h.base + f"/agents/{h.original.agent_id}/publish",
                                    json={"version": "0.2.0", "changelog": "新的路由目标"}, headers=h.headers)
    assert published.status_code == 201
    await channels.save_route(h.context_actor, source_id=h.source_id,
                               agent_id=UUID(h.original.agent_id), agent_version="0.2.0")
    second = await send(h, channels, "om_2", "新版本的新任务", quote="om_1")
    assert second["status"] == "SUBMITTED"
    assert first["task_id"] != second["task_id"]


@pytest.mark.anyio
async def test_group_task_cannot_be_used_to_publish_private_console_followup(contextual: ContextRuns) -> None:
    h = contextual
    channels = await setup(h)
    first = await send(h, channels, "om_1", "群任务")
    response = await h.client.post(h.base + f"/runs/{first['run_id']}/continue",
                                   json={"instruction": "不应发到群的私有输入"},
                                   headers={**h.headers, "Idempotency-Key": "private-follow"})
    assert response.status_code == 404


@pytest.mark.anyio
async def test_source_revocation_rejects_waiting_followup_without_new_execution(contextual: ContextRuns) -> None:
    h = contextual
    channels = await setup(h)
    first = await send(h, channels, "om_1", "原任务")
    queued = await send(h, channels, "om_2", "待处理输入")
    await finish(h.original, first["run_id"])
    await h.context.set_grant(h.context_actor, h.source_id, h.actor.user_id, False)
    service = cast(RunService, h.app.state.run_service)
    assert await service.dispatch_task_inputs() == 0
    async with h.original.sessions() as session:
        assert len((await session.scalars(select(RunModel))).all()) == 1
    with pytest.raises(RunNotFoundError):
        # 不让被撤权的来源读取旧任务或排队消息回执。
        await service.task_input(h.actor, UUID(str(queued["input_id"])))


@pytest.mark.anyio
@pytest.mark.parametrize("problem", [None, "has_more", "quote_unavailable", "expired_history", "initial"])
async def test_long_thread_continuation_uses_authorized_history_without_hiding_other_gaps(
    contextual: ContextRuns, problem: str | None,
) -> None:
    h = contextual
    channels = await setup(h)
    root = await send(h, channels, "om_1", "记住任务代号青竹")
    access = ModulesExecutionAccess(
        identity=h.app.state.identity_service, agents=h.app.state.agent_registry_service,
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(h.original.sessions),
        context=h.context, executions=h.app.state.execution_store,
    )
    service = cast(RunService, h.app.state.run_service)
    initial = await service.access_metadata(h.actor, UUID(str(root["run_id"])))
    assert initial is not None
    target = initial
    if problem != "initial":
        # 先保存根轮次的授权快照；后续检查不能绕过历史过期。
        await access.load_input(initial.run)
        await finish(h.original, root["run_id"], "已记住：青竹")
        followup = await send(h, channels, "om_2", "回答任务代号", quote="om_1")
        continued = await service.access_metadata(h.actor, UUID(str(followup["run_id"])))
        assert continued is not None
        target = continued
    stamp = datetime.now(UTC) - timedelta(minutes=1)
    # 长正文额外触发材料预算，验证预算截断不会覆盖此前的引用读取失败。
    body = "长讨论" * 400 if problem == "quote_unavailable" else "讨论"
    messages = tuple(Message(f"om_history_{i}", "oc_project", "omt_task", "ou_user", stamp, f"{body} {i}")
                     for i in range(15))
    h.reader.pages[None] = MessagePage(messages, "next-page" if problem == "has_more" else None)
    h.reader.single = None if problem == "quote_unavailable" else Message(
        "om_1", "oc_project", "omt_task", "ou_user", stamp, "任务初始请求",
    )
    if problem == "expired_history":
        snapshots = await h.context.store.list(h.actor.workspace_id, "snapshot")
        old = next(item for item in snapshots if item["run_id"] == str(initial.run.id))
        await h.context.store.change(h.actor.workspace_id, "snapshot", str(old["snapshot_id"]),
                                     lambda row: dict(row or {}, expires_at=(stamp - timedelta(days=8)).isoformat()))
    if problem in {"initial", "quote_unavailable", "expired_history"}:
        with pytest.raises(RuntimeFailure, match="WAITING_CONTEXT"):
            await access.load_input(target.run)
        # 已保存的 partial 不能在重复装配时被当作完整上下文放行。
        with pytest.raises(RuntimeFailure, match="WAITING_CONTEXT"):
            await access.load_input(target.run)
    else:
        payload = await access.load_input(target.run)
        assert "已记住：青竹" in payload.instruction and "外部话题仅部分可见" in payload.instruction
        snapshots = [row for row in await h.context.store.list(h.actor.workspace_id, "snapshot")
                     if row["run_id"] == str(target.run.id)]
        assert snapshots and snapshots[0]["status"] == "partial"
        assert len(cast(list[Json], snapshots[0]["messages"])) <= 10
        assert "budget_exceeded" in str(cast(Json, snapshots[0]["coverage"])["reason"])
        again = await access.load_input(target.run)
        assert "外部话题仅部分可见" in again.instruction
        await h.context.set_grant(h.context_actor, h.source_id, h.actor.user_id, False)
        with pytest.raises((RuntimeFailure, ContextError)):
            await access.load_input(target.run)
