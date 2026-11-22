"""为授权代码知识提供仓库路由和两跳依赖查找。"""

import re
from collections import defaultdict
from datetime import datetime
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import Row, Select, and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from weora.modules.knowledge.models import KnowledgeCodebase, KnowledgeEntry, KnowledgeRevision, KnowledgeSearchTerm
from weora.modules.knowledge.search import Candidate, SearchHit, code_selection_key
from weora.modules.knowledge.search_tokens_v1 import TITLE
from weora.modules.knowledge.search_tokens_v2 import QueryWords, compile_query
from weora_code_graph.design import DOMAIN_DOCUMENT, CodeDomain, read_code_domain

type Published = Select[tuple[KnowledgeEntry, KnowledgeRevision]]
type RelatedFiles = dict[UUID, list[dict[str, str]]]
type CodeFields = tuple[
    UUID, UUID | None, UUID | None, int, datetime, str, int, int,
    str | None, str | None, list[dict[str, str]],
]
type CodeNode = Row[CodeFields]

_RELATION_WEIGHTS = {"DEPENDS_ON": 0.8, "REFERENCES": 0.8, "IMPLEMENTS": 1.2}
_ENTRY_NODE_BOOST = 8.0
_SOURCE_PATH = KnowledgeRevision.source["path"].as_string()
_WIKI_PAGE = KnowledgeRevision.source["wiki_page"].as_string()


def is_context_page() -> ColumnElement[bool]:
    """概览、模块和文档是默认层可返回的页面。"""
    return and_(
        _SOURCE_PATH == "",
        or_(_WIKI_PAGE.is_(None), _WIKI_PAGE.startswith("modules/", autoescape=True),
            _WIKI_PAGE.startswith("docs/", autoescape=True)),
    )


def code_rows(published: Published) -> Select[CodeFields]:
    return published.where(
        KnowledgeEntry.codebase_id.is_not(None), KnowledgeEntry.kind == "code",
    ).with_only_columns(
        KnowledgeEntry.id, KnowledgeEntry.project_id, KnowledgeEntry.codebase_id, KnowledgeRevision.revision,
        KnowledgeRevision.created_at, KnowledgeRevision.search_domain, KnowledgeRevision.search_title_length,
        KnowledgeRevision.search_length, _SOURCE_PATH.label("path"), _WIKI_PAGE.label("wiki_page"),
        KnowledgeRevision.relations,
    )


def _filename_terms(words: QueryWords) -> set[str]:
    return {word for word in words.indexed if re.fullmatch(r"[a-z][a-z0-9_-]{2,}", word)}


def _filename_matches(stems: set[str]) -> ColumnElement[bool]:
    path = func.lower(_SOURCE_PATH)
    return or_(false(), *(condition for stem in sorted(stems) for condition in (
        path == stem, path.startswith(f"{stem}.", autoescape=True), path.contains(f"/{stem}.", autoescape=True),
    )))


async def code_route(db: AsyncSession, published: Published) -> list[dict[str, object]]:
    """从已经授权的概览和领域资料生成路由。导航本身不计入采纳。"""
    source = KnowledgeRevision.source
    design = source["design_asset"].as_string()
    rows = (await db.execute(
        published.where(
            KnowledgeEntry.codebase_id.is_not(None), KnowledgeEntry.kind == "code",
            source["path"].as_string() == "", or_(design.is_(None), design == DOMAIN_DOCUMENT),
            source["wiki_page"].as_string().is_(None),
        ).with_only_columns(KnowledgeEntry.codebase_id, KnowledgeEntry.id, source)
        .order_by(KnowledgeEntry.codebase_id, KnowledgeEntry.id)
    )).tuples()
    overviews: dict[UUID, tuple[UUID, str]] = {}
    metadata: dict[UUID, CodeDomain] = {}
    for codebase, entry, provenance in rows:
        if codebase is None:
            continue
        if provenance.get("design_asset") == DOMAIN_DOCUMENT:
            if profile := read_code_domain(provenance.get("domain_profile", "")):
                metadata[codebase] = profile
        else:
            overviews[codebase] = (entry, provenance["ref"])

    ordered = sorted(overviews.items(), key=lambda item: (item[1][1].casefold(), str(item[0])))
    routes: list[dict[str, object]] = []
    remaining = 7000
    for codebase, (entry, name) in ordered:
        profile = metadata.get(codebase)
        description = profile.description if profile else ""
        keywords = list(profile.keywords[:6]) if profile else []
        domain = profile.domain if profile else "其他"
        remaining -= len(name) + len(description) + sum(map(len, keywords)) + len(domain) + 40
        if remaining < 0:
            break
        routes.append({
            "entry_id": str(entry), "name": name, "domain": domain, "description": description, "keywords": keywords,
        })
    return routes


def _candidate(row: CodeNode, *, named: bool = False) -> Candidate:
    return Candidate(
        row.id, row.revision, "code", row.codebase_id, row.project_id,
        row.search_domain, row.search_title_length, row.search_length,
        1, row.search_length, row.created_at, hidden=False, named_module=named,
    )


async def context_graph(
    db: AsyncSession, published: Published, ranked: list[SearchHit], *, query: str,
) -> list[SearchHit]:
    """匹配当前文件声明并沿依赖找到概览或模块，始终保持默认层的页面范围。"""
    words = ranked[0].query if ranked else compile_query(query, with_coverage=False)
    terms = set(words.indexed)
    if not terms:
        return ranked
    indexed = code_rows(published)
    # 先用倒排索引筛选候选，再核对清单内声明，避免把仅引用某函数的文件也当成起点。
    match = select(KnowledgeSearchTerm.entry_id).where(
        KnowledgeSearchTerm.workspace_id == KnowledgeEntry.workspace_id,
        KnowledgeSearchTerm.entry_id == KnowledgeEntry.id,
        KnowledgeSearchTerm.revision == KnowledgeRevision.revision,
        KnowledgeSearchTerm.term.in_(terms),
    ).correlate(KnowledgeEntry, KnowledgeRevision).exists()
    candidates = (await db.execute(indexed.where(
        _SOURCE_PATH != "", or_(match, _filename_matches(_filename_terms(words))),
    ).with_only_columns(
        KnowledgeEntry.id, KnowledgeEntry.codebase_id, KnowledgeRevision.revision, _SOURCE_PATH.label("path"),
        KnowledgeRevision.source["blob"].as_string().label("blob"),
    ).order_by(KnowledgeEntry.id))).all()
    if not candidates:
        return ranked
    catalogs = await db.execute(select(KnowledgeCodebase.id, KnowledgeCodebase.manifest).where(
        KnowledgeCodebase.id.in_({row.codebase_id for row in candidates}),
    ))
    manifests = {codebase: manifest for codebase, manifest in catalogs.tuples()}
    anchors: dict[UUID, int] = {}
    codebases: set[UUID] = set()
    for row in candidates:
        if not row.path or row.codebase_id is None:
            continue
        fact = manifests.get(row.codebase_id, {}).get(row.path)
        if fact is None or fact["oid"] != row.blob:
            continue
        path = PurePosixPath(row.path.lower())
        if terms & {path.stem, path.name} or any(symbol["name"].casefold() in terms for symbol in fact["symbols"]):
            anchors[row.id] = row.revision
            codebases.add(row.codebase_id)
    if not anchors:
        return ranked

    # 文件和目标页面从同一数据库快照读取，避免把新旧索引的关系拼到一起。
    rows = (await db.execute(indexed.where(
        KnowledgeEntry.codebase_id.in_(codebases), or_(_SOURCE_PATH != "", is_context_page()),
    ))).all()
    files = {row.id: row for row in rows if row.path}
    summaries = {row.id: row for row in rows if row.path == ""}
    roots = [identity for identity, revision in anchors.items()
             if identity in files and files[identity].revision == revision]
    if not roots:
        return ranked
    weights, _ = _graph_links(files)
    scores = _neighbor_boosts(weights, roots) | dict.fromkeys(roots, _ENTRY_NODE_BOOST)
    page_scores = {
        identity: max((scores.get(UUID(link["target"]), 0.0) for link in page.relations
                       if link["kind"] in {"REFERENCES", "CONTAINS"}), default=0.0)
        for identity, page in summaries.items()
    }
    return _with_boosts(ranked, summaries, {page: score for page, score in page_scores.items() if score}, words)


async def lookup_graph(
    db: AsyncSession, published: Published, ranked: list[SearchHit], *, query: str = "",
) -> tuple[list[SearchHit], RelatedFiles]:
    # 只读命中仓库的边和元数据。图谱两端都以当前可见的发布版本为准。
    indexed = code_rows(published)
    words = ranked[0].query if ranked else compile_query(query, with_coverage=False)
    stems = _filename_terms(words)
    if stems:
        # 分词器保留文件扩展名，图谱查询额外允许用完整文件名词干定位起点。
        found = {hit.candidate.entry for hit in ranked}
        named = (await db.execute(indexed.where(_filename_matches(stems)).order_by(KnowledgeEntry.id).limit(32))).all()
        # 文件名词干完全匹配时，把原始权重设到压缩后恰好相关的下限。
        ranked = ranked + [
            SearchHit.code(_candidate(row, named=True), 3.0, words) for row in named
            if row.id not in found and row.path and PurePosixPath(row.path.lower()).stem in stems
        ]
    anchors = sorted(
        (hit for hit in ranked if hit.candidate.codebase and hit.code_weight is not None),
        key=code_selection_key, reverse=True,
    )
    if not anchors:
        return ranked, {}
    codebases = list(dict.fromkeys(hit.candidate.codebase for hit in anchors))[:8]
    rows = (await db.execute(indexed.where(KnowledgeEntry.codebase_id.in_(codebases)))).all()
    nodes = {row.id: row for row in rows}
    weights, related = _graph_links(nodes)
    roots = [
        hit.candidate.entry for hit in anchors if
        (hit.candidate.named_module or any(fields & TITLE for fields, _ in hit.candidate.matches.values()))
        and hit.candidate.entry in nodes and nodes[hit.candidate.entry].revision == hit.candidate.revision
    ][:16]
    return _with_boosts(ranked, nodes, _neighbor_boosts(weights, roots), words), related


def _graph_links(nodes: dict[UUID, CodeNode]) -> tuple[dict[UUID, dict[UUID, float]], RelatedFiles]:
    weights: dict[UUID, dict[UUID, float]] = defaultdict(dict)
    related: RelatedFiles = {}
    for identifier, row in nodes.items():
        seen: set[UUID] = set()
        related[identifier] = []
        for link in row.relations:
            weight = _RELATION_WEIGHTS.get(link["kind"])
            if weight is None and link["kind"] != "CONTAINS":
                continue
            target = UUID(link["target"])
            if target == identifier or target not in nodes:
                continue
            # 聚合页面可展示出处，但不能把无关文件连成两跳代码依赖。
            if weight is not None and row.wiki_page is None and nodes[target].wiki_page is None:
                weights[identifier][target] = max(weight, weights[identifier].get(target, 0))
                weights[target][identifier] = max(weight, weights[target].get(identifier, 0))
            path = nodes[target].path
            if path and target not in seen and len(related[identifier]) < 15:
                seen.add(target)
                related[identifier].append({"id": str(target), "path": path})
    return weights, related


def _neighbor_boosts(weights: dict[UUID, dict[UUID, float]], roots: list[UUID]) -> dict[UUID, float]:
    boosts: dict[UUID, float] = {}
    for identifier in roots:
        for neighbor, weight in weights.get(identifier, {}).items():
            boosts[neighbor] = max(weight, boosts.get(neighbor, 0))
            for second, next_weight in weights.get(neighbor, {}).items():
                if second != identifier:
                    boosts[second] = max(next_weight / 2, boosts.get(second, 0))
    return boosts


def _with_boosts(
    ranked: list[SearchHit], nodes: dict[UUID, CodeNode], boosts: dict[UUID, float], words: QueryWords,
) -> list[SearchHit]:
    result = []
    for hit in ranked:
        identifier = hit.candidate.entry
        boost = boosts.pop(identifier, 0.0)
        if boost and nodes[identifier].revision == hit.candidate.revision:
            hit = SearchHit.code(hit.candidate, (hit.code_weight or 0.0) + boost, hit.query)
        result.append(hit)
    for identifier, boost in boosts.items():
        row = nodes[identifier]
        result.append(SearchHit.code(_candidate(row), boost, words))
    return result
