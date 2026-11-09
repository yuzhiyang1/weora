"""补读的事务不变量：单源故障隔离，事件幂等，旧租约不能推进游标。"""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from tests.coordination.test_engine import message, result, setup
from tests.delivery.test_service import scope
from weora.modules.coordination.domain import Config, CoordinationError, Effect, State
from weora.modules.coordination.engine import Engine
from weora.modules.coordination.hosting import CodeEvent, ReviewSnapshot
from weora.modules.coordination.hosting_poll import CodePolling
from weora.modules.coordination.ports import CoordinationStore
from weora.modules.coordination.records import records
from weora.modules.delivery.application.ports import ReviewReference
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.runs.application.ports import RunActor


class MemoryCoordination:
    def __init__(self, state: State) -> None:
        self.state = state

    async def transact[T](
        self, workspace_id: UUID, scope_id: str, action: Callable[[Engine, State], T], *, create: Config | None = None
    ) -> T:
        return action(Engine(records(self.state), self.state.config, lambda: datetime.now(UTC).isoformat()), self.state)


@pytest.mark.anyio
async def test_bad_review_does_not_block_healthy_source_and_duplicate_event_does_not_wake_again() -> None:
    engine, state = setup()
    actor, workspace_id = uuid4(), uuid4()
    references: dict[UUID, ReviewReference] = {}
    for index in (1, 2):
        trigger = replace(message(f"message-{index}"), actor_id=str(actor), threads=[f"thread:{index}"])
        engine.receive(trigger)
        evaluation = engine.pass_due()[0]
        engine.delivered(evaluation.id)
        engine.read_eval(evaluation.id)
        decision = engine.decide(evaluation.id, result())
        ticket_id = str(decision.ticket_id)
        run_id, review_id = uuid4(), uuid4()
        engine.bind_review(ticket_id, f"code:{index}", str(run_id), str(review_id))
        references[run_id] = ReviewReference(
            run_id, review_id, scope(), str(index), f"https://github.com/team/repo/pull/{index}"
        )

    class Delivery:
        async def review_references(self, actor: RunActor, run_id: UUID) -> list[ReviewReference]:
            return [references[run_id]]

    class Reader:
        async def authorize(self, actor: RunActor, reference: ReviewReference) -> None:
            return None

        async def read(self, actor: RunActor, reference: ReviewReference, cursor: dict[str, object]) -> ReviewSnapshot:
            if reference.remote_id == "1":
                raise CoordinationError("CODE_SOURCE_UNAVAILABLE")
            at = datetime.now(UTC).isoformat()
            return ReviewSnapshot("open", at, (CodeEvent("event-2", "ci", "测试失败", at),), {"since": at})

    async def authorized(workspace: UUID, scope_id: str) -> State:
        return state

    store = MemoryCoordination(state)
    poller = CodePolling(cast(CoordinationStore, store), authorized, cast(DeliveryService, Delivery()), Reader())
    with pytest.raises(CoordinationError, match="CODE_SOURCE_PARTIAL_FAILURE"):
        await poller.poll(workspace_id, "scope")
    assert "code_poll:cursor:code:1" not in state.checkpoints
    assert state.checkpoints["code_poll:cursor:code:2"]
    assert (
        len(state.tickets[1].inputs) == 2
        and state.tickets[1].inputs[-1].text == "代码托管事实（不增加权限），event_id=event-2：测试失败"
    )
    first_count = len(state.tickets[1].inputs)
    state.checkpoints["code_poll:due:code:2"] = "2000-01-01T00:00:00+00:00"
    state.checkpoints["code_poll:due:code:1"] = "2999-01-01T00:00:00+00:00"
    await poller.poll(workspace_id, "scope")
    assert len(state.tickets[1].inputs) == first_count


def test_late_effect_cannot_write_events_or_advance_cursor() -> None:
    engine, state = setup()
    old = Effect(
        "effect", "code_poll", "scope", {}, state="running", owner="old", lease_until="2999-01-01T00:00:00+00:00"
    )
    state.effects.append(replace(old, owner="new", generation=2))
    with pytest.raises(CoordinationError, match="SESSION_LEASE_LOST"):
        CodePolling._apply(
            "missing", "thread", "review", "run", ReviewSnapshot("open", engine.now(), (), {}), old, engine, state
        )
    assert not state.checkpoints
