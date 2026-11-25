"""为已有及后续发布的知识版本索引 Markdown 逻辑路径。"""

from uuid import UUID

import sqlalchemy as sa
from alembic import op

from weora.modules.knowledge.markdown_paths_v1 import entry_path, indexed_paths

revision = "20261120_0079"
down_revision = "20261120_0078"
branch_labels = None
depends_on = None

_TABLE = "knowledge_markdown_paths"


def upgrade() -> None:
    paths = op.create_table(
        _TABLE,
        sa.Column("entry_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["entry_id", "revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
    )
    versions = sa.table(
        "knowledge_revisions",
        sa.column("entry_id", sa.Uuid()), sa.column("revision", sa.Integer()),
        sa.column("body", sa.Text()), sa.column("source", sa.JSON()),
    )
    entries = sa.table("knowledge_entries", sa.column("id", sa.Uuid()), sa.column("kind", sa.String(16)))
    key = sa.tuple_(versions.c.entry_id, versions.c.revision)
    page = sa.select(
        versions.c.entry_id, versions.c.revision, entries.c.kind, versions.c.body, versions.c.source,
    ).join(entries, entries.c.id == versions.c.entry_id).where(
        entries.c.kind.in_(("rule", "doc", "learning", "skill")),
    ).order_by(versions.c.entry_id, versions.c.revision).limit(50)
    db = op.get_bind()
    statement = page
    previous = origin = None
    while rows := db.execute(statement).all():
        postings: list[dict[str, UUID | int | str]] = []
        for entry, version, kind, body, source in rows:
            # 一条知识的逻辑身份由首次贡献确定，修订中的来源说明可以变化。
            if entry != previous:
                previous, origin = entry, entry_path(entry, kind, source)
            postings.extend(
                dict(entry_id=entry, revision=version, key=digest, path=path, position=position)
                for position, (path, digest) in enumerate(indexed_paths(kind, origin, body))
            )
        if postings:
            db.execute(paths.insert(), postings)
        statement = page.where(key > (rows[-1].entry_id, rows[-1].revision))
    op.create_index("ix_knowledge_markdown_paths_lookup", _TABLE, ["key", "position"])


def downgrade() -> None:
    # 这些路径全由已有的来源和正文重建，回退不会丢失贡献或审核记录。
    op.drop_table(_TABLE)
