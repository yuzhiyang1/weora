"""已绑定代码线程的持久补偿；一个评审失败不阻塞其他任务，失败游标不前移。"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import cast
from uuid import UUID

from weora.modules.coordination.domain import CoordinationError, Effect, Follow, Json, State, Ticket
from weora.modules.coordination.engine import Engine, key, later
from weora.modules.coordination.hosting import CodeEventReader, ReviewSnapshot
from weora.modules.coordination.ports import CoordinationStore
from weora.modules.coordination.records import records
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.runs.application.ports import RunActor


class CodePolling:
    def __init__(
        self,
        store: CoordinationStore,
        authorized: Callable[[UUID, str], Awaitable[State]],
        delivery: DeliveryService,
        reader: CodeEventReader,
    ) -> None:
        self.store, self.authorized, self.delivery, self.reader = store, authorized, delivery, reader

    async def poll(self, workspace_id: UUID, scope_id: str, effect: Effect | None = None) -> None:
        """逐线程读取并独立提交；到期线程跳过，外部失败记录退避，旧执行者不能改新游标。"""
        state = await self.authorized(workspace_id, scope_id)
        failed = False
        seen: set[str] = set()
        for ticket in state.tickets:
            if not self._eligible(state, ticket):
                continue
            for follow in ticket.follows:
                if not follow.lifecycle or not follow.review_run_id or not follow.review_id or follow.thread in seen:
                    continue
                seen.add(follow.thread)
                if str(state.checkpoints.get(f"code_poll:due:{follow.thread}", "")) > datetime.now(UTC).isoformat():
                    continue
                try:
                    await self._one(workspace_id, scope_id, state, ticket, follow, effect)
                except (CoordinationError, DeliveryError) as error:
                    if error.code in {"SESSION_LEASE_LOST", "EFFECT_LEASE_LOST"}:
                        raise
                    failed = True
                    await self.store.transact(
                        workspace_id,
                        scope_id,
                        partial(self._failed, follow.thread, error.code, effect),
                    )
        await self.store.transact(workspace_id, scope_id, partial(self._advance, effect))
        if failed:
            raise CoordinationError("CODE_SOURCE_PARTIAL_FAILURE")

    @staticmethod
    def _eligible(state: State, ticket: Ticket) -> bool:
        """活跃及结束十四天内继续观察；正文权限和 TTL 优先于补读保留窗口。"""
        origin = records(state).triggers.get(ticket.origin_id)
        return bool(
            origin
            and origin.authorized
            and ticket.ended_by != "human"
            and (
                not ticket.ended_at or datetime.fromisoformat(ticket.ended_at) >= datetime.now(UTC) - timedelta(days=14)
            )
        )

    async def _one(
        self,
        workspace_id: UUID,
        scope_id: str,
        state: State,
        ticket: Ticket,
        follow: Follow,
        effect: Effect | None,
    ) -> None:
        actor = RunActor(UUID(ticket.mandate.actor_id), workspace_id)
        references = await self.delivery.review_references(actor, UUID(str(follow.review_run_id)))
        reference = next((item for item in references if str(item.record_id) == follow.review_id), None)
        if not reference:
            raise CoordinationError("CODE_SOURCE_REVIEW_MISMATCH")
        cursor = cast(Json, state.checkpoints.get(f"code_poll:cursor:{follow.thread}", {}))
        observed = await self.reader.read(actor, reference, cursor)
        await self.authorized(workspace_id, scope_id)
        await self.store.transact(
            workspace_id,
            scope_id,
            partial(
                self._apply,
                ticket.id,
                follow.thread,
                str(follow.review_id),
                str(follow.review_run_id),
                observed,
                effect,
            ),
        )

    @staticmethod
    def _apply(
        ticket_id: str,
        thread_id: str,
        review_id: str,
        run_id: str,
        observed: ReviewSnapshot,
        effect: Effect | None,
        engine: Engine,
        state: State,
    ) -> None:
        """完整事件与小摘要同存；游标只在绑定仍有效、事件落库成功后推进。"""
        if effect:
            state.require_effect_lease(effect, engine.now())
        owner = engine.ticket(ticket_id)
        if not CodePolling._eligible(state, owner):
            return
        if not any(item.thread == thread_id and item.review_id == review_id for item in owner.follows):
            return
        thread = records(state).threads.get(thread_id)
        subscribers = thread.subscriptions if thread else [ticket_id]
        for identity in subscribers:
            ticket = engine.ticket(identity)
            # 与飞书跟进相同：上下文不能让别人的任务继承原请求人的执行身份。
            if ticket.mandate.actor_id != owner.mandate.actor_id or not CodePolling._eligible(state, ticket):
                continue
            for event in observed.events:
                state.checkpoints[f"code_event:{identity}:{event.id}"] = {
                    "id": event.id,
                    "ticket_id": identity,
                    "thread": thread_id,
                    "review_id": review_id,
                    "run_id": run_id,
                    "kind": event.kind,
                    "text": event.text,
                    "at": event.at,
                }
                engine.note(
                    identity, f"代码托管事实（不增加权限），event_id={event.id}：" + event.text[:3300], event.id
                )
            engine.observe_review(identity, thread_id, observed.outcome, observed.at, closed_at=observed.closed_at)
        state.checkpoints[f"code_poll:cursor:{thread_id}"] = observed.cursor
        state.checkpoints[f"code_poll:due:{thread_id}"] = later(engine.now(), 60)
        state.checkpoints.pop(f"code_poll:error:{thread_id}", None)

    @staticmethod
    def _failed(thread: str, code: str, effect: Effect | None, engine: Engine, state: State) -> None:
        """每个线程独立退避；只记录稳定代码，不保存提供方响应、令牌或错误原文。"""
        if effect:
            state.require_effect_lease(effect, engine.now())
        previous = cast(Json, state.checkpoints.get(f"code_poll:error:{thread}", {}))
        attempts = int(str(previous.get("attempts", 0))) + 1
        started = str(previous.get("since", engine.now()))
        state.checkpoints[f"code_poll:error:{thread}"] = {"code": code, "attempts": attempts, "since": started}
        state.checkpoints[f"code_poll:due:{thread}"] = later(engine.now(), min(60, 2 ** min(attempts, 6)))
        engine.record_notice("source.code_failed", key(thread, code, started), {"thread": thread, "code": code})

    @staticmethod
    def _advance(effect: Effect | None, engine: Engine, state: State) -> None:
        if effect:
            state.require_effect_lease(effect, engine.now())
        threads = {
            follow.thread
            for ticket in state.tickets
            if CodePolling._eligible(state, ticket)
            for follow in ticket.follows
            if follow.lifecycle
        }
        due = [str(state.checkpoints.get(f"code_poll:due:{thread}", engine.now())) for thread in threads]
        state.checkpoints["code_poll:next_at"] = min(due, default=later(engine.now(), 60))
