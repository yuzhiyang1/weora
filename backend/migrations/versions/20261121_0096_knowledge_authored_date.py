"""在不可变知识版本上保存原文日期。"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "20261121_0096"
down_revision = "20261121_0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 历史行可能已经去掉了前置信息。保留空值，读取时使用旧有入库时间。
    op.add_column("knowledge_revisions", sa.Column("authored_at", sa.DateTime(timezone=True), nullable=True))
    # 已有 PR/MR 版本保留了评审日期。只回填可验证的日期，不改审计时间。
    rows = sa.table(
        "knowledge_revisions", sa.column("entry_id", sa.Uuid), sa.column("revision", sa.Integer),
        sa.column("source", sa.JSON), sa.column("status", sa.String),
        sa.column("authored_at", sa.DateTime(timezone=True)),
    )
    bind = op.get_bind()
    query = sa.select(rows.c.entry_id, rows.c.revision, rows.c.source["review_date"].as_string()).where(
        rows.c.source["type"].as_string() == "pull_request",
    )
    update = rows.update().where(
        rows.c.entry_id == sa.bindparam("target_id"), rows.c.revision == sa.bindparam("target_revision"),
    ).values(authored_at=sa.bindparam("date_value"))
    for batch in bind.execute(query).partitions(1000):
        fixes = []
        for entry, version, raw in batch:
            if not isinstance(raw, str) or len(raw) < 10:
                continue
            try:
                date = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            fixes.append({"target_id": entry, "target_revision": version, "date_value": date})
        if fixes:
            bind.execute(update, fixes)
    # 旧修订可能改过来源。沿用当时已经发布的日期，跳过被驳回的版本。
    dated = rows.alias("dated_knowledge")
    inherited = sa.select(dated.c.authored_at).where(
        dated.c.entry_id == rows.c.entry_id, dated.c.revision < rows.c.revision,
        dated.c.status == "published", dated.c.authored_at.is_not(None),
    ).order_by(dated.c.revision.desc()).limit(1).scalar_subquery()
    origins = sa.select(dated.c.entry_id).where(dated.c.status == "published", dated.c.authored_at.is_not(None))
    bind.execute(rows.update().where(
        rows.c.authored_at.is_(None), rows.c.entry_id.in_(origins), inherited.is_not(None),
    ).values(authored_at=inherited))


def downgrade() -> None:
    statement = sa.text("SELECT 1 FROM knowledge_revisions WHERE authored_at IS NOT NULL LIMIT 1")
    if op.get_bind().scalar(statement):
        raise RuntimeError("已有知识原文日期，无法无损回退。")
    op.drop_column("knowledge_revisions", "authored_at")
