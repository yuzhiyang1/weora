"""为 Registry 固定发布版本建立词项索引并保留召回里的精确版本。"""

from hashlib import sha256
from uuid import UUID

import sqlalchemy as sa
from alembic import op

from weora.modules.skills.domain.errors import SkillRegistryError
from weora.modules.skills.domain.package import read_skill_markdown
from weora.modules.skills.infrastructure.knowledge_index import index_document

revision = "20261120_0082"
down_revision = "20261120_0081"
branch_labels = None
depends_on = None

_DOC = "skill_search_versions"
_TERM = "skill_search_terms"
_HIT = "knowledge_recall_run_hits"


def upgrade() -> None:
    docs = op.create_table(
        _DOC,
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("skill_versions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(16), nullable=False),
        sa.Column("title_length", sa.Integer(), nullable=False),
        sa.Column("length", sa.Integer(), nullable=False),
    )
    terms = op.create_table(
        _TERM,
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("term", sa.String(160), primary_key=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey(f"{_DOC}.version_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("fields", sa.Integer(), nullable=False),
        sa.Column("frequency", sa.Integer(), nullable=False),
    )
    op.add_column(_HIT, sa.Column("skill_version_id", sa.Uuid()))
    op.add_column(_HIT, sa.Column("skill_opened_at", sa.DateTime(timezone=True)))
    db = op.get_bind()
    skills = sa.table(
        "skills", sa.column("id", sa.Uuid()), sa.column("workspace_id", sa.Uuid()),
        sa.column("latest_published_version_id", sa.Uuid()), sa.column("slug", sa.String()),
    )
    versions = sa.table(
        "skill_versions", sa.column("id", sa.Uuid()), sa.column("workspace_id", sa.Uuid()),
        sa.column("skill_id", sa.Uuid()), sa.column("package_size", sa.Integer()),
        sa.column("package_sha256", sa.String()),
    )
    packages = sa.table(
        "skill_packages", sa.column("workspace_id", sa.Uuid()), sa.column("package_sha256", sa.String()),
        sa.column("archive_bytes", sa.LargeBinary()),
    )
    page = sa.select(
        versions.c.id, versions.c.workspace_id, skills.c.slug, versions.c.package_size, versions.c.package_sha256,
    ).select_from(skills).join(versions, sa.and_(
        versions.c.id == skills.c.latest_published_version_id,
        versions.c.skill_id == skills.c.id, versions.c.workspace_id == skills.c.workspace_id,
    )).order_by(versions.c.id).limit(40)
    statement = page
    while rows := db.execute(statement).all():
        for version, workspace, slug, size, digest in rows:
            # 每次只载入一个包。仅在旧对象存储中的包由 Registry 的校验回填补齐。
            archive = db.scalar(sa.select(packages.c.archive_bytes).where(
                packages.c.workspace_id == workspace, packages.c.package_sha256 == digest,
            ))
            if archive is None or len(archive) != size or sha256(archive).hexdigest() != digest:
                continue
            try:
                markdown = read_skill_markdown(archive, slug)
            except SkillRegistryError:
                continue
            summary = index_document(slug, markdown)
            db.execute(docs.insert().values(
                version_id=version, title=summary.title, tags=summary.tags, excerpt=summary.excerpt,
                domain=summary.domain, title_length=summary.title_length, length=summary.length,
            ))
            postings: list[dict[str, UUID | str | int]] = [
                dict(version_id=version, workspace_id=workspace, term=term, fields=flags, frequency=count)
                for term, (flags, count) in summary.terms.items()
            ]
            for offset in range(0, len(postings), 2000):
                db.execute(terms.insert(), postings[offset:offset + 2000])
        statement = page.where(versions.c.id > rows[-1].id)
    op.create_index("ix_skill_search_terms_version", _TERM, ["version_id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {_HIT} WHERE skill_version_id IS NOT NULL LIMIT 1")):
        raise RuntimeError("已有 Registry Skill 召回快照，回退会丢失固定版本与打开记录。")
    op.drop_column(_HIT, "skill_opened_at")
    op.drop_column(_HIT, "skill_version_id")
    op.drop_table(_TERM)
    op.drop_table(_DOC)
