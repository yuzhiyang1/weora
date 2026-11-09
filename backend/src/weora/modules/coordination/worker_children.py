"""Worker 子会话的独立日志、收件和后代控制；不持有新的审批或执行身份。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

from jsonschema import Draft202012Validator

from weora.modules.coordination.activities import RunActivities
from weora.modules.coordination.domain import CoordinationError, Json, Session, State
from weora.modules.coordination.engine import Engine, key
from weora.modules.coordination.ports import CoordinationStore
from weora.modules.coordination.records import records
from weora.modules.runs.application.execution import RuntimeFailure, RuntimeInput, RuntimeSession, TextRuntime
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.application.session_delivery import deliver_session

if TYPE_CHECKING:
    from weora.modules.tools.application.gateway import ToolSpec

ToolCaller = Callable[[str, str, Json], Awaitable[Json]]
RuntimeFactory = Callable[[RuntimeSession, ToolCaller], TextRuntime]


@dataclass(frozen=True)
class ChildContext:
    workspace_id: UUID
    scope_id: str
    run_id: str
    owner: str
    payload: RuntimeInput
    profile: ExecutionProfile


class WorkerChildren:
    """一次原生租约内的子会话管理，授权回调由父 Worker 提供，所有日志仍在协调事务内。"""

    def __init__(
        self,
        store: CoordinationStore,
        context: ChildContext,
        activities: RunActivities,
        fresh: Callable[[], Awaitable[State]],
        check: Callable[[State], None],
        gateway: ToolCaller,
        runtime: RuntimeFactory,
        tools: tuple["ToolSpec", ...],
    ) -> None:
        self.store, self.context, self.activities = store, context, activities
        self.fresh, self.check, self.gateway, self.runtime, self.tools = fresh, check, gateway, runtime, tools

    def _id(self, identity: str) -> str:
        return key("subagent-session", self.context.run_id, identity)

    async def _change[T](self, action: Callable[[Engine, State], T]) -> T:
        return await self.store.transact(self.context.workspace_id, self.context.scope_id, action)

    def _session(self, current: State, session_id: str) -> Session:
        self.check(current)
        child = records(current).sessions.get(session_id)
        if not child or child.owner != self.context.owner:
            raise RuntimeFailure("ACCESS_REVOKED")
        return child

    async def message(self, call_id: str, target: str, text: str) -> Json:
        """先落子会话收件箱，才确认父指令；允许发生在子进程领取之前，不唤醒已结束活动。"""
        await self.fresh()
        item = self.activities.items.get(target)
        if not item or item.kind != "subagent" or item.state != "running":
            return {"ok": False, "code": "ACTIVITY_NOT_RUNNING"}
        child_id, owner = self._id(target), self.context.owner
        input_id = key(child_id, call_id)

        def save(_: Engine, current: State) -> None:
            self.check(current)
            child = records(current).sessions.get(child_id) or Session(child_id, "subagent", owner=owner)
            if child.owner != owner:
                raise CoordinationError("SESSION_LEASE_LOST")
            if input_id in child.read or any(item["id"] == input_id for item in child.pending_messages):
                return
            if len(child.pending_messages) >= 32:
                raise CoordinationError("SESSION_INPUT_LIMIT")
            child.pending_messages.append({"id": input_id, "text": "父会话补充（不增加权限）：" + text})
            records(current).sessions.save(child)

        await self._change(save)
        return {"ok": True, "input_id": input_id, "state": "queued"}

    async def delegate(self, identity: str, instruction: str, depth: int = 1) -> Json:
        """冻结与父会话相同的模型和工具范围，日志独立；控制操作只返回父会话，不代替人工审批。"""
        child_id = self._id(identity)

        def acquire(_: Engine, current: State) -> Session:
            self.check(current)
            child = records(current).sessions.get(child_id) or Session(child_id, "subagent", owner=self.context.owner)
            if child.owner != self.context.owner:
                raise RuntimeFailure("ACCESS_REVOKED")
            child.model_id = self.context.profile.model_id
            records(current).sessions.save(child)
            return child

        child = await self._change(acquire)
        session = RuntimeSession(
            child_id,
            child.messages,
            pull=partial(self._pull, child_id),
            checkpoint=partial(self._checkpoint, child_id),
            status=partial(self._status, child_id),
        )
        runtime = self.runtime(session, partial(self._tool, identity, child_id, depth))
        request = RuntimeInput(
            self.context.payload.system_prompt + "\n你是当前 Worker 的子会话，只处理委派的子任务。"
            "原生审批保持有效，不自行提出或批准计划，不直接回复外部用户，结果返回父会话。",
            instruction,
            tuple(item for item in self.context.payload.tools if item.effect != "control") + self.tools,
        )
        text = await deliver_session(
            partial(runtime.run, request, self.context.profile), partial(self._status, child_id)
        )
        return {"ok": True, "text": text}

    async def _checkpoint(self, child_id: str, messages: list[Json], read: list[str]) -> None:
        await self.fresh()

        def save(_: Engine, current: State) -> None:
            child = self._session(current, child_id)
            child.messages, child.read = messages, read
            child.pending_messages = [item for item in child.pending_messages if item["id"] not in read]

        await self._change(save)

    async def _pull(self, child_id: str) -> list[tuple[str, str]]:
        current = await self.fresh()
        return [(str(item["id"]), str(item["text"])) for item in self._session(current, child_id).pending_messages]

    async def _status(self, child_id: str, value: Json | None) -> None:
        """重试状态与正文共用同一父 Run 闸门，迟到回调不能覆盖新执行者的健康状态。"""
        await self.fresh()

        def save(_: Engine, current: State) -> None:
            self._session(current, child_id).model_retry = value

        await self._change(save)

    async def _tool(self, identity: str, child_id: str, depth: int, call_id: str, name: str, arguments: Json) -> Json:
        """子会话只控制自己的后代；原生工具的声明、预算、幂等和审批仍由同一个 Gateway 执行。"""
        await self.fresh()
        spec = next((item for item in self.tools if item.name == name), None)
        if spec:
            if not Draft202012Validator(spec.input_schema).is_valid(arguments):
                return {"ok": False, "code": "INVALID_TOOL_PARAMETERS"}
            if name == "orchestrator_delegate":
                if depth >= 3:
                    return {"ok": False, "code": "SUBAGENT_DEPTH_LIMIT"}
                nested = key(child_id, call_id)
                return await self.activities.start(
                    nested,
                    "subagent",
                    partial(self.delegate, nested, str(arguments["instruction"]), depth + 1),
                    foreground_seconds=0,
                    parent_id=identity,
                )
            target = str(arguments["activity_id"])
            if target not in self.activities.descendants(identity):
                return {"ok": False, "code": "ACTIVITY_NOT_OWNED"}
            if name == "orchestrator_activity_read":
                return self.activities.describe(target)
            if target == identity:
                return {"ok": False, "code": "ACTIVITY_SELF_CONTROL_DENIED"}
            if name == "orchestrator_activity_message":
                return await self.message(key(child_id, call_id), target, str(arguments["text"]))
            return await self.activities.cancel(target)
        declared = next((item for item in self.context.payload.tools if item.name == name), None)
        if not declared or declared.effect == "control":
            return {"ok": False, "code": "CHILD_CONTROL_DENIED"}
        tool_id = key(child_id, call_id)
        if name == "sandbox.command":
            return await self.activities.start(
                tool_id,
                "command",
                partial(self.gateway, tool_id, name, arguments),
                foreground_seconds=60,
                parent_id=identity,
            )
        return await self.gateway(tool_id, name, arguments)
