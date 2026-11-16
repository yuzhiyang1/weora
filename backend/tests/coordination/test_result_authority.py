"""最终正文由 Worker 报告决定，Main 仅触发投递，不能再次压缩或改写证据。"""

from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from tests.api.test_runs import runs as runs
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.coordination.test_engine import NOW, accept, setup
from tests.coordination.test_service import configure, decide, receive
from weora.modules.coordination.domain import Report, State
from weora.modules.coordination.engine import Engine, later
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore


@pytest.mark.parametrize("fallback_first", [False, True])
@pytest.mark.parametrize("at_limit", [False, True])
def test_main_and_fallback_share_unmodified_worker_report_and_one_outbox(
    fallback_first: bool, at_limit: bool,
) -> None:
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    assignment = ticket.assignments[-1]
    assignment.run_id = "actual-run"
    engine.mark_input(identity, assignment.id, ticket.inputs[0].id, read=True)
    summary = "材料组：2/2/2/3=9；位置：ExamComposeManager.java:480。\n"
    if at_limit:
        summary += "全文保留" * ((16000 - len(summary)) // 4)
        summary += "尾" * (16000 - len(summary))
    engine.report(identity, assignment.id, Report(
        "ready", summary, NOW, verified=True, completed_input_ids=(ticket.inputs[0].id,),
    ))
    engine.now = lambda: later(NOW, 21)
    if fallback_first:
        engine.relay_retried_results("https://console.example")
    sent = engine.reply(
        "m1", "result", "错误摘要：3/3/3/4=9，位置 :17", ticket_id=identity,
        console_url="https://console.example",
    )
    engine.relay_retried_results("https://console.example")
    repeated = engine.reply("m1", "result", "再次改写", ticket_id=identity)
    answer = engine.reply("m1", "answer", "改用 answer 伪造最终数字", ticket_id=identity)
    assert repeated.id == sent.id == answer.id and answer.purpose == "result"
    assert len([item for item in state.replies if item.purpose == "result"]) == 1
    assert len([item for item in state.effects if item.data.get("reply_id") == sent.id]) == 1
    expected = summary if at_limit else summary + "\n\n[查看运行详情](https://console.example/runs/actual-run)"
    assert sent.text == expected and "错误摘要" not in sent.text


@pytest.mark.anyio
@pytest.mark.parametrize("legacy_pending", [False, True])
async def test_channel_receives_exact_worker_report_even_if_main_or_old_outbox_rewrites_it(
    contextual: ContextRuns, monkeypatch: pytest.MonkeyPatch, legacy_pending: bool,
) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    coordinator.console_url = None
    workspace_id = h.actor.workspace_id
    await receive(h, coordinator)
    ticket_id = await decide(h, coordinator, scope_id)
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    await coordinator.launch(workspace_id, scope_id, next(item for item in state.effects if item.kind == "worker"))
    ticket = (await coordinator.store.snapshot(workspace_id, scope_id)).tickets[0]
    assignment = ticket.assignments[-1]
    run_id = UUID(str(assignment.run_id))
    store = SqlAlchemyExecutionStore(h.original.sessions)
    lease = await store.claim(workspace_id=workspace_id, run_id=run_id, owner="report-authority", lease_seconds=30)
    assert lease
    summary = "## 完整报告\n材料组数为2/2/2/3=9，位置 ExamComposeManager.java:480。\n" + "保留完整证据与边界。\n" * 500
    report = Report(
        "ready", summary, datetime.now(UTC).isoformat(), completed_input_ids=(ticket.inputs[0].id,),
    )

    def propose(engine: Engine, current: State) -> None:
        engine.mark_input(ticket_id, assignment.id, ticket.inputs[0].id, read=True)
        current.checkpoints[f"report:{run_id}"] = asdict(report)

    await coordinator.store.transact(workspace_id, scope_id, propose)
    assert await store.finish(lease, status="SUCCEEDED", output="模型末尾简短结论")
    await coordinator.reconcile(workspace_id, scope_id)
    result = await coordinator.tool(workspace_id, scope_id, "wrong-summary", "orchestrator_reply", {
        "trigger_id": "om_trigger", "ticket_id": ticket_id, "purpose": "result",
        "text": "错误摘要：3/3/3/4=9，HSK4阅读第二部分。",
    })
    assert result["ok"] is True
    current = await coordinator.store.snapshot(workspace_id, scope_id)
    reply = next(item for item in current.replies if item.purpose == "result")
    assert reply.text == summary
    effect = next(item for item in current.effects if item.data.get("reply_id") == reply.id)
    if legacy_pending:
        # 升级时可能已有旧 Main 摘要待发送；发送事务也应恢复 Worker 正文。
        def old_summary(_: Engine, current: State) -> None:
            next(item for item in current.replies if item.id == reply.id).text = "升级前错误数字17"
        await coordinator.store.transact(workspace_id, scope_id, old_summary)
    sender = AsyncMock(return_value="om_full_report")
    monkeypatch.setattr(h.reader, "reply", sender)
    assert await coordinator.deliver(workspace_id, scope_id, effect)
    assert not await coordinator.deliver(workspace_id, scope_id, effect)
    sender.assert_awaited_once()
    assert sender.await_args and sender.await_args.args[2] == summary
