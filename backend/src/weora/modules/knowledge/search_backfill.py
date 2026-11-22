"""按不可变版本回填派生索引，迁移负责选择固定分词版本。"""

from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy.engine import Connection

type IndexDocument = Callable[
    [str, str, list[str], dict[str, str]], tuple[str, int, int, dict[str, tuple[int, int]]],
]


def backfill(db: Connection, postings: sa.Table, index_document: IndexDocument) -> None:
    versions = sa.table(
        "knowledge_revisions",
        sa.column("entry_id", sa.Uuid()), sa.column("revision", sa.Integer()),
        sa.column("workspace_id", sa.Uuid()), sa.column("title", sa.String()),
        sa.column("body", sa.Text()), sa.column("tags", sa.JSON()), sa.column("source", sa.JSON()),
        sa.column("search_domain", sa.String()), sa.column("search_title_length", sa.Integer()),
        sa.column("search_length", sa.Integer()),
    )
    key = sa.tuple_(versions.c.entry_id, versions.c.revision)
    page = sa.select(
        versions.c.entry_id, versions.c.revision, versions.c.workspace_id,
        versions.c.title, versions.c.body, versions.c.tags, versions.c.source,
    ).order_by(versions.c.entry_id, versions.c.revision).limit(50)
    statement = page
    while rows := db.execute(statement).all():
        # 待审核和历史版本也按同一规则处理，审批和回退不需要再触发索引任务。
        for entry_id, version, workspace, title, body, tags, source in rows:
            domain, title_length, length, terms = index_document(title, body, tags, source)
            db.execute(versions.update().where(key == (entry_id, version)).values(
                search_domain=domain, search_title_length=title_length, search_length=length,
            ))
            if terms:
                db.execute(postings.insert(), [
                    dict(entry_id=entry_id, revision=version, workspace_id=workspace,
                         term=term, fields=fields, frequency=frequency)
                    for term, (fields, frequency) in terms.items()
                ])
        statement = page.where(key > (rows[-1].entry_id, rows[-1].revision))
