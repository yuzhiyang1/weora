"""最小私有任务契约，不依赖 Runtime 或持久化实现。"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """固定发起人与初始目标的持续任务；受众由 audience 决定，执行状态由各次 Run 表达。"""

    id: UUID
    workspace_id: UUID
    created_by: UUID
    instruction: str
    created_at: datetime
    origin: str = "console"
    audience: str = "private"
    origin_metadata: dict[str, object] | None = None
    # 冻结本次选中项目/仓库的白名单资料，不含凭据；更新项目不会回写历史任务。
    project_context: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class TaskInput:
    """持久跟进及派发回执；status 区分待处理、已派发和拒绝，不将 Run 关联误作模型已读。"""

    id: UUID
    task_id: UUID
    workspace_id: UUID
    created_by: UUID
    # 提供固定 Agent 版本与执行配置的锚点，不代表当前正在执行的 Run。
    anchor_run_id: UUID
    instruction: str
    idempotency_key: str
    request_sha256: str
    request_id: str
    created_at: datetime
    sequence: int = 0
    run_id: UUID | None = None
    status: str = "PENDING"
    origin_metadata: dict[str, object] | None = None


class TaskStore(Protocol):
    """由调用方事务提供的任务存储端口。"""

    async def add(self, task: TaskRecord) -> None: ...

    async def get_private(self, *, workspace_id: UUID, task_id: UUID, user_id: UUID) -> TaskRecord | None: ...

    async def purge_content(self, workspace_id: UUID, task_ids: set[UUID]) -> None: ...

    async def lock_private(self, workspace_id: UUID, task_id: UUID, user_id: UUID) -> TaskRecord | None: ...
    async def add_input(self, item: TaskInput) -> TaskInput: ...
    async def input_by_key(self, workspace_id: UUID, user_id: UUID, key: str) -> TaskInput | None: ...
    async def input_by_id(self, workspace_id: UUID, user_id: UUID, input_id: UUID) -> TaskInput | None: ...
    async def input_for_run(self, workspace_id: UUID, run_id: UUID) -> TaskInput | None: ...
    async def inputs(self, workspace_id: UUID, task_id: UUID, user_id: UUID) -> list[TaskInput]: ...
    async def assign_input(self, item: TaskInput, run_id: UUID) -> None: ...
    async def reject_input(self, item: TaskInput) -> None: ...
    async def defer_input(self, item: TaskInput) -> None: ...
    async def pending_inputs(self, limit: int) -> list[TaskInput]: ...
    async def thread_run(self, workspace_id: UUID, user_id: UUID, key: str) -> UUID | None: ...
    async def bind_thread(self, task: TaskRecord, key: str, run_id: UUID) -> None: ...


class TaskService:
    """在调用方事务内暂存任务，由最外层用例统一提交或回滚。"""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    async def create_private_console_task(self, task: TaskRecord) -> None:
        """仅接受受信应用层构造的目标，不在模块内提前提交事务。"""
        await self._store.add(task)

    async def create_task(self, task: TaskRecord) -> None:
        """群聊来源仅由渠道应用构造；不开放 HTTP 修改任务受众。"""
        if (task.origin, task.audience) not in {("console", "private"), ("feishu", "group"), ("code_hosting", "group")}:
            raise ValueError("任务来源与受众不匹配")
        if task.audience == "group" and not task.origin_metadata:
            raise ValueError("群聊任务必须保留原会话")
        await self._store.add(task)

    async def get_private(self, *, workspace_id: UUID, task_id: UUID, user_id: UUID) -> TaskRecord | None:
        """即使同工作空间管理员也不隐式获得个人任务正文。"""
        return await self._store.get_private(workspace_id=workspace_id, task_id=task_id, user_id=user_id)

    async def purge_content(self, workspace_id: UUID, task_ids: set[UUID]) -> None:
        """仅保留审计关联，不再保留已过期的请求正文副本。"""
        await self._store.purge_content(workspace_id, task_ids)

    async def lock_private(self, workspace_id: UUID, task_id: UUID, user_id: UUID) -> TaskRecord | None:
        """同目标的跟进与派工串行化；锁只覆盖短数据库事务。"""
        return await self._store.lock_private(workspace_id, task_id, user_id)

    async def for_run(self, task: TaskRecord, run_id: UUID) -> TaskRecord:
        """投影该 Run 的输入与回报目标，不覆写 Task 的原始目标。"""
        item = await self.input_for_run(task, run_id)
        if item is None:
            return task
        return replace(task, instruction=item.instruction, origin_metadata=item.origin_metadata or task.origin_metadata)

    async def input_for_run(self, task: TaskRecord, run_id: UUID) -> TaskInput | None:
        """只有本 Task、本发起人的持久输入才能作为续接依据。"""
        item = await self._store.input_for_run(task.workspace_id, run_id)
        if item is None:
            return None
        if item.task_id != task.id or item.created_by != task.created_by:
            raise ValueError("跟进与任务归属不匹配")
        return item

    async def add_input(self, item: TaskInput) -> TaskInput:
        return await self._store.add_input(item)

    async def input_by_key(self, workspace_id: UUID, user_id: UUID, key: str) -> TaskInput | None:
        return await self._store.input_by_key(workspace_id, user_id, key)

    async def input_by_id(self, workspace_id: UUID, user_id: UUID, input_id: UUID) -> TaskInput | None:
        return await self._store.input_by_id(workspace_id, user_id, input_id)

    async def inputs(self, workspace_id: UUID, task_id: UUID, user_id: UUID) -> list[TaskInput]:
        return await self._store.inputs(workspace_id, task_id, user_id)

    async def assign_input(self, item: TaskInput, run_id: UUID) -> None:
        await self._store.assign_input(item, run_id)

    async def reject_input(self, item: TaskInput) -> None:
        await self._store.reject_input(item)

    async def defer_input(self, item: TaskInput) -> None:
        await self._store.defer_input(item)

    async def pending_inputs(self, limit: int = 50) -> list[TaskInput]:
        return await self._store.pending_inputs(limit)

    async def thread_run(self, workspace_id: UUID, user_id: UUID, key: str) -> UUID | None:
        return await self._store.thread_run(workspace_id, user_id, key)

    async def bind_thread(self, task: TaskRecord, key: str, run_id: UUID) -> None:
        await self._store.bind_thread(task, key, run_id)
