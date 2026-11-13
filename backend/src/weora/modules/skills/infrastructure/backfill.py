"""核对或补齐指定工作空间仍只存在于历史对象存储的 Skill 包。"""

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import exists, select

from weora.bootstrap.object_storage import create_object_storage
from weora.bootstrap.settings import get_settings
from weora.modules.skills.application.service import SkillRegistryService
from weora.modules.skills.domain.errors import SkillRegistryError
from weora.modules.skills.infrastructure.models import SkillDraftModel, SkillModel, SkillPackageModel, SkillVersionModel
from weora.modules.skills.infrastructure.unit_of_work import SqlAlchemySkillsUnitOfWork
from weora.shared.infrastructure.database import create_database
from weora.shared.object_storage.errors import ObjectStorageError


async def backfill(workspace_id: UUID, *, apply: bool) -> int:
    """逐包独立提交；摘要不匹配时保留原数据，并返回失败退出码。"""
    settings = get_settings()
    engine, sessions = create_database(settings.database_url)
    storage = create_object_storage(settings) if apply else None
    service = SkillRegistryService(uow_factory=lambda: SqlAlchemySkillsUnitOfWork(sessions), object_storage=storage)
    versions = select(SkillVersionModel.id).where(
        SkillVersionModel.workspace_id == workspace_id,
        SkillVersionModel.storage_key != "",
        ~exists().where(
            SkillPackageModel.workspace_id == workspace_id,
            SkillPackageModel.package_sha256 == SkillVersionModel.package_sha256,
        ),
    )
    drafts = (
        select(SkillDraftModel.skill_id)
        .join(SkillModel, SkillModel.id == SkillDraftModel.skill_id)
        .where(
            SkillModel.workspace_id == workspace_id,
            SkillDraftModel.storage_key != "",
            ~exists().where(
                SkillPackageModel.workspace_id == workspace_id,
                SkillPackageModel.package_sha256 == SkillDraftModel.package_sha256,
            ),
        )
    )
    count = failures = 0
    try:
        async with sessions() as db:
            for kind, query in (("version", versions), ("draft", drafts)):
                result = await db.stream_scalars(query.execution_options(yield_per=100))
                async for record_id in result:
                    count += 1
                    if not apply:
                        continue
                    try:
                        if kind == "version":
                            await service.read_version_archive(workspace_id, record_id)
                        else:
                            await service.backfill_draft_archive(workspace_id, record_id)
                    except (SkillRegistryError, ObjectStorageError) as exc:
                        failures += 1
                        print(f"{kind} {record_id}: {type(exc).__name__}")
    finally:
        if storage is not None:
            await storage.aclose()
        await engine.dispose()
    print(f"workspace={workspace_id} apply={apply} candidates={count} failures={failures}")
    return int(failures > 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=UUID, required=True)
    parser.add_argument("--apply", action="store_true", help="将通过摘要校验的旧包写入 PostgreSQL")
    args = parser.parse_args()
    return asyncio.run(backfill(args.workspace, apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
