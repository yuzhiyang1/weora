"""飞书运行详情与控制台操作复用原 Ticket、来源和执行状态。"""

from dataclasses import replace
from typing import Literal
from uuid import UUID

from weora.modules.coordination.domain import ConsoleMode, CoordinationError, Input, State, Ticket, Trigger
from weora.modules.coordination.engine import Engine, key
from weora.modules.coordination.policy import gate, revalidate
from weora.modules.coordination.records import records
from weora.modules.coordination.service import CoordinationService
from weora.modules.runs.application.ports import RunActor, RunDetail, RunRecord
from weora.modules.runs.application.service import RunService
from weora.modules.runs.domain.errors import RunNotFoundError

TERMINAL = {"FAILED", "CANCELLED", "SUCCEEDED"}


class FeishuTaskConsole:
    """从已授权运行提取来源。每次读写均复核 Ticket 和原发起人。"""

    def __init__(self, service: CoordinationService, actor: RunActor, detail: RunDetail) -> None:
        self.service = service
        self.actor = actor
        self.detail = detail
        metadata = detail.task.origin_metadata or {}
        self.scope_id = str(metadata["coordination_scope_id"])
        self.ticket_id = str(metadata["coordination_ticket_id"])

    def _ticket(self, state: State) -> tuple[Ticket, Trigger]:
        repository = records(state)
        ticket = repository.tickets.get(self.ticket_id)
        origin = repository.triggers.get(ticket.origin_id) if ticket else None
        if (
            state.config.source_kind != "feishu" or not ticket or not ticket.assignments
            or not origin or not origin.authorized
            or ticket.mandate.actor_id != str(self.actor.user_id) or origin.actor_id != str(self.actor.user_id)
            or ticket.task_id != str(self.detail.task.id)
        ):
            raise RunNotFoundError
        return ticket, origin

    def _allowed(self, state: State, ticket: Ticket, origin: Trigger) -> bool:
        return (
            origin.human and not state.config.paused
            and revalidate(state.config, ticket.mandate, authorized=origin.authorized)
            and gate(state.config, replace(origin, addressed=True)).strength == "explicit"
        )

    def _modes(self, state: State, ticket: Ticket, origin: Trigger, run: RunRecord) -> set[ConsoleMode]:
        if (
            not self._allowed(state, ticket, origin) or ticket.delivery_error == "DELIVERY_RESULT_UNKNOWN"
            or ticket.assignments[-1].run_id != str(run.id)
        ):
            return set()

        if ticket.ended_by == "human":
            return {"resume"} if run.status in TERMINAL else set()
        if run.status in {"WAITING_APPROVAL", "RECOVERY_REQUIRED"} or (
            run.cancellation_requested_at and run.status not in TERMINAL
        ):
            return set()

        assignment = ticket.assignments[-1]
        failed = run.status in {"FAILED", "CANCELLED"} or (assignment.report and assignment.report.state == "failed")
        blocked = assignment.report and assignment.report.state == "blocked"
        if failed or blocked or ticket.delivery_error:
            return {"retry"} if run.status in TERMINAL | {"WAITING_CONTEXT"} else set()

        modes: set[ConsoleMode] = {"continue"}
        if not ticket.ended_at and not assignment.status_only and RunService.accepts_live_input(run):
            modes.add("steer")
        return modes

    def _trigger(
        self, engine: Engine, origin: Trigger, identity: str, mode: ConsoleMode | Literal["stop"], text: str,
        run: RunRecord,
    ) -> Trigger:
        # 从服务端继承原话题与保留期，来源记录为控制台，不能由请求提供飞书身份。
        return Trigger(
            identity, origin.workspace_id, origin.scope_id, origin.actor_id, origin.actor_open_id,
            True, True, list(origin.threads), engine.now(), text,
            {**origin.origin, "ingress": "console"}, expires_at=origin.expires_at,
            console_mode=mode, console_ticket_id=self.ticket_id, console_run_id=str(run.id),
            console_fence=run.fencing_token if mode == "steer" else None,
        )

    @staticmethod
    def _receipt(item: Input, trigger: Trigger, latest: RunRecord | None) -> tuple[str, str | None]:
        if trigger.console_mode == "steer":
            if item.read_at:
                return "READ", trigger.console_run_id
            if (
                latest and str(latest.id) == trigger.console_run_id and latest.fencing_token == trigger.console_fence
                and RunService.accepts_live_input(latest)
            ):
                return "STEERING", trigger.console_run_id
            return "MISSED", trigger.console_run_id
        run_id = item.console_dispatch_run_id
        return ("DISPATCHED" if run_id else "PENDING"), run_id

    async def conversation(self) -> dict[str, object]:
        state = await self.service.authorized_state(self.actor.workspace_id, self.scope_id)
        ticket, origin = self._ticket(state)
        repository = records(state)
        native_inputs = await self.service.runs.task_inputs(self.actor, self.actor.workspace_id, self.detail.run.id)
        run_ids = dict.fromkeys([
            self.detail.run.id,
            *(UUID(item.run_id) for item in reversed(ticket.assignments) if item.run_id),
            *(item.run_id for item in reversed(native_inputs) if item.run_id),
        ])
        runs = []
        latest = None
        for run_id in list(run_ids)[:12]:
            try:
                detail = await self.service.runs.get(
                    actor=self.actor, workspace_id=self.actor.workspace_id, run_id=run_id,
                )
            except RunNotFoundError:
                continue
            run = detail.run
            if str(run.id) == ticket.assignments[-1].run_id:
                latest = run
            runs.append({
                "id": str(run.id), "instruction": "", "output": run.output_text, "status": run.status,
                "created_at": run.created_at, "updated_at": run.updated_at, "agent_name": run.agent_name,
            })

        inputs = []
        for item in ticket.inputs[-100:]:
            trigger = repository.triggers.get(item.trigger_id or "")
            if not trigger or not trigger.authorized:
                continue
            status, delivered_run = self._receipt(item, trigger, latest)
            inputs.append({
                "id": item.id, "instruction": trigger.text, "created_at": trigger.at,
                "mode": trigger.console_mode or "continue", "status": status if trigger.console_mode else None,
                "run_id": delivered_run, "author": "飞书群成员" if trigger.peer_evidence else None,
            })
        if not any(item.trigger_id == origin.id for item in ticket.inputs[-100:]):
            inputs.insert(0, {
                "id": origin.id, "instruction": origin.text, "created_at": origin.at,
                "mode": "continue", "status": None, "run_id": None, "author": None,
            })

        modes = self._modes(state, ticket, origin, latest) if latest and latest.id == self.detail.run.id else set()
        return {
            "channel": "feishu", "runs": runs, "inputs": inputs,
            "task_goal": ticket.mandate.scope, "attention": ticket.attention,
            "can_send": bool(modes), "can_steer": "steer" in modes,
            "can_retry": "retry" in modes, "can_resume": "resume" in modes,
        }

    async def submit(self, mode: ConsoleMode, instruction: str, idempotency_key: str) -> dict[str, object]:
        """事务内校验运行代次和每个幂等键的请求语义，返回持久接收回执。"""
        workspace = self.actor.workspace_id
        state = await self.service.authorized_state(workspace, self.scope_id)
        self._ticket(state)
        identity = key("console-input", self.ticket_id, str(self.actor.user_id), idempotency_key)
        if mode == "retry" and not records(state).triggers.exists(identity):
            await self.service.reconcile(workspace, self.scope_id)
        run = (await self.service.runs.get(actor=self.actor, workspace_id=workspace, run_id=self.detail.run.id)).run

        def submit(engine: Engine, current: State) -> dict[str, object]:
            ticket, origin = self._ticket(current)
            previous = records(current).triggers.get(identity)
            if previous:
                if (previous.console_mode, previous.console_run_id, previous.text) != (mode, str(run.id), instruction):
                    raise CoordinationError("INPUT_KEY_CONFLICT")
                if not previous.authorized:
                    raise RunNotFoundError
                trigger = previous
            else:
                if current.config.revision != state.config.revision:
                    raise CoordinationError("TASK_INPUT_UNAVAILABLE")
                if ticket.delivery_error == "DELIVERY_RESULT_UNKNOWN":
                    raise CoordinationError("CHANNEL_RECONCILIATION_REQUIRED")
                if mode not in self._modes(current, ticket, origin, run):
                    raise CoordinationError("TASK_INPUT_UNAVAILABLE")
                if sum(
                    item.id in ticket.undelivered and not item.console_dispatch_run_id
                    and (source := records(current).triggers.get(item.trigger_id or "")) is not None
                    and source.console_mode is not None
                    and (source.console_mode != "steer" or source.console_run_id == str(run.id)
                         and source.console_fence == run.fencing_token)
                    for item in ticket.inputs
                ) >= 32 and mode in {"continue", "steer"}:
                    raise CoordinationError("TASK_INPUT_LIMIT")

                trigger = self._trigger(engine, origin, identity, mode, instruction, run)
                engine.accept_console_input(ticket, trigger)

            for sequence, item in enumerate(ticket.inputs, start=1):
                if item.trigger_id == identity:
                    status, target = self._receipt(item, trigger, run)
                    return {
                        "input_id": item.id, "task_id": str(self.detail.task.id), "status": status,
                        "sequence": sequence, "run_id": target,
                    }
            raise CoordinationError("TASK_INPUT_UNAVAILABLE")

        return await self.service.store.transact(workspace, self.scope_id, submit)

    async def stop(self) -> None:
        """先持久停止 Ticket，旧轮终态仍由 Runs 和原生执行器确认。"""
        workspace = self.actor.workspace_id
        state = await self.service.authorized_state(workspace, self.scope_id)
        run = self.detail.run
        identity = key("console-stop", self.ticket_id, str(run.id))

        def stop(engine: Engine, current: State) -> None:
            ticket, origin = self._ticket(current)
            if ticket.assignments[-1].run_id != str(run.id):
                raise CoordinationError("TASK_INPUT_UNAVAILABLE")
            if ticket.ended_by == "human":
                return  # 来自飞书或控制台的第一次停止保留其审计。
            if (
                current.config.revision != state.config.revision or not self._allowed(current, ticket, origin)
                or ticket.ended_at or run.status in TERMINAL
            ):
                raise CoordinationError("TASK_INPUT_UNAVAILABLE")
            engine.accept_console_input(ticket, self._trigger(engine, origin, identity, "stop", "停止飞书任务", run))

        await self.service.store.transact(workspace, self.scope_id, stop)
