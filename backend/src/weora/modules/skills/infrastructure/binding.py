"""将项目 Registry 用例接入调用方已授权的数据库事务。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from weora.modules.skills.application.service import SkillRegistryService
from weora.modules.skills.infrastructure.unit_of_work import RegistryPublicationHooks, SqlAlchemySkillsUnitOfWork
from weora.shared.object_storage import ObjectStoragePort


async def scoped_skill_registry(
    db: AsyncSession, project: UUID | None, storage: ObjectStoragePort | None,
    *, publication: RegistryPublicationHooks | None = None,
) -> SkillRegistryService:
    """每次 Registry 操作使用自己的保存点，随外层事务提交或回滚。"""
    sessions = async_sessionmaker(
        await db.connection(), class_=AsyncSession, expire_on_commit=False, join_transaction_mode="create_savepoint",
    )
    return SkillRegistryService(
        uow_factory=lambda: SqlAlchemySkillsUnitOfWork(
            sessions, project_id=project, publication=publication,
        ),
        object_storage=storage,
    )
