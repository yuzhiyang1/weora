"""为已存在的代码概览、文件和已发布设计回填 teamwiki 路径。"""

from hashlib import sha256
from uuid import UUID

import sqlalchemy as sa
from alembic import op

from weora.modules.knowledge.code_wiki_paths_v1 import wiki_locations, wiki_namespaces

revision = "20261120_0081"
down_revision = "20261120_0080"
branch_labels = None
depends_on = None

_BASES = "knowledge_codebases"
_PATHS = "knowledge_markdown_paths"


def upgrade() -> None:
    op.add_column(_BASES, sa.Column("wiki_namespaces", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    bases = sa.table(
        _BASES, sa.column("id", sa.Uuid()), sa.column("repository_id", sa.Uuid()),
        sa.column("connection_version", sa.Integer()), sa.column("local_name", sa.String()),
        sa.column("wiki_namespaces", sa.JSON()),
    )
    repositories = sa.table(
        "project_repositories", sa.column("id", sa.Uuid()), sa.column("connection_version", sa.Integer()),
        sa.column("git_url", sa.String()), sa.column("provider", sa.String()),
    )
    entries = sa.table("knowledge_entries", sa.column("id", sa.Uuid()), sa.column("codebase_id", sa.Uuid()))
    versions = sa.table(
        "knowledge_revisions", sa.column("entry_id", sa.Uuid()), sa.column("revision", sa.Integer()),
        sa.column("source", sa.JSON()),
    )
    paths = sa.table(
        _PATHS, sa.column("entry_id", sa.Uuid()), sa.column("revision", sa.Integer()), sa.column("key", sa.String()),
        sa.column("kind", sa.String()), sa.column("path", sa.Text()), sa.column("position", sa.Integer()),
    )
    db = op.get_bind()
    page = sa.select(
        bases.c.id, bases.c.local_name, repositories.c.git_url, repositories.c.provider,
    ).outerjoin(repositories, sa.and_(
        repositories.c.id == bases.c.repository_id,
        repositories.c.connection_version == bases.c.connection_version,
    )).order_by(bases.c.id).limit(40)
    statement = page
    while rows := db.execute(statement).all():
        for key, local_name, address, provider in rows:
            namespaces = wiki_namespaces(address, provider, local_name)
            if not namespaces:
                continue
            db.execute(bases.update().where(bases.c.id == key).values(wiki_namespaces=list(namespaces)))
            snapshots = sa.select(versions.c.entry_id, versions.c.revision, versions.c.source).join(
                entries, entries.c.id == versions.c.entry_id,
            ).where(entries.c.codebase_id == key)
            # 每批只读取来源元数据。旧索引正文与进入审批或召回日志的版本保持原样。
            for batch in db.execute(snapshots).partitions(500):
                postings: list[dict[str, UUID | int | str]] = [
                    dict(entry_id=identity, revision=version, position=0, path=path,
                         key=sha256(path.encode()).hexdigest(), kind="REFERENCES")
                    for identity, version, source in batch for path in wiki_locations(namespaces, source)
                ]
                if postings:
                    db.execute(paths.insert(), postings)
        statement = page.where(bases.c.id > rows[-1].id)


def downgrade() -> None:
    # 这里只撤掉派生路径；旧代码证据、声明和审计仍由各自的表保留。
    op.execute(sa.text(
        f"DELETE FROM {_PATHS} WHERE position = 0 AND entry_id IN "
        "(SELECT id FROM knowledge_entries WHERE codebase_id IS NOT NULL)",
    ))
    op.drop_column(_BASES, "wiki_namespaces")
