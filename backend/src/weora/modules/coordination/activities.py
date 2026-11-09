"""对应上游 executor/background.ts 的受控活动监督，不创建进程内全局任务表。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

from weora.modules.coordination.domain import Activity, CoordinationError, Json
from weora.modules.runs.application.execution import RuntimeFailure


class RunActivities:
    """一个已领取 Run 的活动集合；状态先持久化，具体工具仍由原生 Gateway 执行。"""

    def __init__(
        self,
        run_id: str,
        owner: str,
        save: Callable[[Activity], Awaitable[None]],
        *,
        limit: int = 8,
        shutdown_seconds: float = 10,
    ) -> None:
        self.run_id, self.owner, self.save, self.limit = run_id, owner, save, limit
        self.shutdown_seconds = shutdown_seconds
        self.tasks: dict[str, asyncio.Task[Json]] = {}
        self.items: dict[str, Activity] = {}
        self.changed = asyncio.Event()

    async def start(
        self,
        identity: str,
        kind: Literal["command", "subagent"],
        work: Callable[[], Awaitable[Json]],
        *,
        foreground_seconds: float = 5,
        parent_id: str | None = None,
    ) -> Json:
        """保存领取事实后启动活动；短操作直接返回，长操作返回可追踪 ID，不隐式重试。"""
        if identity in self.items:
            item = self.items[identity]
            return item.result if item.state != "running" else self.describe(identity)
        if len(self.items) >= 24 or sum(not task.done() for task in self.tasks.values()) >= self.limit:
            return {"ok": False, "code": "BACKGROUND_CAPACITY_WAIT"}
        item = Activity(identity, self.run_id, kind, self.owner, datetime.now(UTC).isoformat(), parent_id=parent_id)
        await self.save(item)
        self.items[identity] = item

        async def execute() -> Json:
            try:
                result = await work()
                state: Literal["completed", "failed", "cancelled"] = "completed" if result.get("ok") else "failed"
            except asyncio.CancelledError:
                result, state = {"ok": False, "code": "ACTIVITY_CANCELLED"}, "cancelled"
            except (RuntimeFailure, CoordinationError) as error:
                result, state = {"ok": False, "code": error.code}, "failed"
            except Exception:
                # 具体错误已由原生网关记账；供应商异常或凭据不进入协调状态。
                result, state = {"ok": False, "code": "ACTIVITY_FAILED"}, "failed"
            completed = replace(item, state=state, result=result)
            try:
                await self.save(completed)
                self.items[identity] = completed
            finally:
                self.changed.set()
            return result

        task = asyncio.create_task(execute(), name=f"weora-{kind}-{identity}")
        self.tasks[identity] = task
        finished, _ = await asyncio.wait({task}, timeout=foreground_seconds)
        if finished:
            return await task
        return self.describe(identity)

    def describe(self, identity: str) -> Json:
        """只返回当前 Run 的活动状态，不能通过 ID 访问其他 Run。"""
        item = self.items.get(identity)
        if not item:
            return {"ok": False, "code": "ACTIVITY_NOT_FOUND"}
        return {"ok": True, "activity_id": item.id, "kind": item.kind, "state": item.state, "result": item.result}

    @property
    def running(self) -> bool:
        """回合结束后仍有活动时保持原生 Run 槽位，不能先报告整个目标完成。"""
        return any(not task.done() for task in self.tasks.values())

    def descendants(self, identity: str) -> set[str]:
        """枚举活动的全部后代；取消子 Agent 时其命令和更深子会话必须一起结束。"""
        found = {identity}
        while True:
            children = {item.id for item in self.items.values() if item.parent_id in found}
            expanded = found | children
            if expanded == found:
                return found
            found = expanded

    def require_settled_records(self) -> None:
        """真实动作完成但结果入库失败时，不能让父会话把缺失结果当成没有后台工作。"""
        if any(task.done() and not task.cancelled() and task.exception() is not None for task in self.tasks.values()):
            raise RuntimeFailure("BACKGROUND_PERSISTENCE_REQUIRED")

    async def cancel(self, identity: str) -> Json:
        """取消本 Run 的指定活动，并等待 Gateway 或子进程完成清理。"""
        task = self.tasks.get(identity)
        if not task:
            return {"ok": False, "code": "ACTIVITY_NOT_FOUND"}
        selected = [self.tasks[item] for item in self.descendants(identity) if item in self.tasks]
        await self._stop_tasks(selected)
        self.require_settled_records()
        return self.describe(identity)

    async def close(self) -> None:
        """结束、撤权或停机时取消全部后代工作，等受控进程退出再释放执行资源。"""
        await self._stop_tasks(list(self.tasks.values()))

    async def _stop_tasks(self, tasks: list[asyncio.Task[Json]]) -> None:
        """停机等待有界；未确认清理转人工恢复，不能无限卡住 Worker 或声称已全部停止。"""
        if not tasks:
            return
        for task in tasks:
            if not task.done():
                task.cancel()
        _, pending = await asyncio.wait(tasks, timeout=self.shutdown_seconds)
        for task in tasks:
            task.add_done_callback(self._consume_failure)
        if pending:
            for task in pending:
                task.cancel()
            raise RuntimeFailure("BACKGROUND_SHUTDOWN_UNCONFIRMED")

    @staticmethod
    def _consume_failure(task: asyncio.Task[Json]) -> None:
        """原生 Run 与活动记录承担错误反馈，避免晚到异常被事件循环打印成含私密上下文的堆栈。"""
        if not task.cancelled():
            task.exception()

    def require_clean_recovery(self, activities: list[Activity]) -> None:
        """旧执行者的在途工作不能由新租约猜测重放；交给原生恢复与未知写对账。"""
        if any(item.run_id == self.run_id and item.state == "running" for item in activities):
            raise RuntimeFailure("BACKGROUND_RECOVERY_REQUIRED")
