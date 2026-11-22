"""Skill 发布版本的派生词项。Registry 仍负责包正文与生命周期。"""

import re
from dataclasses import dataclass
from uuid import UUID

import yaml
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.search_domains_v3 import declared_domain, resource_domain
from weora.modules.knowledge.search_tokens_v2 import document_index
from weora.modules.skills.infrastructure.models import SkillSearchTerm, SkillSearchVersion

_INVALID_TEXT = re.compile(r"[\x00\ud800-\udfff]")


@dataclass(frozen=True, slots=True)
class SkillIndex:
    title: str
    tags: list[str]
    excerpt: str
    domain: str
    title_length: int
    length: int
    terms: dict[str, tuple[int, int]]


def _manifest(lines: list[str], boundary: int | None) -> dict[str, object]:
    if not lines or lines[0].strip() != "---" or boundary is None:
        return {}
    try:
        metadata = yaml.safe_load("".join(lines[1:boundary]))
    except (yaml.YAMLError, RecursionError, OverflowError, ValueError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def ranked_skill_domain(tags: list[str], markdown: str = "") -> str:
    """从已校验的包恢复声明。只有词项索引的旧包先按标签与类型分类。"""
    lines = markdown.splitlines(keepends=True)
    boundary = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    metadata = _manifest(lines, boundary)
    domain = declared_domain(metadata.get("domain"))
    return resource_domain(tags, "skill", {"domain": domain} if domain else {})


def index_document(slug: str, markdown: str, *, rank_domain: bool = False) -> SkillIndex:
    """按 TeamAI 使用标题、标签和正文，默认标题来自资源名。"""
    lines = _INVALID_TEXT.sub("", markdown).splitlines(keepends=True)
    boundary = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    title, tags = slug, []
    metadata = _manifest(lines, boundary)
    if lines and lines[0].strip() == "---" and boundary is not None:
        raw = metadata.get("title")
        if isinstance(raw, str) and (clean := _INVALID_TEXT.sub("", raw).strip()):
            title = clean[:160]
        raw_tags = metadata.get("tags")
        if isinstance(raw_tags, list):
            tags = list(dict.fromkeys(
                clean[:60] for tag in raw_tags
                if isinstance(tag, str) and (clean := _INVALID_TEXT.sub("", tag).strip())
            ))[:24]
        body = "".join(lines[boundary + 1:]).lstrip("\r\n")
    else:
        body = "".join(lines)
    domain, title_length, length, terms = document_index(title, body, tags, {"resource_path": f"skills/{slug}"})
    if rank_domain:
        explicit = declared_domain(metadata.get("domain"))
        domain = resource_domain(tags, "skill", {"domain": explicit} if explicit else {})
    return SkillIndex(title, tags, body[:360], domain, title_length, length, terms)


async def index_skill_version(
    db: AsyncSession, workspace: UUID, version: UUID, slug: str, markdown: str,
) -> None:
    """发布及旧包回填共用；只有拿到版本索引写入权的事务追加词项。"""
    existing = await db.get(SkillSearchVersion, version)
    if existing is not None:
        # 旧对象归档首次成功读回时可补齐当时未能读取的显式声明。
        domain = ranked_skill_domain(existing.tags, markdown)
        if domain != existing.domain:
            await db.execute(update(SkillSearchVersion).where(SkillSearchVersion.version_id == version).values(
                domain=domain,
            ))
        return
    summary = index_document(slug, markdown, rank_domain=True)
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else postgres_insert
    claimed = await db.scalar(insert(SkillSearchVersion).values(
        version_id=version, title=summary.title, tags=summary.tags, excerpt=summary.excerpt,
        domain=summary.domain, title_length=summary.title_length, length=summary.length,
    ).on_conflict_do_nothing(index_elements=[SkillSearchVersion.version_id]).returning(SkillSearchVersion.version_id))
    if claimed is not None:
        terms = list(summary.terms.items())
        for offset in range(0, len(terms), 2000):
            await db.execute(insert(SkillSearchTerm), [
                dict(version_id=version, workspace_id=workspace, term=term, fields=flags, frequency=count)
                for term, (flags, count) in terms[offset:offset + 2000]
            ])
