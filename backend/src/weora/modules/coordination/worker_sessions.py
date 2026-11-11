"""受管 Worker 的持久会话端口：授权、回执、活动和重试状态共用同一原生租约。"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from weora.modules.coordination.domain import Activity, Json, Session, State
from weora.modules.coordination.engine import Engine
from weora.modules.coordination.records import records
from weora.modules.runs.application.execution import RunLease, RuntimeFailure, RuntimeSession
from weora.modules.runs.application.harness import PI_RUNTIME
from weora.modules.runs.application.ports import RunActor

if TYPE_CHECKING:
    from weora.modules.coordination.service import CoordinationService


@dataclass(frozen=True)
class WorkerBinding:
    """一次领取冻结的协调位置，不从 Pi 的输入或工具参数重新解析执行身份。"""

    lease: RunLease
    scope_id: str
    ticket_id: str
    assignment_id: str
    session_id: str
    initial: tuple[str, ...]

    @property
    def owner(self) -> str:
        return f"{self.lease.owner}:{self.lease.fencing_token}"


class WorkerSession:
    def __init__(
        self, service: "CoordinationService", binding: WorkerBinding, authorize: Callable[[], Awaitable[bool]]
    ) -> None:
        self.service, self.binding, self.authorize = service, binding, authorize
        self.workspace_id = binding.lease.run.workspace_id
        self.actor = RunActor(binding.lease.run.created_by, self.workspace_id)
        self.port: RuntimeSession | None = None

    def check(self, state: State) -> None:
        """事务内再次验证安排归属；旧租约的迟到回调不能污染新 Run 或已停止任务。"""
        ticket = records(state).tickets.get(self.binding.ticket_id)
        origin = records(state).triggers.get(ticket.origin_id) if ticket else None
        if (
            not ticket
            or not origin
            or not origin.authorized
            or ticket.ended_at
            or not ticket.assignments
            or ticket.assignments[-1].id != self.binding.assignment_id
            or ticket.assignments[-1].run_id != str(self.binding.lease.run.id)
        ):
            raise RuntimeFailure("ACCESS_REVOKED")

    def _stored(self, state: State) -> Session:
        self.check(state)
        session = records(state).sessions.get(self.binding.session_id)
        if not session or session.owner != self.binding.owner:
            raise RuntimeFailure("ACCESS_REVOKED")
        return session

    async def open(self, model_id: str, runtime_id: str = PI_RUNTIME) -> RuntimeSession:
        """原子接管持久日志；保留历史，记录本次 Run 已冻结的模型。"""

        def acquire(_: Engine, state: State) -> Session:
            self.check(state)
            session = records(state).sessions.get(self.binding.session_id) or Session(self.binding.session_id, "worker")
            # 旧会话的检查点由 Pi 写入，禁止用其他内核读取。
            legacy_runtime = PI_RUNTIME if session.generation or session.messages else runtime_id
            stored_runtime = session.runtime_id or legacy_runtime
            if stored_runtime != runtime_id:
                raise RuntimeFailure("HARNESS_SESSION_MISMATCH")
            session.runtime_id = runtime_id
            session.owner, session.generation = self.binding.owner, session.generation + 1
            session.model_id = model_id
            records(state).sessions.save(session)
            return session

        journal = await self.service.store.transact(self.workspace_id, self.binding.scope_id, acquire)
        self.port = RuntimeSession(
            self.binding.session_id,
            journal.messages,
            self.pull,
            self.accepted,
            self.checkpoint,
            list(self.binding.initial),
            self.status,
        )
        return self.port

    async def fresh(self) -> State:
        """IO 前复核成员、来源、正文 TTL、原生 fencing 和运行状态，事务内仍检查安排及日志 owner。"""
        if not await self.authorize():
            raise RuntimeFailure("ACCESS_REVOKED")
        state = await self.service.authorized_state(self.workspace_id, self.binding.scope_id)
        self._stored(state)
        detail = await self.service.runs.access_metadata(self.actor, self.binding.lease.run.id)
        if not detail or detail.run.status != "RUNNING":
            raise RuntimeFailure("RUN_PAUSED")
        return state

    async def pull(self) -> list[tuple[str, str]]:
        """只转交未读输入；等待人类时自动事实继续保存，但不能替代人类唤醒执行。"""
        state = await self.fresh()
        ticket = records(state).tickets.get(self.binding.ticket_id)
        assert ticket is not None
        pending: list[tuple[str, str]] = []
        for item in ticket.inputs:
            if item.id in self.binding.initial or item.read_at:
                continue
            if item.id not in ticket.undelivered and item.delivered_to != self.binding.assignment_id:
                continue
            trigger = records(state).triggers.get(item.trigger_id) if item.trigger_id else None
            if trigger and not trigger.authorized:
                continue
            if trigger and trigger.console_mode:
                run_id = str(self.binding.lease.run.id)
                if trigger.console_mode == "steer" and (
                    trigger.console_target != run_id or trigger.console_fence != self.binding.lease.fencing_token
                ):
                    continue
                if trigger.console_mode == "continue" and trigger.console_target == run_id:
                    continue
            needs_author = Engine.waits_for_person(ticket) or ticket.attention
            if needs_author and (not item.human or item.context_only or item.peer_evidence):
                continue
            pending.append(
                (
                    item.id,
                    json.dumps(
                        {
                            "input_id": item.id,
                            "trigger_id": item.trigger_id,
                            "kind": item.kind,
                            "human": item.human,
                            "context_only": item.context_only,
                            "peer_evidence": item.peer_evidence,
                            "text": (trigger.text if trigger else item.text or "")[:4000],
                            "reference": "长正文使用 orchestrator_read resource=input 分段读取",
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        return pending[:8]

    async def accepted(self, identity: str) -> None:
        """入箱回执不是模型已读；恢复时仍可重投未进入检查点的输入。"""
        await self.fresh()

        def save(engine: Engine, state: State) -> None:
            self._stored(state)
            engine.mark_input(
                self.binding.ticket_id, self.binding.assignment_id, identity, run_id=str(self.binding.lease.run.id),
            )

        await self.service.store.transact(
            self.workspace_id,
            self.binding.scope_id,
            save,
        )

    async def checkpoint(self, messages: list[Json], read: list[str]) -> None:
        """会话和已读同事务保存；历史重复回执保持原安排，活动结果不能冒充人类输入。"""
        await self.fresh()

        def save(engine: Engine, state: State) -> None:
            session = self._stored(state)
            session.messages = messages
            session.read = list(dict.fromkeys(session.read + read))[-500:]
            ticket = engine.ticket(self.binding.ticket_id)
            for identity in read:
                activity = next(
                    (
                        item
                        for item in state.activities
                        if f"activity:{item.id}" == identity and item.run_id == str(self.binding.lease.run.id)
                    ),
                    None,
                )
                if activity:
                    activity.read = True
                    continue
                previous = next((item for item in ticket.inputs if item.id == identity), None)
                if not previous or not previous.read_at:
                    engine.mark_input(
                        ticket.id, self.binding.assignment_id, identity,
                        read=True, run_id=str(self.binding.lease.run.id),
                    )

        await self.service.store.transact(self.workspace_id, self.binding.scope_id, save)
        if self.port:
            self.port.messages = messages

    async def save_activity(self, activity: Activity) -> None:
        """活动结果与会话共用来源保护；保存失败交给恢复，不能把真实动作当作没有发生。"""
        await self.fresh()

        def save(_: Engine, state: State) -> None:
            self._stored(state)
            existing = next((item for item in state.activities if item.id == activity.id), None)
            if existing:
                if existing.owner != self.binding.owner:
                    raise RuntimeFailure("ACCESS_REVOKED")
                state.activities[state.activities.index(existing)] = activity
            else:
                state.activities.append(activity)

        await self.service.store.transact(self.workspace_id, self.binding.scope_id, save)

    async def status(self, value: Json | None) -> None:
        """仅保存脱敏退避状态；结束或撤权后由协调对账清除，不留下永久重试假象。"""
        await self.fresh()

        def save(_: Engine, state: State) -> None:
            self._stored(state).model_retry = value

        await self.service.store.transact(self.workspace_id, self.binding.scope_id, save)
