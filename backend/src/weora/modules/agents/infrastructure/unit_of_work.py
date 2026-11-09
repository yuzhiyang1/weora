"""Agents SQLAlchemy Repository 与 Unit of Work。"""

from datetime import UTC, datetime
from types import TracebackType
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.agents.application.ports import (
    AgentDraftRecord,
    AgentRecord,
    AgentSkillBindingRecord,
    AgentStore,
    AgentVersionRecord,
)
from weora.modules.agents.domain.errors import AgentVersionConflictError
from weora.modules.agents.infrastructure.models import (
    AgentDraftModel,
    AgentDraftSkillBindingModel,
    AgentEventModel,
    AgentModel,
    AgentVersionModel,
    AgentVersionSkillBindingModel,
)
from weora.shared.infrastructure.database import SessionFactory


def _as_utc(value: datetime) -> datetime:
    """SQLite 测试会丢失时区信息，统一恢复为 UTC。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _binding_from_model(
    model: AgentDraftSkillBindingModel | AgentVersionSkillBindingModel,
) -> AgentSkillBindingRecord:
    """把两种绑定表恢复为同一个应用层快照。"""
    return AgentSkillBindingRecord(
        skill_version_id=model.skill_version_id,
        skill_id=model.skill_id,
        skill_slug=model.skill_slug,
        skill_display_name=model.skill_display_name,
        version=model.version,
        storage_key=model.storage_key,
        package_size=model.package_size,
        package_sha256=model.package_sha256,
        content_sha256=model.content_sha256,
    )


def _draft_binding_model(
    agent_id: UUID,
    position: int,
    binding: AgentSkillBindingRecord,
) -> AgentDraftSkillBindingModel:
    """创建保持用户配置顺序的草稿绑定行。"""
    return AgentDraftSkillBindingModel(
        agent_id=agent_id,
        position=position,
        **_binding_values(binding),
    )


def _version_binding_model(
    version_id: UUID,
    position: int,
    binding: AgentSkillBindingRecord,
) -> AgentVersionSkillBindingModel:
    """创建随 AgentVersion 冻结的绑定行。"""
    return AgentVersionSkillBindingModel(
        agent_version_id=version_id,
        position=position,
        **_binding_values(binding),
    )


def _binding_values(binding: AgentSkillBindingRecord) -> dict[str, object]:
    """集中维护两种绑定模型共享的不可变字段。"""
    return {
        "skill_version_id": binding.skill_version_id,
        "skill_id": binding.skill_id,
        "skill_slug": binding.skill_slug,
        "skill_display_name": binding.skill_display_name,
        "version": binding.version,
        "storage_key": binding.storage_key,
        "package_size": binding.package_size,
        "package_sha256": binding.package_sha256,
        "content_sha256": binding.content_sha256,
    }


class SqlAlchemyAgentStore:
    """把 Agent 持久化端口映射到 SQLAlchemy ORM。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_agent(
        self,
        *,
        agent: AgentRecord,
        draft: AgentDraftRecord,
        actor_id: UUID,
        request_id: str,
    ) -> None:
        # 没有 ORM relationship 时显式按父子顺序 flush，兼容启用外键的数据库。
        self._session.add(
            AgentModel(
                id=agent.id,
                workspace_id=agent.workspace_id,
                display_name=agent.display_name,
                description=agent.description,
                status=agent.status,
                latest_published_version_id=None,
                created_by=agent.created_by,
                created_at=agent.created_at,
                updated_at=agent.updated_at,
            )
        )
        await self._session.flush()
        self._session.add(
            AgentDraftModel(
                agent_id=agent.id,
                system_prompt=draft.system_prompt,
                updated_by=actor_id,
                updated_at=draft.updated_at,
            )
        )
        await self._session.flush()
        self._session.add_all(
            [
                *[_draft_binding_model(agent.id, position, binding) for position, binding in enumerate(draft.skills)],
                AgentEventModel(
                    id=uuid4(),
                    workspace_id=agent.workspace_id,
                    agent_id=agent.id,
                    actor_id=actor_id,
                    event_type="AGENT_CREATED",
                    request_id=request_id,
                    event_data={"skill_version_ids": [str(item.skill_version_id) for item in draft.skills]},
                    occurred_at=agent.created_at,
                ),
            ]
        )

    async def list_agents(self, *, workspace_id: UUID) -> list[AgentRecord]:
        statement = (
            select(AgentModel, AgentVersionModel.version)
            .outerjoin(
                AgentVersionModel,
                AgentVersionModel.id == AgentModel.latest_published_version_id,
            )
            .where(AgentModel.workspace_id == workspace_id)
            .order_by(AgentModel.updated_at.desc(), AgentModel.id.desc())
        )
        rows = (await self._session.execute(statement)).all()
        return [self._agent_from_model(model, latest_version=version) for model, version in rows]

    async def get_agent(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
    ) -> AgentRecord | None:
        statement = (
            select(AgentModel, AgentDraftModel, AgentVersionModel.version)
            .outerjoin(AgentDraftModel, AgentDraftModel.agent_id == AgentModel.id)
            .outerjoin(
                AgentVersionModel,
                AgentVersionModel.id == AgentModel.latest_published_version_id,
            )
            .where(AgentModel.workspace_id == workspace_id, AgentModel.id == agent_id)
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        model, draft_model, latest_version = row
        draft = None
        if draft_model is not None:
            bindings = await self._draft_bindings(agent_id)
            draft = AgentDraftRecord(
                system_prompt=draft_model.system_prompt,
                skills=bindings,
                updated_at=_as_utc(draft_model.updated_at),
            )
        return self._agent_from_model(model, draft=draft, latest_version=latest_version)

    async def replace_draft(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
        draft: AgentDraftRecord,
        actor_id: UUID,
        request_id: str,
    ) -> None:
        agent = await self._session.scalar(
            select(AgentModel).where(
                AgentModel.workspace_id == workspace_id,
                AgentModel.id == agent_id,
            )
        )
        draft_model = await self._session.get(AgentDraftModel, agent_id)
        if agent is None or draft_model is None:
            return
        await self._session.execute(
            delete(AgentDraftSkillBindingModel).where(AgentDraftSkillBindingModel.agent_id == agent_id)
        )
        draft_model.system_prompt = draft.system_prompt
        draft_model.updated_by = actor_id
        draft_model.updated_at = draft.updated_at
        agent.updated_at = draft.updated_at
        self._session.add_all(
            [
                *[_draft_binding_model(agent_id, position, binding) for position, binding in enumerate(draft.skills)],
                AgentEventModel(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    actor_id=actor_id,
                    event_type="AGENT_DRAFT_UPDATED",
                    request_id=request_id,
                    event_data={"skill_version_ids": [str(item.skill_version_id) for item in draft.skills]},
                    occurred_at=draft.updated_at,
                ),
            ]
        )

    async def publish_version(
        self,
        *,
        agent: AgentRecord,
        version: AgentVersionRecord,
        actor_id: UUID,
        request_id: str,
    ) -> None:
        model = await self._session.scalar(
            select(AgentModel).where(
                AgentModel.workspace_id == agent.workspace_id,
                AgentModel.id == agent.id,
            )
        )
        if model is None:
            return
        # 版本绑定依赖版本父记录，flush 不改变最外层事务的原子性。
        published_model = AgentVersionModel(
            id=version.id,
            agent_id=version.agent_id,
            workspace_id=version.workspace_id,
            version=version.version,
            changelog=version.changelog,
            display_name=version.display_name,
            description=version.description,
            system_prompt=version.system_prompt,
            published_by=version.published_by,
            published_at=version.published_at,
        )
        self._session.add(published_model)
        await self._session.flush()
        self._session.add_all(
            [
                *[
                    _version_binding_model(version.id, position, binding)
                    for position, binding in enumerate(version.skills)
                ],
                AgentEventModel(
                    id=uuid4(),
                    workspace_id=version.workspace_id,
                    agent_id=version.agent_id,
                    actor_id=actor_id,
                    event_type="AGENT_PUBLISHED",
                    request_id=request_id,
                    event_data={"version": version.version},
                    occurred_at=version.published_at,
                ),
            ]
        )
        model.latest_published_version_id = version.id
        model.updated_at = version.published_at

    async def get_version(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
        version: str,
    ) -> AgentVersionRecord | None:
        model = await self._session.scalar(
            select(AgentVersionModel).where(
                AgentVersionModel.workspace_id == workspace_id,
                AgentVersionModel.agent_id == agent_id,
                AgentVersionModel.version == version,
            )
        )
        return await self._version_from_model(model) if model is not None else None

    async def list_versions(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
    ) -> list[AgentVersionRecord]:
        statement = (
            select(AgentVersionModel)
            .where(
                AgentVersionModel.workspace_id == workspace_id,
                AgentVersionModel.agent_id == agent_id,
            )
            .order_by(AgentVersionModel.published_at.desc(), AgentVersionModel.id.desc())
        )
        models = (await self._session.scalars(statement)).all()
        return [await self._version_from_model(model) for model in models]

    async def get_version_by_id(self, *, workspace_id: UUID, version_id: UUID) -> AgentVersionRecord | None:
        """后台只按固定发布快照解析，不重新读取可变草稿或 latest。"""
        model = await self._session.scalar(
            select(AgentVersionModel).where(
                AgentVersionModel.workspace_id == workspace_id,
                AgentVersionModel.id == version_id,
            )
        )
        return await self._version_from_model(model) if model is not None else None

    async def _draft_bindings(self, agent_id: UUID) -> tuple[AgentSkillBindingRecord, ...]:
        statement = (
            select(AgentDraftSkillBindingModel)
            .where(AgentDraftSkillBindingModel.agent_id == agent_id)
            .order_by(AgentDraftSkillBindingModel.position)
        )
        return tuple(_binding_from_model(model) for model in (await self._session.scalars(statement)).all())

    async def _version_from_model(self, model: AgentVersionModel) -> AgentVersionRecord:
        statement = (
            select(AgentVersionSkillBindingModel)
            .where(AgentVersionSkillBindingModel.agent_version_id == model.id)
            .order_by(AgentVersionSkillBindingModel.position)
        )
        skills = tuple(_binding_from_model(binding) for binding in (await self._session.scalars(statement)).all())
        return AgentVersionRecord(
            id=model.id,
            agent_id=model.agent_id,
            workspace_id=model.workspace_id,
            version=model.version,
            changelog=model.changelog,
            display_name=model.display_name,
            description=model.description,
            system_prompt=model.system_prompt,
            skills=skills,
            published_by=model.published_by,
            published_at=_as_utc(model.published_at),
        )

    @staticmethod
    def _agent_from_model(
        model: AgentModel,
        *,
        draft: AgentDraftRecord | None = None,
        latest_version: str | None = None,
    ) -> AgentRecord:
        return AgentRecord(
            id=model.id,
            workspace_id=model.workspace_id,
            display_name=model.display_name,
            description=model.description,
            status=model.status,
            latest_published_version=latest_version,
            created_by=model.created_by,
            created_at=_as_utc(model.created_at),
            updated_at=_as_utc(model.updated_at),
            draft=draft,
        )


class SqlAlchemyAgentsUnitOfWork:
    """为一次 Agent 用例持有独立数据库事务。"""

    store: AgentStore

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> "SqlAlchemyAgentsUnitOfWork":
        self._session = self._session_factory()
        self.store = SqlAlchemyAgentStore(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is not None:
            await self._session.rollback()
        await self._session.close()
        if isinstance(exc_value, IntegrityError) and _is_version_conflict(exc_value):
            raise AgentVersionConflictError from exc_value

    async def commit(self) -> None:
        """提交事务，并把同 Agent 版本冲突转换为稳定错误。"""
        if self._session is None:
            raise RuntimeError("Unit of Work 尚未进入事务上下文")
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_version_conflict(exc):
                raise AgentVersionConflictError from exc
            raise


def _is_version_conflict(error: IntegrityError) -> bool:
    """父版本提前 flush 后，仍保持与 commit 相同的并发版本错误契约。"""
    details = str(error.orig)
    return "uq_agent_versions_agent_version" in details or "agent_versions.agent_id, agent_versions.version" in details
