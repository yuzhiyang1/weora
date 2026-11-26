"""飞书控制台入口用真实 HTTP 和持久协调记录验证，不调用真实飞书或模型。"""

from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from httpx import Response

from tests.api.test_runs import runs as runs
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.coordination.test_service import configure, receive
from weora.modules.coordination.domain import CoordinationError, Effect, State
from weora.modules.coordination.engine import Engine
from weora.modules.coordination.service import CoordinationService
from weora.modules.coordination.worker_reads import worker_facts
from weora.modules.runs.application.harness import PI_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore

pytestmark = pytest.mark.anyio
type FeishuRun = tuple[ContextRuns, CoordinationService, str, UUID]


@pytest.fixture
async def feishu_run(contextual: ContextRuns, monkeypatch: pytest.MonkeyPatch) -> FeishuRun:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace = h.actor.workspace_id
    profile = ExecutionProfile(PI_RUNTIME, "deepseek", "test-model", None, 30)
    monkeypatch.setattr(coordinator.runs, "_profile_provider", AsyncMock(return_value=profile))
    await receive(h, coordinator, addressed=True)
    decision = await coordinator.tool(workspace, scope_id, "route", "orchestrator_route", {"decisions": [{
        "trigger_id": "om_trigger", "decision": "accept", "title": "核对登录", "scope": "修复登录",
        "reason": "发起人明确请求",
    }]})
    assert cast(list[dict[str, object]], decision["results"])[0]["ok"] is True, decision
    state = await coordinator.store.snapshot(workspace, scope_id)
    await coordinator.launch(workspace, scope_id, next(effect for effect in state.effects if effect.kind == "worker"))
    state = await coordinator.store.snapshot(workspace, scope_id)
    run_id = UUID(str(state.tickets[0].assignments[0].run_id))
    return h, coordinator, scope_id, run_id


async def submit(h: ContextRuns, run_id: UUID, mode: str, key: str, text: str = "保留好友登录入口") -> Response:
    return await h.client.post(
        f"{h.base}/runs/{run_id}/continue", json={"mode": mode, "instruction": text},
        headers={**h.headers, "Idempotency-Key": key},
    )


def worker(effects: list[Effect]) -> Effect:
    return next(effect for effect in effects if effect.kind == "worker")


async def test_continue_is_bound_to_next_run_and_steer_to_current_lease(feishu_run: FeishuRun) -> None:
    h, coordinator, scope_id, run_id = feishu_run
    workspace = h.actor.workspace_id
    execution = cast(SqlAlchemyExecutionStore, h.app.state.execution_store)
    lease = await execution.claim(workspace_id=workspace, run_id=run_id, owner="web-console-test", lease_seconds=120)
    assert lease

    continuation = await submit(h, run_id, "continue", "next")
    assert continuation.status_code == 202, continuation.text
    pending = continuation.json()
    assert (pending["status"], pending["run_id"]) == ("PENDING", None)
    assert (await submit(h, run_id, "continue", "next")).json() == pending
    assert (await submit(h, run_id, "continue", "next", "不同的续接内容")).status_code == 409

    def hand_back_console_input(engine: Engine, state: State) -> None:
        ticket = state.tickets[0]
        item = next(item for item in ticket.inputs if item.id == pending["input_id"])
        assert item.trigger_id is not None
        with pytest.raises(CoordinationError, match="CONSOLE_INPUT_BOUND"):
            engine.handback(ticket.id, item.trigger_id, "不能另开任务")
        with pytest.raises(CoordinationError, match="CONSOLE_INPUT_BOUND"):
            engine.route(item.trigger_id, {"decision": "stop", "ticket_id": ticket.id, "reason": "不能改变操作"})

    await coordinator.store.transact(workspace, scope_id, hand_back_console_input)

    steering = await submit(h, run_id, "steer", "now", "先核对登录日志")
    assert steering.status_code == 202, steering.text
    assert steering.json()["status"] == "STEERING"
    state = await coordinator.store.snapshot(workspace, scope_id)
    ticket = state.tickets[0]
    thread = {"resource": "ticket"}
    visible = worker_facts(state, ticket.id, thread, run_id=str(run_id), fence=lease.fencing_token)
    ids = {item["id"] for item in cast(list[dict[str, object]], visible["inputs"])}
    assert steering.json()["input_id"] in ids and pending["input_id"] not in ids
    with pytest.raises(CoordinationError, match="EVIDENCE_NOT_DELIVERED"):
        worker_facts(state, ticket.id, {"resource": "input", "id": steering.json()["input_id"]},
                     run_id=str(run_id), fence=lease.fencing_token + 1)

    assert await execution.finish(lease, status="SUCCEEDED", output="原轮已处理")
    await coordinator.reconcile(workspace, scope_id)
    state = await coordinator.store.snapshot(workspace, scope_id)
    await coordinator.launch(workspace, scope_id, worker(state.effects))
    state = await coordinator.store.snapshot(workspace, scope_id)
    next_run = state.tickets[0].assignments[-1].run_id
    assert next_run and next_run != str(run_id) and state.tickets[0].task_id == pending["task_id"]
    history = await h.client.get(f"{h.base}/runs/{run_id}/conversation")
    assert history.status_code == 200 and history.headers["Cache-Control"] == "no-store"
    inputs = {item["id"]: item for item in history.json()["inputs"]}
    assert (inputs[pending["input_id"]]["status"], inputs[pending["input_id"]]["run_id"]) == ("DISPATCHED", next_run)
    assert inputs[steering.json()["input_id"]]["status"] == "MISSED"
    assert history.json()["can_send"] is False


async def test_stop_requires_explicit_resume_and_retains_task_and_run_history(feishu_run: FeishuRun) -> None:
    h, coordinator, scope_id, run_id = feishu_run
    workspace = h.actor.workspace_id
    assert (await submit(h, run_id, "resume", "too-soon")).status_code == 409
    cancelled = await h.client.post(f"{h.base}/runs/{run_id}/cancel", headers=h.headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    state = await coordinator.store.snapshot(workspace, scope_id)
    assert state.tickets[0].ended_by == "human"
    assert state.tickets[0].assignments[-1].end_reason == "human_stopped"
    for action in ("continue", "retry"):
        assert (await submit(h, run_id, action, action)).status_code == 409
    conversation = await h.client.get(f"{h.base}/runs/{run_id}/conversation")
    assert conversation.json()["can_resume"] and not conversation.json()["can_retry"]

    resumed = await submit(h, run_id, "resume", "explicit-resume", "#原任务 补齐验收")
    assert resumed.status_code == 202, resumed.text
    assert (await submit(h, run_id, "resume", "explicit-resume", "#原任务 补齐验收")).json() == resumed.json()
    state = await coordinator.store.snapshot(workspace, scope_id)
    ticket = state.tickets[0]
    assert len(ticket.assignments) == 2 and ticket.ended_by is None
    assert ticket.assignments[0].end_reason == "human_stopped"
    await coordinator.launch(workspace, scope_id, worker(state.effects))
    state = await coordinator.store.snapshot(workspace, scope_id)
    assert state.tickets[0].task_id == ticket.task_id
    assert state.tickets[0].assignments[0].run_id == str(run_id)
    assert state.tickets[0].assignments[1].run_id not in {None, str(run_id)}


async def test_unknown_delivery_blocks_retry_and_revocation_closes_conversation(feishu_run: FeishuRun) -> None:
    h, coordinator, scope_id, run_id = feishu_run
    workspace = h.actor.workspace_id
    execution = cast(SqlAlchemyExecutionStore, h.app.state.execution_store)
    lease = await execution.claim(workspace_id=workspace, run_id=run_id, owner="retry-test", lease_seconds=120)
    assert lease and await execution.finish(lease, status="FAILED", failure_code="MODEL_FAILED")
    await coordinator.reconcile(workspace, scope_id)

    def delivery_unknown(_engine: Engine, state: State) -> None:
        state.tickets[0].delivery_error = "DELIVERY_RESULT_UNKNOWN"

    await coordinator.store.transact(workspace, scope_id, delivery_unknown)
    rejected = await submit(h, run_id, "retry", "retry-once")
    assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "CHANNEL_RECONCILIATION_REQUIRED"

    def delivery_reconciled(_engine: Engine, state: State) -> None:
        state.tickets[0].delivery_error = None

    await coordinator.store.transact(workspace, scope_id, delivery_reconciled)
    conversation = await h.client.get(f"{h.base}/runs/{run_id}/conversation")
    assert conversation.json()["can_retry"] and not conversation.json()["can_resume"]
    accepted = await submit(h, run_id, "retry", "retry-once")
    assert accepted.status_code == 202, accepted.text
    assert (await submit(h, run_id, "retry", "retry-once")).json() == accepted.json()
    state = await coordinator.store.snapshot(workspace, scope_id)
    assert len(state.tickets[0].assignments) == 2 and state.tickets[0].assignments[-1].refresh_model

    await h.context.set_grant(h.context_actor, h.source_id, h.actor.user_id, False)
    assert (await h.client.get(f"{h.base}/runs/{run_id}/conversation")).status_code == 404
    assert (await submit(h, run_id, "retry", "retry-once")).status_code == 404
