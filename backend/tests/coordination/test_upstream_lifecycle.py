"""对照 59e6c405 的 tickets/assignments/tools 反例，保护人类等待和消息归属。"""

from dataclasses import replace

import pytest

from tests.coordination.test_engine import NOW, accept, message, setup
from weora.modules.coordination.domain import CoordinationError, Report
from weora.modules.coordination.engine import later


@pytest.mark.parametrize("status", ["blocked", "failed"])
def test_automatic_facts_do_not_wake_until_new_human_input(status: str) -> None:
    """自动备注留在事实中，不能解除人工等待；新的人类输入才允许再次派发。"""
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    worker = next(item for item in state.effects if item.kind == "worker")
    engine.note(identity, "报告前的自动事实", "before")
    worker.state = "done"
    engine.report(identity, ticket.assignments[-1].id, Report(status, "需人工处理", NOW))  # type: ignore[arg-type]
    engine.note(identity, "报告后的自动事实", "after")
    engine.pass_due()
    assert worker.state == "done"
    assert len([item for item in ticket.inputs if item.kind == "note"]) == 2
    assert not any(item.id in ticket.undelivered for item in ticket.inputs if item.kind == "note")
    engine.now = lambda: later(NOW, 1)
    engine.receive(message("human-resumes"))
    assert not engine.waits_for_person(ticket)
    assert next(item for item in state.effects if item.kind == "worker").state == "pending"


@pytest.mark.parametrize("status", ["ready", "dropped"])
@pytest.mark.parametrize("handled", [False, True])
def test_ticket_end_releases_only_unhandled_human_messages(status: str, handled: bool) -> None:
    """结束窗口的未读跟进退回主 Agent；已确认的消息不重复接单。"""
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    assignment = ticket.assignments[-1]
    engine.mark_input(identity, assignment.id, ticket.inputs[0].id, read=True)
    engine.receive(message("late"))
    if handled:
        engine.reply("late", "ack", "已记录补充", ticket_id=identity)
    engine.report(identity, assignment.id, Report(status, "本轮结束", NOW, verified=True))  # type: ignore[arg-type]
    assert ticket.ended_at
    assert not ticket.undelivered
    evaluations = engine.pass_due()
    assert any("late" in item.trigger_ids for item in evaluations) is not handled
    assert all("m1" not in item.trigger_ids for item in evaluations)
    assert not engine.owners(state.triggers[-1])


def test_main_can_unsubscribe_own_origin_but_worker_and_bot_cannot() -> None:
    """人类管理请求与 Worker 的判无关操作分开，机器人不能生成管理授权。"""
    engine, state = setup()
    identity = accept(engine, state)
    with pytest.raises(CoordinationError, match="OWN_THREAD_REQUIRED"):
        engine.unsubscribe(identity, "chat:root")
    engine.receive(replace(message("bot"), human=False))
    with pytest.raises(CoordinationError, match="EXPLICIT_UNSUBSCRIBE_REQUIRED"):
        engine.unsubscribe_thread("chat:root", "bot")
    engine.receive(message("unsubscribe", addressed=True))
    assert engine.unsubscribe_thread("chat:root", "unsubscribe") == [identity]
    assert not state.threads[0].subscriptions
    assert not engine.ticket(identity).follows


def test_stopping_releases_unread_followup_without_requeueing_stop() -> None:
    """停止消息已由主 Agent 决定，其他未接住的消息仍需重新判断归属。"""
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    engine.mark_input(identity, ticket.assignments[-1].id, ticket.inputs[0].id, read=True)
    engine.receive(message("late"))
    engine.receive(message("stop", addressed=True))
    engine.stop(identity, "stop")
    evaluation = engine.pass_due()[0]
    assert evaluation.trigger_ids == ["late"]


def test_ready_waits_for_owned_review_and_merge_does_not_swallow_human_input() -> None:
    """对应上游 followsDone/unfinished：ready 是等待评审，晚到人类请求需要先被处理。"""
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    assignment = ticket.assignments[-1]
    engine.mark_input(identity, assignment.id, ticket.inputs[0].id, read=True)
    engine.bind_review(identity, "code:review", "run", "review")
    engine.report(identity, assignment.id, Report("ready", "已提交评审", NOW, verified=True))
    assert not ticket.ended_at and not assignment.ended_at
    engine.receive(message("late"))
    engine.observe_review(identity, "code:review", "merged", later(NOW, 1))
    assert not ticket.ended_at
    engine.mark_input(identity, assignment.id, ticket.inputs[-1].id, read=True)
    engine.report(identity, assignment.id, Report("ready", "补充已完成", later(NOW, 2), verified=True))
    assert ticket.outcome == "merged"
    assert engine.reply("m1", "result", "评审已经合并", ticket_id=identity).purpose == "result"


def test_review_reopening_does_not_automatically_restart_ended_work() -> None:
    """机器人和生命周期事件只能保留事实，只有后续有效人类输入可以恢复已结束工作。"""
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    assignment = ticket.assignments[-1]
    engine.mark_input(identity, assignment.id, ticket.inputs[0].id, read=True)
    engine.bind_review(identity, "code:review", "run", "review")
    engine.report(identity, assignment.id, Report("ready", "已提交", NOW, verified=True))
    engine.observe_review(identity, "code:review", "closed", later(NOW, 1))
    assert ticket.outcome == "closed"
    engine.observe_review(identity, "code:review", "open", later(NOW, 2))
    assert ticket.ended_at and len(ticket.assignments) == 1
    engine.receive(message("resume"))
    assert not ticket.ended_at and len(ticket.assignments) == 2


def test_late_close_cannot_finish_resumed_assignment_or_steal_unread_human_input() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    ticket = engine.ticket(identity)
    assignment = ticket.assignments[-1]
    engine.mark_input(identity, assignment.id, ticket.inputs[0].id, read=True)
    engine.bind_review(identity, "code:review", "run", "review")
    engine.report(identity, assignment.id, Report("waiting", "当前轮次空闲", NOW))
    engine.observe_review(identity, "code:review", "closed", later(NOW, 3), closed_at=later(NOW, -1))
    assert not ticket.ended_at
    # 缺少可信关闭时间时也不能直接把 waiting 转成完成。
    engine.observe_review(identity, "code:review", "closed", later(NOW, 4))
    assert not ticket.ended_at
    engine.receive(message("late-human"))
    engine.mark_input(identity, assignment.id, ticket.inputs[-1].id)
    engine.observe_review(identity, "code:review", "closed", later(NOW, 5), closed_at=later(NOW, 2))
    assert not ticket.ended_at
