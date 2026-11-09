"""独立仓库已有讨论的补读：PR/CI 没有更新仓库消息游标时，也继续跟进已订阅线程。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from weora.modules.context.domain import ContextError
from weora.modules.coordination.domain import CoordinationError, Json, State, Trigger
from weora.modules.coordination.engine import Engine, key
from weora.modules.coordination.hosting import ReviewSnapshot
from weora.modules.coordination.hosting_http import HostedDiscussion
from weora.modules.coordination.hosting_poll import CodePolling
from weora.modules.coordination.records import records
from weora.modules.delivery.domain.models import DeliveryError

if TYPE_CHECKING:
    from weora.modules.coordination.repository_source import RepositoryIntake


@dataclass(frozen=True)
class DiscussionRound:
    observations: tuple[tuple[str, str, str, ReviewSnapshot], ...]
    failures: tuple[tuple[str, str], ...]
    next_offset: int


class RepositoryDiscussions:
    """只读已授权并已订阅的同作者讨论；补读事实不能成为新的人类实施授权。"""

    def __init__(self, intake: "RepositoryIntake") -> None:
        self.intake = intake

    @staticmethod
    def bindings(state: State, *, pull_only: bool = True) -> list[tuple[str, str, Trigger]]:
        found: list[tuple[str, str, Trigger]] = []
        for ticket in state.tickets:
            if not CodePolling._eligible(state, ticket):
                continue
            for follow in ticket.follows:
                if follow.review_id:
                    continue  # Weora 交付的评审由原生交付轮询负责，不重复读取。
                trigger = next(
                    (
                        item
                        for item in state.triggers
                        if item.authorized
                        and (not pull_only or item.origin.get("is_pull") is True)
                        and str(item.origin.get("number", "")).isdecimal()
                        and follow.thread in item.threads
                        and item.actor_id == ticket.mandate.actor_id
                    ),
                    None,
                )
                if trigger:
                    found.append((ticket.id, follow.thread, trigger))
        return sorted(found, key=lambda item: (item[1], item[0]))

    async def read(self, workspace_id: UUID, state: State) -> DiscussionRound:
        bindings = self.bindings(state)
        if not bindings:
            return DiscussionRound((), (), 0)
        offset = int(str(state.checkpoints.get("repository:discussion:offset", 0))) % len(bindings)
        selected = (bindings[offset:] + bindings[:offset])[:20]
        observations: list[tuple[str, str, str, ReviewSnapshot]] = []
        failures: list[tuple[str, str]] = []
        for ticket_id, thread, trigger in selected:
            if str(state.checkpoints.get(f"code_poll:due:{thread}", "")) > datetime.now(UTC).isoformat():
                continue
            try:
                identity = await self.intake.authorize(workspace_id, state.config, trigger)
                locator = HostedDiscussion(
                    uuid5(NAMESPACE_URL, state.config.id + ":" + thread),
                    str(trigger.origin["number"]),
                    str(trigger.origin["url"]),
                )
                source = state.config.repository_source
                assert source
                cursor = cast(
                    Json, state.checkpoints.get(f"repository:discussion:{thread}", {"since": source.started_at})
                )
                snapshot = await self.intake.reader.discussion(identity, locator, cursor)
                observations.append((ticket_id, thread, trigger.id, snapshot))
            except (CoordinationError, ContextError, DeliveryError) as error:
                failures.append((thread, error.code))
        return DiscussionRound(tuple(observations), tuple(failures), (offset + len(selected)) % len(bindings))

    @staticmethod
    def apply(engine: Engine, state: State, result: DiscussionRound) -> None:
        state.checkpoints["repository:discussion:offset"] = result.next_offset
        for thread, code in result.failures:
            CodePolling._failed(thread, code, None, engine, state)
        for ticket_id, thread, trigger_id, snapshot in result.observations:
            trigger = records(state).triggers.get(trigger_id)
            ticket = records(state).tickets.get(ticket_id)
            if not trigger or not trigger.authorized or not ticket or not CodePolling._eligible(state, ticket):
                continue
            follow = next((item for item in ticket.follows if item.thread == thread), None)
            if not follow:
                continue
            for event in snapshot.events:
                # 大正文拆成有界、可去重的自动备注；Worker 仍可按 input 引用读取完整片段。
                body = event.text[:24000]
                if len(event.text) > len(body):
                    body += "\n[提供方事实过长，已明确截断；不能据此宣称完整读取]"
                chunks = [body[index : index + 3800] for index in range(0, len(body), 3800)]
                for index, text in enumerate(chunks):
                    identity = key("repository-fact", event.id, str(index))
                    if any(item.id == key(ticket_id, "note", identity) for item in ticket.inputs):
                        continue
                    engine.note(
                        ticket_id, f"[代码托管自动事实 {event.kind} {index + 1}/{len(chunks)}]\n" + text, identity
                    )
            follow.lifecycle = True
            engine.observe_review(ticket_id, thread, snapshot.outcome, snapshot.at, closed_at=snapshot.closed_at)
            state.checkpoints[f"repository:discussion:{thread}"] = snapshot.cursor
            state.checkpoints.pop(f"code_poll:error:{thread}", None)
            state.checkpoints.pop(f"code_poll:due:{thread}", None)
