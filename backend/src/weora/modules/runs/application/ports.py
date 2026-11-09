"""Run 记录和端口，不导入 Pi、模型 SDK 或 SQLAlchemy。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Literal, Protocol
from uuid import UUID

from weora.modules.collaboration.application.tasks import TaskRecord, TaskService

RunStatus = Literal[
    "QUEUED",
    "RUNNING",
    "WAITING_CONTEXT",
    "WAITING_APPROVAL",
    "RECOVERY_REQUIRED",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
]


@dataclass(frozen=True, slots=True)
class RunActor:
    """由认证层注入的成员身份，不能从请求正文覆盖。"""

    user_id: UUID
    workspace_id: UUID


@dataclass(frozen=True, slots=True)
class PublishedAgentVersion:
    """仅包含运行固定版本所需的稳定标识和显示快照。"""

    id: UUID
    agent_id: UUID
    version: str
    display_name: str


class PublishedAgentCatalog(Protocol):
    """通过 Agents 应用接口读取版本，不跨模块查询 ORM。"""

    async def resolve(self, *, actor: RunActor, agent_id: UUID, version: str) -> PublishedAgentVersion: ...


class ProjectContextCatalog(Protocol):
    async def snapshot(
        self, actor: RunActor, project_id: UUID, repository_ids: tuple[UUID, ...]
    ) -> dict[str, object]: ...
    async def authorize(self, actor: RunActor, snapshot: dict[str, object]) -> bool: ...
    async def prepare(self, actor: RunActor, run_id: UUID, snapshot: dict[str, object]) -> list[dict[str, object]]: ...


@dataclass(frozen=True, slots=True)
class RunRecord:
    """一次执行尝试，冻结 AgentVersion；当前切片不伪造模型配置或输出。"""

    id: UUID
    task_id: UUID
    workspace_id: UUID
    created_by: UUID
    agent_version_id: UUID
    agent_name: str
    agent_version: str
    status: RunStatus
    last_sequence: int
    created_at: datetime
    updated_at: datetime
    execution_profile: "ExecutionProfile | None" = None
    cancellation_requested_at: datetime | None = None
    output_text: str | None = None
    failure_code: str | None = None
    recovery_read_only: bool = False
    # 共享执行冻结经过显式发布的摘要和本轮授权快照。
    collaboration_context: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    """每次运行固定的非敏感模型配置，密钥不属于快照。"""

    runtime_id: str
    provider: str
    model_id: str
    max_tokens: int
    timeout_seconds: int
    # 新任务显式冻结整体时限；旧任务缺省保留原来的模型/整体同一预算语义。
    execution_timeout_seconds: int | None = None
    # 仅保存配置版本标识，密钥由受信任后端按团队解析。
    configuration_id: str | None = None

    @property
    def wall_timeout_seconds(self) -> int:
        return self.execution_timeout_seconds or self.timeout_seconds


@dataclass(frozen=True, slots=True)
class RunSubmission:
    """私有幂等键及规范化请求摘要，不存储密钥或额外指令副本。"""

    run: RunRecord
    idempotency_key: str
    request_sha256: str


@dataclass(frozen=True, slots=True)
class RunEventRecord:
    """事件只保留平台事实与标识，任务正文仍由 Task 权限保护。"""

    run_id: UUID
    sequence: int
    event_type: str
    request_id: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class RunDetail:
    """应用层组合 Task 与 Run，基础设施不跨模块访问任务表。"""

    run: RunRecord
    task: TaskRecord


class RunStore(Protocol):
    """单次事务内的运行记录与调度操作。"""

    async def find_submission(self, *, actor: RunActor, key: str) -> RunSubmission | None: ...

    async def add(self, submission: RunSubmission, *, request_id: str) -> None: ...

    async def get_private(self, *, actor: RunActor, run_id: UUID) -> RunRecord | None: ...

    async def list_private(self, *, actor: RunActor, limit: int, before: RunRecord | None) -> list[RunRecord]: ...

    async def list_events(self, *, actor: RunActor, run_id: UUID, after: int, limit: int) -> list[RunEventRecord]: ...

    async def cancel_queued(self, *, actor: RunActor, run_id: UUID, request_id: str) -> bool: ...

    async def request_running_cancellation(self, *, actor: RunActor, run_id: UUID, request_id: str) -> bool: ...

    async def task_runs(self, *, actor: RunActor, task_id: UUID, limit: int = 8) -> list[RunRecord]: ...

    async def active_task_run(self, *, actor: RunActor, task_id: UUID) -> RunRecord | None: ...

    async def active_run_for_task(self, workspace_id: UUID, task_id: UUID) -> RunRecord | None: ...


class RunsUnitOfWork(Protocol):
    """通过协作模块用例参与同一事务，原子保存 Task/Run/事件/待调度记录。"""

    store: RunStore
    tasks: TaskService

    async def __aenter__(self) -> "RunsUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


RunsUnitOfWorkFactory = Callable[[], RunsUnitOfWork]
