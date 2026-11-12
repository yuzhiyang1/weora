"""最小私有任务契约，不依赖 Runtime 或持久化实现。"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID


def summarize_task_title(text: str, limit: int = 48) -> str:
    """用单行摘要做标题，保留原请求供任务详情读取。"""
    title = " ".join(text.split())
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


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
    # 私有运行标题与显式发布到协作区的标题各自独立。
    title: str | None = None

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.audience == "group":
            return "仓库任务" if self.origin == "code_hosting" else "飞书任务"
        return summarize_task_title(self.instruction) or "未命名任务"


@dataclass(frozen=True, slots=True)
class TaskAssignment:
    """负责人独立于任务发起人；Agent 指派固定可运行的发布版本。"""

    kind: str
    id: UUID
    name: str
    version: str | None = None


TaskStatus = Literal["backlog", "todo", "in_progress", "in_review", "blocked", "done", "cancelled"]


@dataclass(frozen=True, slots=True)
class TaskCollaboration:
    """协作区只发布明确填写的标题和摘要，不返回私有任务正文。"""

    task_id: UUID
    workspace_id: UUID
    owner_id: UUID
    title: str | None
    summary: str | None
    participants: tuple[UUID, ...]
    assignment: TaskAssignment | None
    manager_id: UUID | None
    pending: TaskAssignment | None
    pending_by: UUID | None
    revision: int
    status: TaskStatus

    def can_manage(self, user_id: UUID) -> bool:
        return self.owner_id == user_id or (self.manager_id == user_id and user_id in self.participants) or (
            self.assignment is not None and self.assignment.kind == "member" and self.assignment.id == user_id
        )

    def can_view(self, user_id: UUID) -> bool:
        return self.can_manage(user_id) or user_id in self.participants


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
    # 运行中消息绑定指定 Run 的这次领取，恢复时不会重放。
    steer_fence: int | None = None


class TaskStore(Protocol):
    """由调用方事务提供的任务存储端口。"""

    async def add(self, task: TaskRecord) -> None: ...

    async def get_private(self, *, workspace_id: UUID, task_id: UUID, user_id: UUID) -> TaskRecord | None: ...

    async def list_private(self, workspace_id: UUID, user_id: UUID, task_ids: set[UUID]) -> list[TaskRecord]: ...
    async def claim_title(self, now: datetime, lease_until: datetime) -> TaskRecord | None: ...
    async def finish_title(self, task: TaskRecord, lease_until: datetime, title: str | None) -> bool: ...

    async def purge_content(self, workspace_id: UUID, task_ids: set[UUID]) -> None: ...

    async def lock_private(self, workspace_id: UUID, task_id: UUID, user_id: UUID) -> TaskRecord | None: ...
    async def add_input(self, item: TaskInput) -> TaskInput: ...
    async def input_by_key(self, workspace_id: UUID, user_id: UUID, key: str) -> TaskInput | None: ...
    async def input_by_id(self, workspace_id: UUID, user_id: UUID, input_id: UUID) -> TaskInput | None: ...
    async def input_for_run(self, workspace_id: UUID, run_id: UUID) -> TaskInput | None: ...
    async def inputs(
        self, workspace_id: UUID, task_id: UUID, user_id: UUID, *, recent: bool = False,
    ) -> list[TaskInput]: ...
    async def steering_inputs(
        self, workspace_id: UUID, run_id: UUID, user_id: UUID, fence: int,
    ) -> list[TaskInput]: ...
    async def mark_input_read(self, item: TaskInput) -> None: ...
    async def assign_input(self, item: TaskInput, run_id: UUID) -> None: ...
    async def reject_input(self, item: TaskInput) -> None: ...
    async def defer_input(self, item: TaskInput) -> None: ...
    async def pending_inputs(self, limit: int) -> list[TaskInput]: ...
    async def thread_run(self, workspace_id: UUID, user_id: UUID, key: str) -> UUID | None: ...
    async def bind_thread(self, task: TaskRecord, key: str, run_id: UUID) -> None: ...

    async def collaboration(
        self, workspace_id: UUID, task_id: UUID, *, lock: bool = False,
    ) -> TaskCollaboration | None: ...
    async def list_collaborations(self, workspace_id: UUID, user_id: UUID) -> list[TaskCollaboration]: ...
    async def save_collaboration(self, state: TaskCollaboration) -> None: ...
    async def add_activity(self, state: TaskCollaboration, event: dict[str, object]) -> None: ...
    async def activities(self, workspace_id: UUID, task_id: UUID) -> list[dict[str, object]]: ...
    async def pending_collaborations(self, limit: int) -> list[tuple[UUID, UUID]]: ...
    async def defer_collaboration(self, workspace_id: UUID, task_id: UUID) -> None: ...


class TaskService:
    """在调用方事务内暂存任务，由最外层用例统一提交或回滚。"""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    async def collaboration(self, workspace_id: UUID, task_id: UUID, *, lock: bool = False) -> TaskCollaboration | None:
        return await self._store.collaboration(workspace_id, task_id, lock=lock)

    async def list_collaborations(self, workspace_id: UUID, user_id: UUID) -> list[TaskCollaboration]:
        return await self._store.list_collaborations(workspace_id, user_id)

    async def change_collaboration(
        self, state: TaskCollaboration, *, actor_id: UUID, action: str, note: str = "",
        previous: TaskAssignment | None = None, target: TaskAssignment | None = None,
    ) -> None:
        """调用方持有 Task 锁；权限、状态和协作动态在同一事务生效。"""
        await self._store.save_collaboration(state)
        await self._store.add_activity(state, {
            "revision": state.revision, "actor_id": str(actor_id), "action": action, "note": note,
            "previous": previous.name if previous else None, "target": target.name if target else None,
            "assignment_changed": previous != target if action in {"assigned", "started"} else False,
        })

    async def activities(self, workspace_id: UUID, task_id: UUID) -> list[dict[str, object]]:
        return await self._store.activities(workspace_id, task_id)

    async def pending_collaborations(self, limit: int) -> list[tuple[UUID, UUID]]:
        return await self._store.pending_collaborations(limit)

    async def defer_collaboration(self, workspace_id: UUID, task_id: UUID) -> None:
        await self._store.defer_collaboration(workspace_id, task_id)

    async def for_collaboration_run(
        self, workspace_id: UUID, task_id: UUID, user_id: UUID, context: dict[str, object],
    ) -> TaskRecord | None:
        """共享执行只装配本轮冻结的摘要及项目，来源回传和私有历史各自隔离。"""
        state = await self.collaboration(workspace_id, task_id)
        if state is None or not state.can_view(user_id) or state.summary is None:
            return None
        project = context.get("project")
        return TaskRecord(
            task_id, workspace_id, user_id, str(context["instruction"]),
            datetime.fromisoformat(str(context["created_at"])),
            project_context=project if isinstance(project, dict) else None,
            title=state.title,
        )

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

    async def list_private(self, workspace_id: UUID, user_id: UUID, task_ids: set[UUID]) -> list[TaskRecord]:
        return await self._store.list_private(workspace_id, user_id, task_ids)

    async def claim_title(self, now: datetime, lease_until: datetime) -> TaskRecord | None:
        return await self._store.claim_title(now, lease_until)

    async def finish_title(self, task: TaskRecord, lease_until: datetime, title: str | None) -> bool:
        return await self._store.finish_title(task, lease_until, title)

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
        return replace(
            task, title=task.display_title if task.audience == "private" else task.title, instruction=item.instruction,
            origin_metadata=item.origin_metadata or task.origin_metadata,
        )

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

    async def inputs(
        self, workspace_id: UUID, task_id: UUID, user_id: UUID, *, recent: bool = False,
    ) -> list[TaskInput]:
        return await self._store.inputs(workspace_id, task_id, user_id, recent=recent)

    async def steering_inputs(
        self, workspace_id: UUID, run_id: UUID, user_id: UUID, fence: int,
    ) -> list[TaskInput]:
        return await self._store.steering_inputs(workspace_id, run_id, user_id, fence)

    async def mark_input_read(self, item: TaskInput) -> None:
        await self._store.mark_input_read(item)

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
