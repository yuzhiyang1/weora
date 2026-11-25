"""保存知识条目的逻辑路径标识，历史版本继续使用原 UUID。"""

import sqlalchemy as sa
from alembic import op

from weora.modules.knowledge.resource_paths import resource_key

revision = "20261119_0043"
down_revision = "20261119_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_entries", sa.Column("resource_key", sa.String(64)))
    db = op.get_bind()
    entries = sa.table("knowledge_entries", sa.column("id", sa.Uuid()), sa.column("resource_key", sa.String(64)))
    revisions = sa.table(
        "knowledge_revisions", sa.column("entry_id", sa.Uuid()), sa.column("revision", sa.Integer()),
        sa.column("source", sa.JSON()),
    )
    # 首个版本确定资源身份，正文修订和发布状态均不改变同路径的覆盖关系。
    first = sa.select(revisions.c.entry_id, revisions.c.source).where(
        revisions.c.revision == 1,
    ).order_by(revisions.c.entry_id).limit(500)
    page = first
    while rows := db.execute(page).all():
        updates = [
            {"target_id": entry_id, "target_key": key}
            for entry_id, source in rows if (key := resource_key(source)) is not None
        ]
        if updates:
            db.execute(entries.update().where(entries.c.id == sa.bindparam("target_id")).values(
                resource_key=sa.bindparam("target_key"),
            ), updates)
        page = first.where(revisions.c.entry_id > rows[-1].entry_id)
    op.create_index(
        "ix_knowledge_entries_resource", "knowledge_entries",
        ["workspace_id", "project_id", "kind", "resource_key", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_entries_resource", table_name="knowledge_entries")
    op.drop_column("knowledge_entries", "resource_key")
