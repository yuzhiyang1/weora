"""Agent 应用层依赖的端口与稳定记录。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AgentSkillBindingRecord:
    """Agent 草稿或版本固定的 SkillVersion 快照。"""

    skill_version_id: UUID
    skill_id: UUID
    skill_slug: str
    skill_display_name: str
    version: str
    storage_key: str
    package_size: int
    package_sha256: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class AgentDraftRecord:
    """Agent 当前可变配置草稿。"""

    system_prompt: str
    skills: tuple[AgentSkillBindingRecord, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AgentRecord:
    """工作空间中的 Agent 资产及其当前草稿。"""

    id: UUID
    workspace_id: UUID
    display_name: str
    description: str
    status: str
    latest_published_version: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    draft: AgentDraftRecord | None = None


@dataclass(frozen=True, slots=True)
class AgentVersionRecord:
    """发布后不可修改的 Agent 定义快照。"""

    id: UUID
    agent_id: UUID
    workspace_id: UUID
    version: str
    changelog: str
    display_name: str
    description: str
    system_prompt: str
    skills: tuple[AgentSkillBindingRecord, ...]
    published_by: UUID
    published_at: datetime


@dataclass(frozen=True, slots=True)
class RuntimeSkillReference:
    """Pi 等 Runtime 可按需读取的 Skill 文本参考资料。"""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class RuntimeSkillDefinition:
    """从已校验发布包装配出的 Runtime 中立 Skill 定义。"""

    skill_version_id: UUID
    name: str
    version: str
    content_sha256: str
    instructions: str
    references: tuple[RuntimeSkillReference, ...]


@dataclass(frozen=True, slots=True)
class AgentRuntimeContext:
    """执行面适配器消费的不可变 Agent 输入。"""

    agent_version_id: UUID
    agent_id: UUID
    workspace_id: UUID
    display_name: str
    system_prompt: str
    skills: tuple[RuntimeSkillDefinition, ...]


class AgentRuntimeContextAssemblerPort(Protocol):
    """把版本快照和对象存储材料转换成 Runtime 中立输入。"""

    async def assemble(self, version: AgentVersionRecord) -> AgentRuntimeContext: ...


class PublishedSkillVersionCatalog(Protocol):
    """隔离 Agents 与 Skills 持久化实现的稳定查询端口。"""

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        version_ids: tuple[UUID, ...],
    ) -> tuple[AgentSkillBindingRecord, ...]: ...


class AgentStore(Protocol):
    """一次 Agents 事务内需要的聚合读写操作。"""

    async def create_agent(
        self,
        *,
        agent: AgentRecord,
        draft: AgentDraftRecord,
        actor_id: UUID,
        request_id: str,
    ) -> None: ...

    async def list_agents(self, *, workspace_id: UUID) -> list[AgentRecord]: ...

    async def get_agent(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
    ) -> AgentRecord | None: ...

    async def replace_draft(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
        draft: AgentDraftRecord,
        actor_id: UUID,
        request_id: str,
    ) -> None: ...

    async def publish_version(
        self,
        *,
        agent: AgentRecord,
        version: AgentVersionRecord,
        actor_id: UUID,
        request_id: str,
    ) -> None: ...

    async def get_version(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
        version: str,
    ) -> AgentVersionRecord | None: ...

    async def list_versions(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
    ) -> list[AgentVersionRecord]: ...

    async def get_version_by_id(self, *, workspace_id: UUID, version_id: UUID) -> AgentVersionRecord | None: ...


class AgentsUnitOfWork(Protocol):
    """保证 Agent 元数据、绑定和版本快照原子写入。"""

    store: AgentStore

    async def __aenter__(self) -> "AgentsUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


AgentsUnitOfWorkFactory = Callable[[], AgentsUnitOfWork]
