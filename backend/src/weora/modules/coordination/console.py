"""飞书协调任务的控制台入口，沿用原发起人、话题、权限和 Worker 回执。"""

from dataclasses import replace
from typing import Literal
from uuid import UUID

from weora.modules.coordination.domain import CoordinationError, Input, State, Ticket, Trigger
from weora.modules.coordination.engine import Engine, key
from weora.modules.coordination.policy import revalidate
from weora.modules.coordination.records import records
from weora.modules.coordination.service import CoordinationService
from weora.modules.runs.application.ports import RunActor, RunDetail, RunRecord
from weora.modules.runs.application.service import RunService
from weora.modules.runs.domain.errors import RunNotFoundError

ConsoleMode = Literal["continue", "steer", "retry"]


class FeishuTaskConsole:
    def __init__(self, service: CoordinationService, actor: RunActor, detail: RunDetail) -> None:
        self.service, self.actor, self.detail = service, actor, detail
        metadata = detail.task.origin_metadata or {}
        self.scope_id = str(metadata.get("coordination_scope_id", ""))
        self.ticket_id = str(metadata.get("coordination_ticket_id", ""))

    def _ticket(self, state: State) -> tuple[Ticket, Trigger]:
        repository = records(state)
        ticket = repository.tickets.get(self.ticket_id)
        origin = repository.triggers.get(ticket.origin_id) if ticket else None
        if (
            state.config.source_kind != "feishu" or not ticket or not origin or not origin.authorized
            or ticket.mandate.actor_id != str(self.actor.user_id) or origin.actor_id != str(self.actor.user_id)
            or ticket.task_id != str(self.detail.task.id) or not ticket.assignments
        ):
            raise RunNotFoundError
        return ticket, origin

    async def _latest(self, ticket: Ticket) -> RunRecord | None:
        run_id = ticket.assignments[-1].run_id
        if not run_id:
            return None
        latest = await self.service.runs.access_metadata(self.actor, UUID(run_id))
        return latest.run if latest else None

    @staticmethod
    def _status(item: Input, trigger: Trigger, latest: RunRecord | None) -> str:
        if trigger.console_mode == "steer":
            if item.read_at:
                return "READ"
            if (
                latest and str(latest.id) == trigger.console_target
                and latest.fencing_token == trigger.console_fence and RunService.accepts_live_input(latest)
            ):
                return "STEERING"
            return "MISSED"
        return "DISPATCHED" if item.run_id else "PENDING"

    async def conversation(self) -> dict[str, object]:
        state = await self.service.authorized_state(self.actor.workspace_id, self.scope_id)
        ticket, origin = self._ticket(state)
        latest = await self._latest(ticket)
        failed = bool(latest and latest.status in {"FAILED", "CANCELLED"}) or (
            ticket.assignments[-1].report is not None and ticket.assignments[-1].report.state == "failed"
        )
        allowed = not state.config.paused and ticket.ended_by != "human" and revalidate(
            state.config, ticket.mandate, authorized=True,
        )
        can_send = allowed and (not failed or ticket.delivery_error != "DELIVERY_RESULT_UNKNOWN")
        can_steer = bool(
            can_send and latest and latest.id == self.detail.run.id and RunService.accepts_live_input(latest)
        )
        inputs = []
        for item in ticket.inputs[-100:]:
            trigger = records(state).triggers.get(item.trigger_id or "")
            if not trigger or not trigger.authorized:
                continue
            inputs.append({
                "id": item.id, "instruction": trigger.text, "created_at": trigger.at,
                "mode": trigger.console_mode or "continue",
                "status": self._status(item, trigger, latest) if trigger.console_mode else None,
                "run_id": item.run_id, "author": "飞书群成员" if trigger.peer_evidence else None,
            })
        if not any(item.trigger_id == origin.id for item in ticket.inputs[-100:]):
            inputs.insert(0, {
                "id": origin.id, "instruction": origin.text, "created_at": origin.at,
                "mode": "continue", "status": None, "run_id": None, "author": None,
            })
        ids = [a.run_id for a in ticket.assignments if a.run_id]
        native_inputs = await self.service.runs.task_inputs(self.actor, self.actor.workspace_id, self.detail.run.id)
        for native_input in native_inputs:
            if native_input.run_id:
                ids.append(str(native_input.run_id))
        ids.append(str(self.detail.run.id))
        runs = []
        for run_id in list(dict.fromkeys(reversed(ids)))[:12]:
            try:
                detail = await self.service.runs.get(
                    actor=self.actor, workspace_id=self.actor.workspace_id, run_id=UUID(run_id),
                )
            except RunNotFoundError:
                continue
            run = detail.run
            runs.append({
                "id": run_id, "instruction": "", "output": run.output_text, "status": run.status,
                "created_at": run.created_at, "updated_at": run.updated_at, "agent_name": run.agent_name,
            })
        return {
            "can_send": can_send, "can_steer": can_steer, "can_retry": can_send and failed,
            "channel": "feishu", "task_goal": ticket.mandate.scope, "runs": runs, "inputs": inputs,
        }

    async def submit(self, mode: ConsoleMode, instruction: str, idempotency_key: str) -> dict[str, object]:
        workspace = self.actor.workspace_id
        state = await self.service.authorized_state(workspace, self.scope_id)
        ticket, _ = self._ticket(state)
        identity = key("console-input", self.ticket_id, str(self.actor.user_id), idempotency_key)
        if mode == "retry" and not records(state).triggers.exists(identity):
            await self.service.reconcile(workspace, self.scope_id)
            state = await self.service.authorized_state(workspace, self.scope_id)
            ticket, _ = self._ticket(state)
        latest = await self._latest(ticket)

        def submit(engine: Engine, current: State) -> tuple[Input, Trigger, int]:
            active, origin = self._ticket(current)
            saved = records(current).triggers.get(identity)
            if saved:
                if saved.text != instruction or saved.console_mode != mode or (
                    mode == "steer" and saved.console_target != str(self.detail.run.id)
                ):
                    raise CoordinationError("INPUT_KEY_CONFLICT")
            else:
                if current.config.paused or not current.config.enabled or active.ended_by == "human":
                    raise CoordinationError("TASK_INPUT_UNAVAILABLE")
                if active.assignments[-1].run_id != (str(latest.id) if latest else None):
                    raise CoordinationError("TASK_INPUT_UNAVAILABLE")
                if mode == "steer" and (
                    not latest or latest.id != self.detail.run.id or not RunService.accepts_live_input(latest)
                    or active.ended_at
                ):
                    raise CoordinationError("STEERING_UNAVAILABLE")
                if mode == "continue" and latest and latest.status in {"FAILED", "CANCELLED"}:
                    raise CoordinationError("EXPLICIT_RETRY_REQUIRED")
                if mode == "retry" and latest and latest.status not in {"FAILED", "CANCELLED", "SUCCEEDED"}:
                    raise CoordinationError("NOT_RETRYABLE")
                queued = sum(
                    item.id in active.undelivered and item.run_id is None
                    and (source := records(current).triggers.get(item.trigger_id or "")) is not None
                    and source.console_mode is not None
                    for item in active.inputs
                )
                if queued >= 32:
                    raise CoordinationError("TASK_INPUT_LIMIT")
                saved = Trigger(
                    id=identity, workspace_id=origin.workspace_id, scope_id=origin.scope_id,
                    actor_id=origin.actor_id, actor_open_id=origin.actor_open_id,
                    human=True, addressed=True, threads=list(origin.threads), at=engine.now(),
                    text=instruction, origin={**origin.origin, "ingress": "console"},
                    expires_at=origin.expires_at, console_mode=mode,
                    console_target=str(latest.id) if latest else None,
                    console_fence=latest.fencing_token if latest and mode == "steer" else None,
                )
                engine.attach_console_input(active, saved)
                if mode == "continue":
                    engine.advance_console_inputs()
            for index, item in enumerate(active.inputs, 1):
                if item.trigger_id == identity:
                    return replace(item), replace(saved), index
            raise CoordinationError("TASK_INPUT_UNAVAILABLE")

        item, trigger, sequence = await self.service.store.transact(workspace, self.scope_id, submit)
        return {
            "input_id": item.id, "task_id": str(self.detail.task.id), "status": self._status(item, trigger, latest),
            "sequence": sequence, "run_id": item.run_id or (trigger.console_target if mode == "steer" else None),
        }
