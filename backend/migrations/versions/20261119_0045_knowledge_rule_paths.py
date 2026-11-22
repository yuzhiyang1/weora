"""为规则版本保留适用路径，从历史正文中的前置信息回填。"""

import sqlalchemy as sa
from alembic import op

from weora.modules.knowledge.rule_paths import markdown_rule_paths

revision = "20261119_0045"
down_revision = "20261119_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_revisions", sa.Column("rule_paths", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    db = op.get_bind()
    entries = sa.table("knowledge_entries", sa.column("id", sa.Uuid()), sa.column("kind", sa.String()))
    versions = sa.table(
        "knowledge_revisions", sa.column("entry_id", sa.Uuid()), sa.column("revision", sa.Integer()),
        sa.column("body", sa.Text()), sa.column("rule_paths", sa.JSON()),
    )
    key = sa.tuple_(versions.c.entry_id, versions.c.revision)
    base = sa.select(versions.c.entry_id, versions.c.revision, versions.c.body).join(
        entries, entries.c.id == versions.c.entry_id,
    ).where(entries.c.kind == "rule").order_by(versions.c.entry_id, versions.c.revision).limit(50)
    statement = base
    while rows := db.execute(statement).all():
        for entry, version, body in rows:
            if paths := markdown_rule_paths(body):
                db.execute(versions.update().where(key == (entry, version)).values(rule_paths=paths))
        statement = base.where(key > (rows[-1].entry_id, rows[-1].revision))


def downgrade() -> None:
    db = op.get_bind()
    # 显式 paths 可能只存于版本元数据，拒绝回退时静默丢失这些内容。
    if db.scalar(sa.text("SELECT 1 FROM knowledge_revisions WHERE json_array_length(rule_paths) > 0 LIMIT 1")):
        raise RuntimeError("存在规则适用路径，请保留版本元数据后再处理迁移回退。")
    op.drop_column("knowledge_revisions", "rule_paths")
