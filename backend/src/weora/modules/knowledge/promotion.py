"""从已审核的知识生成对应范围的 Skill Registry 资产。"""

import re
from io import BytesIO
from pathlib import PurePosixPath
from uuid import UUID
from zipfile import ZipFile

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.domain import KnowledgeError, PromotionKind
from weora.modules.knowledge.markdown_paths_v1 import entry_path
from weora.modules.knowledge.registry_publication import KNOWLEDGE_SKILL_PUBLICATION
from weora.modules.knowledge.registry_skills import next_skill_patch, replace_skill_body, skill_markdown_parts
from weora.modules.skills.application.ports import SkillVersionRecord
from weora.modules.skills.application.service import SkillActor
from weora.modules.skills.domain.errors import SkillManifestInvalidError, SkillRegistryError
from weora.modules.skills.domain.package import MAX_SKILL_DESCRIPTION_LENGTH, is_skill_slug
from weora.modules.skills.infrastructure.binding import scoped_skill_registry

_SIGNALS: dict[PromotionKind, tuple[str, ...]] = {
    "rule": ("must", "never", "always", "constraint", "convention", "standard", "rule"),
    "skill": ("command", "cli", "workflow", "step-by-step", "procedure", "how to", "recipe"),
    "doc": ("architecture", "design", "overview", "background", "decision", "context"),
}
_UUID_STEM = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def suggested_kind(title: str, body: str) -> PromotionKind:
    """沿用 TeamAI 的关键词和打平顺序，让列表与预演无需调用模型。"""
    text = f"{body} {title}".lower()
    return max(_SIGNALS, key=lambda kind: sum(signal in text for signal in _SIGNALS[kind]))


def promotion_path(entry: UUID, kind: PromotionKind, origin: str | None) -> str:
    """将经验的首发路径映射到目标资源树，手工条目使用稳定 ID。"""
    original = origin or ""
    relative = original.removeprefix("learnings/") if original.startswith("learnings/") else str(entry)
    return f"skills/{relative}/SKILL.md" if kind == "skill" else f"{kind}s/{relative}.md"


def promotion_targets(entry: UUID, origin: str | None) -> dict[PromotionKind, str]:
    return {kind: promotion_path(entry, kind, origin) for kind in _SIGNALS}


def _skill_description(body: str, slug: str) -> str:
    """沿用 TeamAI 的正文摘要，优先使用首个符合条件的标题或文本行。"""
    for line in body.splitlines():
        line = line.strip()
        if heading := re.fullmatch(r"#+\s+(.+)", line):
            return heading[1].strip()[:MAX_SKILL_DESCRIPTION_LENGTH]
        if len(line) > 10:
            return line if len(line) <= 80 else f"{line[:77]}..."
    return f"{slug} skill"


def _skill_manifest(markdown: str, slug: str) -> tuple[str, bool]:
    """返回补齐后的清单，并标识名称是否由安装名生成。"""
    header, body = skill_markdown_parts(markdown)
    try:
        raw = yaml.safe_load("\n".join(header.splitlines()[1:-1])) if header else None
    except yaml.YAMLError as exc:
        raise SkillManifestInvalidError from exc
    if raw is not None and not isinstance(raw, dict):
        raise SkillManifestInvalidError
    metadata: dict[object, object] = raw if raw is not None else {}
    name, description = metadata.get("name"), metadata.get("description")
    generated_name = not isinstance(name, str) or not name.strip()
    if not generated_name and isinstance(description, str) and description.strip():
        return markdown, False
    if generated_name:
        metadata["name"] = slug
    if not isinstance(description, str) or not description.strip():
        metadata["description"] = _skill_description(body, slug)
    fields = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
    if not header:
        body = "\n" + body
    return f"---\n{fields}---\n{body}", generated_name


async def publish_knowledge_skill(
    db: AsyncSession,
    *,
    workspace: UUID,
    project_id: UUID | None,
    reviewer: UUID,
    role: str,
    entry: UUID,
    title: str,
    body: str,
    request_id: str,
    revision: int,
    previous: tuple[UUID, str] | None,
    source: dict[str, str],
    repromoting: bool = False,
) -> SkillVersionRecord:
    """复用 Skill 的校验、版本与审计；子事务随知识审核一起提交或回滚。"""
    registry = await scoped_skill_registry(db, project_id, None)
    actor = SkillActor(reviewer, workspace, role)
    changelog = "应用已审核的质量修订" if previous else "从已审核的经验晋升"
    if repromoting:
        changelog = "从已审核的经验重新晋升"
    try:
        if previous is None:
            version = f"1.0.{revision - 1}"
            parts = PurePosixPath(entry_path(entry, "skill", source) or "").parts
            # 复用经验文件名和目录。旧数据中的不规范文件名仍可用稳定标识安装。
            directories = parts[1:] if len(parts) > 1 and parts[0] == "skills" else ()
            stem = directories[-1] if directories else ""
            slug = stem if is_skill_slug(stem) else f"learning-{entry.hex}"
            namespace = "/".join(directories[:-1])
            manifest, generated_name = _skill_manifest(body, slug)
            archive = BytesIO()
            with ZipFile(archive, "w") as bundle:
                bundle.writestr(f"{slug}/SKILL.md", manifest)
                if generated_name and (slug != stem or _UUID_STEM.fullmatch(stem)):
                    # 标题便于成员辨认，内部编号继续作为可重复安装的目录标识。
                    interface = {"interface": {"display_name": title[:120].rstrip()}}
                    bundle.writestr(f"{slug}/agents/openai.yaml", yaml.safe_dump(interface, allow_unicode=True))
            skill = await registry.create_skill(
                actor=actor,
                workspace_id=workspace,
                package_name=f"{slug}.skill",
                archive_bytes=archive.getvalue(),
                request_id=request_id,
                namespace=namespace,
                installation_slug=slug,
            )
            skill_id, draft_sha = skill.id, None
        else:
            skill_id, base = previous
            manifest = None
            if repromoting:
                restoration = await scoped_skill_registry(db, project_id, None, publication=KNOWLEDGE_SKILL_PUBLICATION)
                restored = await restoration.set_archived(
                    actor=actor, workspace_id=workspace, skill_id=skill_id, archived=False, request_id=request_id,
                )
                # 恢复、草稿校验和再次发布随知识审核一起提交。任一步失败时原资产仍归档。
                manifest, _ = _skill_manifest(body, restored.slug)
            minimum = revision - 1 if base.startswith("1.0.") else 0
            version = await next_skill_patch(registry, actor, skill_id, base, minimum=minimum)
            draft_sha = await replace_skill_body(
                registry, actor, skill_id, base, body, request_id, replacement_manifest=manifest,
            )
        return await registry.publish(
            actor=actor,
            workspace_id=workspace,
            skill_id=skill_id,
            version=version,
            changelog=changelog,
            request_id=request_id,
            draft_sha256=draft_sha,
        )
    except SkillRegistryError as exc:
        raise KnowledgeError("KNOWLEDGE_SKILL_PUBLISH_FAILED", f"{exc.message}。知识版本仍待审核。", 409) from exc
