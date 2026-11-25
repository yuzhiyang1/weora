"""重新分类知识与 Skill 的领域投影，保留原词项和发布版本。"""

from contextlib import suppress
from hashlib import sha256

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

from weora.modules.knowledge.search_domains_v3 import resource_domain
from weora.modules.knowledge.search_tokens_v2 import legacy_document_domain
from weora.modules.skills.domain.errors import SkillRegistryError
from weora.modules.skills.domain.package import read_skill_markdown
from weora.modules.skills.infrastructure.knowledge_index import ranked_skill_domain

revision = "20261121_0097"
down_revision = "20261121_0096"
branch_labels = None
depends_on = None


def _knowledge(db: Connection, *, forward: bool) -> None:
    entries = sa.table("knowledge_entries", sa.column("id", sa.Uuid), sa.column("kind", sa.String))
    versions = sa.table(
        "knowledge_revisions", sa.column("entry_id", sa.Uuid), sa.column("revision", sa.Integer),
        sa.column("tags", sa.JSON), sa.column("source", sa.JSON), sa.column("search_domain", sa.String),
    )
    paths = sa.table(
        "knowledge_markdown_paths", sa.column("entry_id", sa.Uuid), sa.column("revision", sa.Integer),
        sa.column("position", sa.Integer), sa.column("path", sa.Text),
    )
    resource = sa.select(sa.func.min(paths.c.path)).where(
        paths.c.entry_id == versions.c.entry_id, paths.c.revision == versions.c.revision, paths.c.position == 0,
    ).scalar_subquery()
    key = sa.tuple_(versions.c.entry_id, versions.c.revision)
    page = sa.select(
        versions.c.entry_id, versions.c.revision, entries.c.kind, versions.c.tags, versions.c.source,
        versions.c.search_domain, resource.label("path"),
    ).select_from(versions.join(entries, entries.c.id == versions.c.entry_id)).where(
        entries.c.kind != "code",
    ).order_by(versions.c.entry_id, versions.c.revision).limit(500)
    statement = page
    write = versions.update().where(
        versions.c.entry_id == sa.bindparam("target_id"), versions.c.revision == sa.bindparam("target_revision"),
    ).values(search_domain=sa.bindparam("classification"))
    while rows := db.execute(statement).all():
        changes = []
        for entry, version, kind, tags, source, previous, path in rows:
            domain = resource_domain(tags, kind, source, path) if forward else legacy_document_domain(tags, source)
            if domain != previous:
                changes.append({"target_id": entry, "target_revision": version, "classification": domain})
        if changes:
            db.execute(write, changes)
        statement = page.where(key > (rows[-1].entry_id, rows[-1].revision))


def _skills(db: Connection, *, forward: bool) -> None:
    indexed = sa.table(
        "skill_search_versions", sa.column("version_id", sa.Uuid), sa.column("tags", sa.JSON),
        sa.column("domain", sa.String),
    )
    skills = sa.table("skills", sa.column("id", sa.Uuid), sa.column("slug", sa.String))
    versions = sa.table(
        "skill_versions", sa.column("id", sa.Uuid), sa.column("workspace_id", sa.Uuid),
        sa.column("skill_id", sa.Uuid), sa.column("package_size", sa.Integer), sa.column("package_sha256", sa.String),
    )
    packages = sa.table(
        "skill_packages", sa.column("workspace_id", sa.Uuid), sa.column("package_sha256", sa.String),
        sa.column("archive_bytes", sa.LargeBinary),
    )
    page = sa.select(
        indexed.c.version_id, indexed.c.tags, indexed.c.domain, skills.c.slug,
        versions.c.workspace_id, versions.c.package_size, versions.c.package_sha256,
    ).select_from(indexed.join(versions, indexed.c.version_id == versions.c.id).join(
        skills, skills.c.id == versions.c.skill_id,
    )).order_by(indexed.c.version_id).limit(100)
    statement = page
    while rows := db.execute(statement).all():
        for version, tags, previous, slug, workspace, size, digest in rows:
            if forward:
                markdown = ""
                archive = db.scalar(sa.select(packages.c.archive_bytes).where(
                    packages.c.workspace_id == workspace, packages.c.package_sha256 == digest,
                ))
                # 不用未经校验的包作为召回依据。对象存储老包在首次成功读取时恢复声明。
                if archive is not None and len(archive) == size and sha256(archive).hexdigest() == digest:
                    with suppress(SkillRegistryError):
                        markdown = read_skill_markdown(archive, slug)
                domain = ranked_skill_domain(tags, markdown)
            else:
                domain = legacy_document_domain(tags, {"resource_path": f"skills/{slug}"})
            if domain != previous:
                db.execute(indexed.update().where(indexed.c.version_id == version).values(domain=domain))
        statement = page.where(indexed.c.version_id > rows[-1].version_id)


def upgrade() -> None:
    db = op.get_bind()
    _knowledge(db, forward=True)
    _skills(db, forward=True)


def downgrade() -> None:
    # 领域声明和原包始终保留，仅恢复旧版本使用的排序投影。
    db = op.get_bind()
    _knowledge(db, forward=False)
    _skills(db, forward=False)
