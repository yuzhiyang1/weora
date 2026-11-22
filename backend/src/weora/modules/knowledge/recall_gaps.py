"""从当前项目已发布的索引缺口页读取匹配的结构化事实。"""

import json
from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha256
from itertools import batched
from typing import cast
from uuid import UUID

from sqlalchemy import func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.code_wiki_paths_v1 import wiki_locations
from weora.modules.knowledge.models import KnowledgeCodebase, KnowledgeCodeGapSource, KnowledgeEntry, KnowledgeRevision
from weora.modules.knowledge.recall_code import Published
from weora.modules.knowledge.search_tokens_v2 import compile_query
from weora_code_graph.coverage import GAP_PAGE, GapFinding, matches_gap


def _key(value: str) -> str | None:
    return sha256(value.casefold().encode()).hexdigest() if value else None


async def add_gap_sources(
    db: AsyncSession, version: KnowledgeRevision, findings: Sequence[GapFinding],
) -> None:
    """正文版本和精确位置同事务发布，返回数据仍可保持有界。"""
    rows: list[dict[str, str | int | UUID | None]] = []
    seen: set[tuple[str, str]] = set()
    for gap in findings:
        for item in gap.get("sources", []):
            path, name, line = item["path"], item["name"], item["line"]
            digest = sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
            identity = (gap["id"], digest)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append({
                "entry_id": version.entry_id, "revision": version.revision, "finding": gap["id"], "digest": digest,
                "path": path, "name": name, "line": line, "name_key": _key(name),
                "alias_key": _key(item.get("alias", "")),
                "path_key": _key(path), "basename_key": _key(path.rsplit("/", 1)[-1]),
            })
    for batch in batched(rows, 2000):
        await db.execute(insert(KnowledgeCodeGapSource), list(batch))


async def _matching_sources(
    db: AsyncSession, scoped: Published, terms: set[str],
) -> dict[tuple[UUID, int, str], list[dict[str, object]]]:
    keys = {_key(term) for term in terms if term}
    if not keys:
        return {}
    visible = scoped.where(
        KnowledgeRevision.source["wiki_page"].as_string() == GAP_PAGE, KnowledgeRevision.status == "published",
    ).with_only_columns(KnowledgeEntry.id.label("entry"), KnowledgeRevision.revision.label("version")).subquery()
    row = KnowledgeCodeGapSource
    ranked = select(
        row.entry_id, row.revision, row.finding, row.path, row.name, row.line,
        func.row_number().over(
            partition_by=(row.entry_id, row.revision, row.finding),
            order_by=(row.path, row.line, row.name),
        ).label("position"),
    ).join_from(row, visible, (row.entry_id == visible.c.entry) & (row.revision == visible.c.version)).where(or_(
        row.name_key.in_(keys), row.alias_key.in_(keys), row.path_key.in_(keys), row.basename_key.in_(keys),
    )).subquery()
    result: dict[tuple[UUID, int, str], list[dict[str, object]]] = defaultdict(list)
    for item in await db.execute(select(ranked).where(ranked.c.position <= 10).order_by(ranked.c.position)):
        result[item.entry_id, item.revision, item.finding].append(
            {"name": item.name, "path": item.path, "line": item.line},
        )
    return result


async def published_gaps(db: AsyncSession, scoped: Published, query: str) -> list[dict[str, object]]:
    rows = await db.execute(scoped.join(KnowledgeCodebase, KnowledgeCodebase.id == KnowledgeEntry.codebase_id).where(
        KnowledgeRevision.source["wiki_page"].as_string() == GAP_PAGE, KnowledgeRevision.status == "published",
    ).with_only_columns(
        KnowledgeEntry.id, KnowledgeCodebase.id, KnowledgeCodebase.wiki_namespaces, KnowledgeRevision.revision,
        KnowledgeRevision.title, KnowledgeRevision.source["gap_findings"].as_string(),
        KnowledgeRevision.source["gap_evidence_sha"].as_string(),
    ).order_by(KnowledgeRevision.title, KnowledgeEntry.id))
    # 保留相对导入的前导点和作用域前缀，同时允许粘贴 Windows 源码路径。
    literals = {word.strip("\"'`「」()[]{}，。,:;").casefold().replace("\\", "/") for word in query.split()}
    terms = literals | set(compile_query(query, with_coverage=False).indexed)
    items: list[dict[str, object]] = []
    matching = await _matching_sources(db, scoped, terms) if query else {}
    for page, index, aliases, revision, title, payload, evidence_sha in rows.tuples():
        if not payload:
            continue
        # 仅代码索引器写入此字段。旧索引没有缺口记录时无需推断覆盖结论。
        findings = cast(list[GapFinding], json.loads(payload))
        paths = wiki_locations(tuple(aliases), {"wiki_page": GAP_PAGE})
        for gap in findings:
            occurrences = matching.get((page, revision, gap["id"]), [])
            if not query or occurrences or matches_gap(gap, query, terms if not evidence_sha else set()):
                items.append(dict(gap) | ({"evidence": occurrences} if occurrences else {}) | {
                    "entry_id": str(page), "codebase_id": str(index), "title": title,
                    "path": paths[0] if paths else None, "status": "open",
                })
                if len(items) == 10:
                    return items
    return items
