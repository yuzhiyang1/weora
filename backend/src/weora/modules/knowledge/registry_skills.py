"""读取已授权 Skill 版本的规范化正文，包校验由 Skill Registry 负责。"""

from io import BytesIO
from uuid import UUID
from zipfile import ZipFile

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import CTE

from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import KnowledgeEvent
from weora.modules.skills.application.service import SkillActor, SkillRegistryService
from weora.modules.skills.domain.errors import SkillDraftChangedError, SkillRegistryError
from weora.modules.skills.infrastructure.binding import scoped_skill_registry
from weora.modules.skills.infrastructure.knowledge_catalog import PublishedSkillReference, searchable_skill_versions
from weora.shared.object_storage import ObjectStoragePort


async def skill_markdown(
    db: AsyncSession, workspace: UUID, reference: PublishedSkillReference, storage: ObjectStoragePort | None,
) -> str:
    """通过 Registry 校验已发布包的摘要，只在内存读取固定的清单。"""
    registry = await scoped_skill_registry(db, reference.project_id, storage)
    try:
        return await registry.read_version_markdown(workspace, reference.version_id)
    except SkillRegistryError as error:
        raise KnowledgeError("KNOWLEDGE_SKILL_UNAVAILABLE", "无法读取已发布的 Skill 文件。", 409) from error


def standalone_skill_catalog(
    workspace: UUID, project: UUID | None, *, delivered: bool = True, directories: tuple[str, ...] | None = None,
    include_shared: bool = True,
) -> CTE:
    registry = searchable_skill_versions(
        workspace, project, delivered=delivered, directories=directories, include_shared=include_shared,
    )
    # 晋升的 Skill 已有知识条目，Registry 分发包不重复进入检索或质量维护。
    mirrored = select(KnowledgeEvent.id).where(
        KnowledgeEvent.action == "skill_published",
        func.replace(KnowledgeEvent.reason, "-", "") == func.replace(cast(registry.c.id, String), "-", ""),
    ).exists()
    return select(registry).where(~mirrored).cte("standalone_skill_search")


def skill_markdown_parts(markdown: str) -> tuple[str, str]:
    """沿用包校验的分隔符语义，拆开元数据与可修订正文。"""
    lines = markdown.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "".join(lines[:index + 1]), "".join(lines[index + 1:])
    return "", markdown


async def next_skill_patch(
    registry: SkillRegistryService, actor: SkillActor, skill_id: UUID, baseline: str, *, minimum: int = 0,
) -> str:
    """从实际发布版本递增补丁号，并跳过历史中已登记的版本。"""
    versions = await registry.list_versions(actor=actor, workspace_id=actor.workspace_id, skill_id=skill_id)
    taken = {item.version for item in versions}
    major, minor, patch = baseline.split("-", 1)[0].split("+", 1)[0].split(".")
    candidate = max(int(patch) + 1, minimum)
    while f"{major}.{minor}.{candidate}" in taken:
        candidate += 1
    return f"{major}.{minor}.{candidate}"


async def replace_skill_body(
    registry: SkillRegistryService,
    actor: SkillActor,
    skill_id: UUID,
    expected: str,
    body: str,
    request_id: str,
    *,
    replacement_manifest: str | None = None,
) -> str:
    """更新评审内容并保留附属文件。重新晋升可以使用已审核的新清单。"""
    workspace = actor.workspace_id
    skill = await registry.get_skill(actor=actor, workspace_id=workspace, skill_id=skill_id)
    if skill.latest_published_version != expected:
        raise SkillDraftChangedError
    # 安装名取已发布资产，兼容早期用 UUID 发布的经验和自定义清单名称。
    slug = skill.slug
    version = await registry.get_version(actor=actor, workspace_id=workspace, skill_id=skill_id, version=expected)
    existing = skill.draft.package_sha256 if skill.draft else None
    if existing is not None and existing != version.package_sha256:
        # 现有 Registry 草稿可能包含独立工作，审核知识修订时不能将其覆盖。
        raise SkillDraftChangedError
    data = await registry.read_version_archive(workspace, version.id)
    updated = BytesIO()
    main = f"{slug}/SKILL.md"
    with ZipFile(BytesIO(data)) as source, ZipFile(updated, "w") as bundle:
        manifest = replacement_manifest
        if manifest is None:
            header, _ = skill_markdown_parts(source.read(main).decode("utf-8"))
            if not header:
                raise SkillDraftChangedError
            if not header.endswith(("\r", "\n")):
                header += "\n"
            _, revised_body = skill_markdown_parts(body)
            revised_body = revised_body.lstrip("\r\n")
            manifest = f"{header}\n{revised_body}\n"
        for item in source.infolist():
            content = manifest if item.filename == main else source.read(item)
            bundle.writestr(item, content)
    draft = await registry.replace_draft(
        actor=actor,
        workspace_id=workspace,
        skill_id=skill_id,
        package_name=f"{slug}.skill",
        archive_bytes=updated.getvalue(),
        request_id=request_id,
        expected_version=expected,
        expected_draft_sha256=existing,
    )
    return draft.package_sha256
