"""移植上游 assessment/followups/passes/queue 的不变量，而非只测 happy path。"""

from dataclasses import replace

import pytest

from weora.modules.coordination.domain import Config, CoordinationError, Report, Rule, State, Trigger
from weora.modules.coordination.engine import Engine, later
from weora.modules.coordination.policy import gate, policy_version
from weora.modules.coordination.records import records

NOW = "2026-11-03T10:00:00+00:00"


def setup(mode: str = "auto") -> tuple[Engine, State]:
    config = Config(
        "scope",
        "workspace",
        "user",
        "source",
        "main",
        "1",
        "worker",
        "1",
        ("ou_a",),
        (Rule("fix", "修复", "明确的修复请求"),),
        debounce_ms=0,
    )
    config = replace(config, mode=mode)  # type: ignore[arg-type]
    state = State(config)
    return Engine(records(state), config, lambda: NOW), state


def message(identity: str = "m1", *, addressed: bool = False, actor: str = "user") -> Trigger:
    return Trigger(
        identity,
        "workspace",
        "scope",
        actor,
        "ou_a",
        True,
        addressed,
        ["chat:root"],
        NOW,
        "请修复登录失败，沿用现有交互",
        {"message_id": identity},
    )


def test_feishu_long_reply_requires_agent_to_shorten_it() -> None:
    engine, state = setup()
    engine.receive(message())
    with pytest.raises(CoordinationError, match="REPLY_TOO_LONG"):
        engine.reply("m1", "answer", "说明" * 121)
    assert state.replies == []


def result() -> dict[str, object]:
    return {
        "decision": "accept",
        "reason": "目标明确",
        "title": "修复登录",
        "scope": "修复现有登录错误",
        "intent_evidence": "请修复登录失败",
        "rule_assessments": [{"rule_id": "fix", "matched": True, "reason": "明确修复"}],
        "direction": {
            "implementation_requested": True,
            "target_basis": "existing-standard",
            "requires_design_decision": False,
            "evidence": "沿用现有交互",
        },
    }


def accept(engine: Engine, state: State, *, addressed: bool = False) -> str:
    assert engine.receive(message(addressed=addressed))
    evaluation = engine.pass_due()[0]
    engine.delivered(evaluation.id)
    engine.read_eval(evaluation.id)
    decision = engine.decide(evaluation.id, result(), route=addressed)
    assert decision.ticket_id
    return decision.ticket_id


def test_accepted_ticket_freezes_worker_version() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    original = engine.ticket(identity).mandate
    state.config = replace(state.config, worker_agent_version="2")
    assert original.worker_agent_version == "1"
    assert engine.ticket(identity).mandate.worker_agent_version == "1"


def test_pause_preserves_input_and_moves_delivered_eval_deadline() -> None:
    engine, state = setup()
    engine.receive(message())
    evaluation = engine.pass_due()[0]
    engine.delivered(evaluation.id)
    original_deadline = evaluation.deadline
    assert original_deadline
    paused_at = later(NOW, 10)
    paused = Engine(records(state), replace(state.config, paused=True), lambda: paused_at)
    paused.pass_due()
    paused.receive(message("during-pause"))
    assert not state.decisions and not state.tickets
    resumed_at = later(paused_at, 600)
    resumed = Engine(records(state), state.config, lambda: resumed_at)
    resumed.pass_due()
    assert evaluation.deadline == later(original_deadline, 600)
    assert any(item.id == "during-pause" for item in state.triggers)


def test_worker_handback_reassesses_once_and_origin_cannot_be_unsubscribed() -> None:
    engine, state = setup()
    ticket_id = accept(engine, state)
    engine.receive(message("different-work"))
    assert engine.handback(ticket_id, "different-work", "另一个明确任务")
    with pytest.raises(CoordinationError, match="ALREADY_HANDED_BACK"):
        engine.handback(ticket_id, "different-work", "重复退回")
    assert engine.pass_due()[0].primary == "different-work"
    with pytest.raises(CoordinationError, match="OWN_THREAD_REQUIRED"):
        engine.unsubscribe(ticket_id, "chat:root")


def test_observe_records_would_accept_without_worker() -> None:
    engine, state = setup("observe")
    engine.receive(message())
    evaluation = engine.pass_due()[0]
    engine.read_eval(evaluation.id)
    decision = engine.decide(evaluation.id, result())
    assert decision.kind == "ignore" and decision.audit["would_accept"] is True
    assert not state.tickets
    assert not any(item.kind == "worker" for item in state.effects)


def test_auto_creates_one_ticket_and_durable_launch_atomically() -> None:
    engine, state = setup()
    ticket_id = accept(engine, state)
    assert not engine.receive(message())
    assert len(state.tickets) == 1
    assert state.triggers[0].fanout == [ticket_id]
    assert any(item.kind == "worker" and item.business_key == ticket_id for item in state.effects)
    with pytest.raises(CoordinationError, match="EVAL_FINISHED"):
        engine.decide(state.evaluations[0].id, result())


@pytest.mark.parametrize("broken", ["missing_rule", "duplicate_rule", "unknown_rule", "no_evidence", "unsettled"])
def test_inferred_accept_requires_complete_structured_evidence(broken: str) -> None:
    engine, state = setup()
    engine.receive(message())
    evaluation = engine.pass_due()[0]
    engine.read_eval(evaluation.id)
    value = result()
    if broken == "missing_rule":
        value["rule_assessments"] = []
    elif broken == "duplicate_rule":
        value["rule_assessments"] = value["rule_assessments"] * 2  # type: ignore[operator]
    elif broken == "unknown_rule":
        value["rule_assessments"] = [{"rule_id": "other", "matched": True, "reason": "x"}]
    elif broken == "no_evidence":
        value["intent_evidence"] = ""
    else:
        value["direction"] = {
            "implementation_requested": True,
            "target_basis": "undetermined",
            "requires_design_decision": True,
            "evidence": "需要设计",
        }
    if broken == "unsettled":
        assert engine.decide(evaluation.id, value).kind == "ignore"
    else:
        with pytest.raises(CoordinationError):
            engine.decide(evaluation.id, value)
    assert not state.tickets


def test_followup_is_workers_and_cannot_spawn_second_ticket() -> None:
    engine, state = setup()
    ticket_id = accept(engine, state)
    assert engine.receive(message("m2"))
    assert engine.pass_due() == []
    assert len(state.tickets[0].inputs) == 2
    assert state.triggers[1].fanout == [ticket_id]
    assert len(state.tickets) == 1
    assert engine.handback(ticket_id, "m2", "独立任务")
    assert engine.pass_due()[0].primary == "m2"
    with pytest.raises(CoordinationError, match="ALREADY_HANDED_BACK"):
        engine.handback(ticket_id, "m2", "再次交回")


def test_numbered_followup_cannot_borrow_another_initiators_task() -> None:
    engine, state = setup()
    ticket = engine.ticket(accept(engine, state))
    ticket.task_id = "abcdef01-0000-4000-8000-000000000001"
    ticket.assignments[0].run_id = "12345678-0000-4000-8000-000000000001"
    request = replace(message("m2", addressed=True, actor="other"), text="#ABCDEF01 继续修复")

    assert engine.receive(request)
    assert engine.pass_due() == []
    assert len(ticket.inputs) == 1
    assert state.replies[0].ticket_id is None
    assert "找不到可续接的任务 #ABCDEF01" in state.replies[0].text
    with pytest.raises(CoordinationError, match="TASK_REFERENCE_ALREADY_HANDLED"):
        engine.route(request.id, result())


def test_numbered_followup_reuses_the_active_assignment() -> None:
    engine, state = setup()
    ticket = engine.ticket(accept(engine, state))
    ticket.task_id = "abcdef01-0000-4000-8000-000000000001"
    ticket.assignments[0].run_id = "12345678-0000-4000-8000-000000000001"
    request = replace(message("m2", addressed=True), text=f"#{ticket.task_id} 继续修复", threads=["chat:m2"])

    assert engine.receive(request)
    assert not engine.receive(request)
    assert engine.pass_due() == []
    assert len(state.tickets) == len(ticket.assignments) == 1
    assert [item.trigger_id for item in ticket.inputs] == ["m1", "m2"]
    assert state.replies[0].ticket_id == ticket.id


def peer_message(identity: str = "peer") -> Trigger:
    return replace(
        message(identity, actor="another-member"), actor_open_id="ou_b",
        text="刚复现了，是页面 2 白屏，页面 1 是笔误",
    )


def test_peer_correction_reaches_original_worker_and_handback_cannot_create_task() -> None:
    engine, state = setup("off")
    ticket_id = accept(engine, state, addressed=True)
    ticket = engine.ticket(ticket_id)
    original = ticket.mandate
    correction = peer_message()

    assert engine.receive(correction)
    assert not engine.receive(correction)
    assert len(ticket.inputs) == 2 and ticket.inputs[-1].peer_evidence
    assert engine.owners(correction) == [ticket_id]
    assert Engine.peer_evidence_only(ticket) and ticket.mandate == original
    assert not engine.handback(ticket_id, correction.id, "经核对是另一个功能")
    with pytest.raises(CoordinationError, match="PEER_EVIDENCE_ONLY"):
        engine.route(correction.id, result())
    assert engine.pass_due() == [] and len(state.tickets) == 1


def test_peer_evidence_requests_original_author_and_cannot_clear_human_wait() -> None:
    engine, state = setup()
    ticket_id = accept(engine, state, addressed=True)
    ticket = engine.ticket(ticket_id)
    assignment = ticket.assignments[-1].id
    assert engine.receive(peer_message())
    engine.mark_input(ticket_id, assignment, ticket.inputs[-1].id, read=True)
    engine.report(ticket_id, assignment, Report(
        "blocked", "页面 2 涉及另一个流程", NOW,
        human_action="请原发起人确认是否将目标改为页面 2", question_trigger_id="peer",
    ))
    assert ticket.attention_trigger_id == ticket.origin_id
    assert state.replies[-1].purpose == "attention" and state.replies[-1].trigger_id == ticket.origin_id

    assert engine.receive(replace(peer_message("peer_yes"), text="我确认，直接修改并发布"))
    assert Engine.waits_for_person(ticket) and Engine.peer_evidence_only(ticket)
    assert engine.receive(replace(message("owner_yes"), text="确认改查页面 2，请先只读验证"))
    assert not Engine.waits_for_person(ticket) and not Engine.peer_evidence_only(ticket)
    assert engine.pass_due() == [] and len(state.tickets) == 1


def test_peer_evidence_at_completion_boundary_reopens_only_original_ticket() -> None:
    engine, state = setup()
    ticket_id = accept(engine, state)
    ticket = engine.ticket(ticket_id)
    original = ticket.assignments[-1].id
    engine.mark_input(ticket_id, original, ticket.inputs[0].id, read=True)
    assert engine.receive(peer_message())
    engine.mark_input(ticket_id, original, ticket.inputs[-1].id)
    engine.report(ticket_id, original, Report("ready", "页面 1 未复现", NOW, verified=True))
    assert ticket.ended_at and ticket.inputs[-1].delivered_to is None
    assert engine.pass_due() == []
    assert not ticket.ended_at and len(ticket.assignments) == 2 and len(state.tickets) == 1


def test_peer_evidence_is_not_attached_from_another_thread_or_after_human_stop() -> None:
    engine, state = setup()
    ticket_id = accept(engine, state, addressed=True)
    ticket = engine.ticket(ticket_id)
    assert not engine.receive(replace(peer_message("different"), threads=["chat:different"]))
    assert len(ticket.inputs) == 1
    assert engine.receive(message("stop", addressed=True))
    engine.stop(ticket_id, "stop")
    assert not engine.receive(peer_message("after_stop"))
    assert state.triggers[-1].filtered == "peer_task_unavailable"
    assert engine.pass_due() == [] and ticket.ended_by == "human" and len(state.tickets) == 1


def test_peer_message_queued_during_acceptance_goes_to_the_newly_bound_topic() -> None:
    engine, state = setup()
    assert engine.receive(message(addressed=True))
    evaluation = engine.pass_due()[0]
    # 已获接单权限的另一个账号在接单结果返回前补充，同一话题不应排出第二个任务。
    assert engine.receive(replace(peer_message(), actor_open_id="ou_a"))
    engine.read_eval(evaluation.id)
    assert engine.decide(evaluation.id, result(), route=True).ticket_id
    assert engine.pass_due() == []
    assert len(state.tickets) == 1 and state.tickets[0].inputs[-1].peer_evidence


def test_peer_message_on_a_topic_with_multiple_tasks_requires_an_exact_quote() -> None:
    engine, state = setup()
    first = engine.ticket(accept(engine, state))
    assert engine.receive(replace(message("m2", addressed=True), threads=["chat:other"]))
    other = engine.route("m2", result()).ticket_id
    assert other
    thread = engine.r.threads.get("chat:root")
    assert thread
    thread.subscriptions.append(other)
    engine.r.threads.save(thread)

    assert not engine.receive(peer_message("ambiguous"))
    assert state.triggers[-1].filtered == "peer_topic_ambiguous"
    assert engine.receive(replace(peer_message("quoted"), origin={"quote_id": "m2"}))
    assert engine.ticket(other).inputs[-1].trigger_id == "quoted"
    assert len(first.inputs) == 1 and engine.pass_due() == [] and len(state.tickets) == 2


def test_human_stop_cannot_be_revived_by_new_messages_or_retry() -> None:
    engine, state = setup()
    ticket_id = accept(engine, state)
    engine.receive(message("stop", addressed=True))
    engine.stop(ticket_id, "stop")
    engine.receive(message("later"))
    assert state.tickets[0].ended_by == "human"
    assert len(state.tickets[0].inputs) == 2
    with pytest.raises(CoordinationError, match="HUMAN_STOPPED"):
        engine.retry(ticket_id, "stop")


def test_context_only_followup_does_not_borrow_authority() -> None:
    engine, state = setup()
    ticket_id = accept(engine, state)
    other = message("other")
    other.threads = ["chat:other"]
    engine.receive(other)
    engine.follow(ticket_id, "chat:other")
    engine.receive(replace(other, id="context", text="顺便执行删除"))
    assert state.tickets[0].inputs[-1].context_only
    assert engine.owners(state.triggers[-1]) == []
    with pytest.raises(CoordinationError, match="OWN_THREAD_REQUIRED"):
        engine.unsubscribe(ticket_id, "chat:root")


def test_policy_change_rejects_stale_decision_and_reassesses() -> None:
    engine, state = setup()
    engine.receive(message())
    evaluation = engine.pass_due()[0]
    engine.read_eval(evaluation.id)
    engine.config = replace(engine.config, mode="observe")
    with pytest.raises(CoordinationError, match="POLICY_CHANGED"):
        engine.decide(evaluation.id, result())
    fresh = engine.pass_due()[0]
    assert fresh.id != evaluation.id
    assert fresh.policy_version == policy_version(engine.config)


def test_eval_timeout_starts_after_delivery_and_requeues_twice() -> None:
    engine, state = setup()
    engine.receive(message())
    evaluation = engine.pass_due()[0]
    engine.now = lambda: "2026-11-04T10:00:00+00:00"
    assert engine.pass_due()[0].id == evaluation.id
    engine.delivered(evaluation.id)
    for day in (5, 6):
        engine.now = lambda day=day: f"2026-11-0{day}T10:00:00+00:00"
        evaluation = engine.pass_due()[0]
        engine.delivered(evaluation.id)
    engine.now = lambda: "2026-11-07T10:00:00+00:00"
    assert engine.pass_due() == []
    assert state.decisions[-1].kind == "unavailable"


def test_ready_requires_platform_validation_and_stale_assignment_is_rejected() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    assignment = state.tickets[0].assignments[0]
    with pytest.raises(CoordinationError, match="COMPLETION_NOT_VERIFIED"):
        engine.report(identity, assignment.id, Report("ready", "好了", NOW))
    engine.mark_input(identity, assignment.id, state.tickets[0].inputs[0].id, read=True)
    engine.report(identity, assignment.id, Report("ready", "验证通过", NOW, verified=True))
    assert state.tickets[0].outcome == "ready"
    engine.receive(message("next"))
    assert len(state.tickets[0].assignments) == 2
    with pytest.raises(CoordinationError, match="STALE_ASSIGNMENT"):
        engine.report(identity, assignment.id, Report("failed", "迟到结果", NOW))


def test_ack_once_and_reply_cannot_change_channel_or_fabricate_answer() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    original = engine.reply("m1", "ack", "收到", ticket_id=identity)
    assert engine.reply("m1", "ack", "不覆盖原文", ticket_id=identity).text == "收到"
    assert len(state.replies) == 1
    assert state.replies[0].id == original.id
    with pytest.raises(CoordinationError, match="RESULT_NOT_READY"):
        engine.reply("m1", "result", "已完成", ticket_id=identity)
    with pytest.raises(CoordinationError, match="ANSWER_NOT_REPORTED"):
        engine.reply("m1", "answer", "未经 Worker 确认", ticket_id=identity)


@pytest.mark.parametrize("mode", ["off", "observe", "auto"])
def test_listed_mention_explicit_works_in_all_modes(mode: str) -> None:
    engine, state = setup(mode)
    assert gate(engine.config, message(addressed=True)).strength == "explicit"
    accept(engine, state, addressed=True)
    assert len(state.tickets) == 1


def test_bots_never_obtain_explicit_stop_authority() -> None:
    engine, _ = setup()
    engine.config = replace(engine.config, allowed_bots=("ou_a",))
    bot = replace(message(addressed=True), human=False)
    assert gate(engine.config, bot).strength == "inferred"


def test_late_followup_survives_completion_race() -> None:
    engine, state = setup()
    ticket_id = accept(engine, state)
    ticket = state.tickets[0]
    original = ticket.assignments[0].id
    engine.mark_input(ticket_id, original, ticket.inputs[0].id, read=True)
    engine.receive(message("late"))
    engine.report(ticket_id, original, Report("ready", "首轮已完成", NOW, verified=True))
    assert ticket.ended_at and len(ticket.assignments) == 1
    assert ticket.inputs[-1].read_at is None
    assert engine.pass_due()[0].primary == "late"
    assert not engine.owners(state.triggers[-1])


def test_question_omission_keeps_null_clears_and_withdraws_outbox() -> None:
    engine, state = setup()
    ticket_id = accept(engine, state)
    assignment_id = state.tickets[0].assignments[0].id
    engine.report(ticket_id, assignment_id, Report("blocked", "等确认", NOW, human_action="请选择范围"))
    reply = engine.reply("m1", "attention", "请选择范围", ticket_id=ticket_id)
    engine.report(ticket_id, assignment_id, Report("waiting", "仍在等", NOW))
    assert state.tickets[0].attention == "请选择范围"
    engine.report(ticket_id, assignment_id, Report("progress", "继续", NOW, human_action_supplied=True))
    assert state.tickets[0].attention is None and reply.state == "withdrawn"


def test_omitting_ticket_id_does_not_bypass_worker_answer_requirement() -> None:
    engine, state = setup()
    accept(engine, state)
    with pytest.raises(CoordinationError, match="ANSWER_NOT_REPORTED"):
        engine.reply("m1", "answer", "绕过 Worker 的回答")


def test_unmentioned_stop_preserves_other_assessment_and_accepted_message_never_restarts() -> None:
    engine, state = setup()
    # 代码来源仍可独立评估同线程不同作者；飞书同话题现在直接把他人的回复交给原任务。
    engine.config = replace(engine.config, source_kind="code_hosting")
    identity = accept(engine, state, addressed=True)
    engine.config = replace(engine.config, allowed_actors=("ou_a", "ou_b"))
    other = replace(message("other", actor="other"), actor_open_id="ou_b")
    engine.receive(other)
    pending = engine.pass_due()[0]
    engine.receive(message("stop-without-mention"))
    decision = engine.route("stop-without-mention", {"decision": "stop", "reason": "人类停止", "ticket_id": identity})
    assert decision.kind == "stop" and state.tickets[0].ended_by == "human"
    assert state.threads[0].evaluation_id == pending.id
    assert engine.read_eval(pending.id).state == "pending"
    engine.route("m1", result())
    assert len(state.tickets) == 1


def test_answer_permission_is_consumed_but_identical_transport_retry_is_allowed() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    assignment_id = state.tickets[0].assignments[0].id
    engine.receive(message("question"))
    engine.mark_input(identity, assignment_id, state.tickets[0].inputs[-1].id, read=True)
    engine.report(identity, assignment_id, Report("progress", "已核实", NOW, answers="question"))
    first = engine.reply("question", "answer", "已核实的回答", ticket_id=identity)
    assert engine.reply("question", "answer", "已核实的回答", ticket_id=identity).id == first.id
    with pytest.raises(CoordinationError, match="ANSWER_NOT_REPORTED"):
        engine.reply("question", "answer", "换措辞重复回答", ticket_id=identity)


def test_report_question_wakes_main_even_in_progress_and_status_is_waiting_only() -> None:
    engine, state = setup()
    identity = accept(engine, state)
    assignment_id = state.tickets[0].assignments[0].id
    engine.report(identity, assignment_id, Report("progress", "需要范围", NOW, human_action="请选择仓库"))
    assert state.notices[-1].quiet is False
    with pytest.raises(CoordinationError, match="WORKER_NOT_IDLE"):
        engine.status(identity)
    engine.report(identity, assignment_id, Report("waiting", "等待下一条人类输入", NOW))
    engine.status(identity)
    assert state.tickets[0].status_requests == 1


def test_expired_outcome_is_refused_before_maintenance_tick() -> None:
    engine, _ = setup()
    engine.receive(message())
    evaluation = engine.pass_due()[0]
    engine.delivered(evaluation.id)
    engine.read_eval(evaluation.id)
    engine.now = lambda: "2026-11-03T10:02:01+00:00"
    with pytest.raises(CoordinationError, match="EVAL_EXPIRED"):
        engine.decide(evaluation.id, result())
