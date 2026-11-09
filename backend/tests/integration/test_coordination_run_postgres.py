"""真实 PostgreSQL 验证协调域到原生 Task/Run、回执与续接，覆盖 SQLite 不限制的字段长度。"""

import asyncio

import pytest

from tests.api.test_runs import RunsHarness
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.coordination.test_service import configure, decide, receive
from tests.coordination.test_service import (
    test_unmentioned_inbox_decision_worker_run_report_reply_and_task_continuation as acceptance,
)
from tests.integration.test_approval_recovery_postgres import PostgresHarness
from tests.integration.test_approval_recovery_postgres import postgres_runs as postgres_runs
from weora.modules.coordination.domain import CoordinationError, State
from weora.modules.coordination.engine import Engine

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def runs(postgres_runs: PostgresHarness) -> RunsHarness:
    return postgres_runs.runs


async def test_postgres_native_task_launch_continuation_receipt_and_retention(contextual: ContextRuns) -> None:
    await acceptance(contextual, root_present=False)


async def test_outbox_reserves_send_once_and_rejects_expired_owner(contextual: ContextRuns) -> None:
    h = contextual
    coordinator, scope_id = await configure(h)
    workspace_id = h.context_actor.workspace_id
    await receive(h, coordinator)
    ticket_id = await decide(h, coordinator, scope_id)
    answer = await coordinator.tool(
        workspace_id,
        scope_id,
        "ack",
        "orchestrator_reply",
        {"trigger_id": "om_trigger", "ticket_id": ticket_id, "purpose": "ack", "text": "收到"},
    )
    assert answer["ok"] is True
    state = await coordinator.store.snapshot(workspace_id, scope_id)
    pending = next(item for item in state.effects if item.kind == "reply")
    old_owner = await coordinator.store.claim(workspace_id, scope_id, pending.id, "expired-owner")
    assert old_owner

    def expire(_: Engine, state: State) -> None:
        next(item for item in state.effects if item.id == pending.id).lease_until = "2000-01-01T00:00:00+00:00"

    await coordinator.store.transact(workspace_id, scope_id, expire)
    owner = await coordinator.store.claim(workspace_id, scope_id, pending.id, "current-owner")
    assert owner
    with pytest.raises(CoordinationError, match="SESSION_LEASE_LOST"):
        await coordinator.deliver(workspace_id, scope_id, old_owner)
    assert h.reader.replies == 0
    sent = await asyncio.gather(*(coordinator.deliver(workspace_id, scope_id, owner) for _ in range(4)))
    assert sent.count(True) == 1 and h.reader.replies == 1
