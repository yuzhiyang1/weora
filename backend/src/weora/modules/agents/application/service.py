"""Agent 资产与不可变版本用例编排。"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from weora.modules.agents.application.ports import (
    AgentDraftRecord,
    AgentRecord,
    AgentRuntimeContext,
    AgentRuntimeContextAssemblerPort,
    AgentSkillBindingRecord,
    AgentsUnitOfWorkFactory,
    AgentVersionRecord,
    PublishedSkillVersionCatalog,
)
from weora.modules.agents.domain.errors import (
    AgentNotFoundError,
    AgentPermissionDeniedError,
    AgentSkillVersionInvalidError,
    AgentVersionConflictError,
    AgentVersionInvalidError,
    AgentWorkspaceAccessDeniedError,
)

_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@dataclass(frozen=True, slots=True)
class AgentActor:
    """执行 Agent 用例所需的最小成员上下文。"""

    user_id: UUID
    workspace_id: UUID
    role: str


class AgentRegistryService:
    """协调 Agent 草稿、SkillVersion 解析和不可变版本发布。"""

    _WRITE_ROLES = frozenset({"owner", "admin"})
    _MAX_BOUND_SKILLS = 20

    def __init__(
        self,
        *,
        uow_factory: AgentsUnitOfWorkFactory,
        skill_catalog: PublishedSkillVersionCatalog,
        runtime_context_assembler: AgentRuntimeContextAssemblerPort,
    ) -> None:
        self._uow_factory = uow_factory
        self._skill_catalog = skill_catalog
        self._runtime_context_assembler = runtime_context_assembler

    async def create_agent(
        self,
        *,
        actor: AgentActor,
        workspace_id: UUID,
        display_name: str,
        description: str,
        system_prompt: str,
        skill_version_ids: tuple[UUID, ...],
        request_id: str,
    ) -> AgentRecord:
        """创建 Agent 资产和第一份可变草稿。"""
        self._require_write(actor, workspace_id)
        bindings = await self._resolve_bindings(workspace_id, skill_version_ids)
        now = datetime.now(UTC)
        draft = AgentDraftRecord(
            system_prompt=system_prompt.strip(),
            skills=bindings,
            updated_at=now,
        )
        agent = AgentRecord(
            id=uuid4(),
            workspace_id=workspace_id,
            display_name=display_name.strip(),
            description=description.strip(),
            status="active",
            latest_published_version=None,
            created_by=actor.user_id,
            created_at=now,
            updated_at=now,
            draft=draft,
        )
        async with self._uow_factory() as uow:
            await uow.store.create_agent(
                agent=agent,
                draft=draft,
                actor_id=actor.user_id,
                request_id=request_id,
            )
            await uow.commit()
        return agent

    async def list_agents(
        self,
        *,
        actor: AgentActor,
        workspace_id: UUID,
    ) -> list[AgentRecord]:
        """列出当前工作空间的 Agent 资产。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            return await uow.store.list_agents(workspace_id=workspace_id)

    async def get_agent(
        self,
        *,
        actor: AgentActor,
        workspace_id: UUID,
        agent_id: UUID,
    ) -> AgentRecord:
        """读取当前工作空间中的 Agent 及其草稿。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            agent = await uow.store.get_agent(workspace_id=workspace_id, agent_id=agent_id)
        if agent is None:
            raise AgentNotFoundError
        return agent

    async def replace_draft(
        self,
        *,
        actor: AgentActor,
        workspace_id: UUID,
        agent_id: UUID,
        system_prompt: str,
        skill_version_ids: tuple[UUID, ...],
        request_id: str,
    ) -> AgentDraftRecord:
        """替换当前草稿；已发布版本不发生变化。"""
        self._require_write(actor, workspace_id)
        await self.get_agent(actor=actor, workspace_id=workspace_id, agent_id=agent_id)
        draft = AgentDraftRecord(
            system_prompt=system_prompt.strip(),
            skills=await self._resolve_bindings(workspace_id, skill_version_ids),
            updated_at=datetime.now(UTC),
        )
        async with self._uow_factory() as uow:
            await uow.store.replace_draft(
                workspace_id=workspace_id,
                agent_id=agent_id,
                draft=draft,
                actor_id=actor.user_id,
                request_id=request_id,
            )
            await uow.commit()
        return draft

    async def publish(
        self,
        *,
        actor: AgentActor,
        workspace_id: UUID,
        agent_id: UUID,
        version: str,
        changelog: str,
        request_id: str,
    ) -> AgentVersionRecord:
        """从当前草稿发布不可变 Agent 版本。"""
        self._require_write(actor, workspace_id)
        version = version.strip()
        changelog = changelog.strip()
        if not _SEMVER_PATTERN.fullmatch(version) or len(version) > 64 or not changelog:
            raise AgentVersionInvalidError
        agent = await self.get_agent(actor=actor, workspace_id=workspace_id, agent_id=agent_id)
        if agent.draft is None:
            raise AgentNotFoundError
        async with self._uow_factory() as uow:
            existing = await uow.store.get_version(
                workspace_id=workspace_id,
                agent_id=agent_id,
                version=version,
            )
        if existing is not None:
            raise AgentVersionConflictError
        published = AgentVersionRecord(
            id=uuid4(),
            agent_id=agent.id,
            workspace_id=workspace_id,
            version=version,
            changelog=changelog,
            display_name=agent.display_name,
            description=agent.description,
            system_prompt=agent.draft.system_prompt,
            skills=agent.draft.skills,
            published_by=actor.user_id,
            published_at=datetime.now(UTC),
        )
        async with self._uow_factory() as uow:
            await uow.store.publish_version(
                agent=agent,
                version=published,
                actor_id=actor.user_id,
                request_id=request_id,
            )
            await uow.commit()
        return published

    async def get_version(
        self,
        *,
        actor: AgentActor,
        workspace_id: UUID,
        agent_id: UUID,
        version: str,
    ) -> AgentVersionRecord:
        """读取指定不可变 Agent 版本。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            record = await uow.store.get_version(
                workspace_id=workspace_id,
                agent_id=agent_id,
                version=version,
            )
        if record is None:
            raise AgentNotFoundError
        return record

    async def list_versions(
        self,
        *,
        actor: AgentActor,
        workspace_id: UUID,
        agent_id: UUID,
    ) -> list[AgentVersionRecord]:
        """列出 Agent 的全部发布版本。"""
        await self.get_agent(actor=actor, workspace_id=workspace_id, agent_id=agent_id)
        async with self._uow_factory() as uow:
            return await uow.store.list_versions(workspace_id=workspace_id, agent_id=agent_id)

    async def build_runtime_context(
        self,
        *,
        actor: AgentActor,
        workspace_id: UUID,
        agent_id: UUID,
        version: str,
    ) -> AgentRuntimeContext:
        """为未来 Pi Adapter 装配一个固定版本且经过摘要校验的运行输入。"""
        record = await self.get_version(
            actor=actor,
            workspace_id=workspace_id,
            agent_id=agent_id,
            version=version,
        )
        return await self._runtime_context_assembler.assemble(record)

    async def build_runtime_context_by_id(
        self,
        *,
        actor: AgentActor,
        workspace_id: UUID,
        version_id: UUID,
    ) -> AgentRuntimeContext:
        """为 Worker 按 Run 冻结的版本 ID 装配材料，不开放跨模块 ORM。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            record = await uow.store.get_version_by_id(workspace_id=workspace_id, version_id=version_id)
        if record is None:
            raise AgentNotFoundError
        return await self._runtime_context_assembler.assemble(record)

    async def _resolve_bindings(
        self,
        workspace_id: UUID,
        version_ids: tuple[UUID, ...],
    ) -> tuple[AgentSkillBindingRecord, ...]:
        """保持用户配置顺序，并拒绝重复、越界或未发布版本。"""
        if len(version_ids) > self._MAX_BOUND_SKILLS or len(set(version_ids)) != len(version_ids):
            raise AgentSkillVersionInvalidError
        bindings = await self._skill_catalog.resolve(
            workspace_id=workspace_id,
            version_ids=version_ids,
        )
        if len(bindings) != len(version_ids):
            raise AgentSkillVersionInvalidError
        by_id = {item.skill_version_id: item for item in bindings}
        try:
            return tuple(by_id[version_id] for version_id in version_ids)
        except KeyError as exc:
            raise AgentSkillVersionInvalidError from exc

    @staticmethod
    def _require_workspace(actor: AgentActor, workspace_id: UUID) -> None:
        if actor.workspace_id != workspace_id:
            raise AgentWorkspaceAccessDeniedError

    @classmethod
    def _require_write(cls, actor: AgentActor, workspace_id: UUID) -> None:
        cls._require_workspace(actor, workspace_id)
        if actor.role not in cls._WRITE_ROLES:
            raise AgentPermissionDeniedError
