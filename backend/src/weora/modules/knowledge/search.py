"""从有权访问的发布版本检索词项，按正文证据和使用反馈排序。"""

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from heapq import nlargest
from itertools import batched
from math import log, log2, sqrt
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Select,
    String,
    Uuid,
    case,
    cast,
    false,
    func,
    insert,
    literal,
    null,
    or_,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CTE

from weora.modules.knowledge.domain import FeedbackTotals
from weora.modules.knowledge.feedback_projection import FeedbackProjection, projected_history
from weora.modules.knowledge.models import (
    KnowledgeEntry,
    KnowledgeRecall,
    KnowledgeRevision,
    KnowledgeSearchTerm,
    KnowledgeSkillRecall,
)
from weora.modules.knowledge.recall_metrics import feedback_totals, recall_aggregates
from weora.modules.knowledge.search_domains_v3 import query_domain, resource_domain
from weora.modules.knowledge.search_tokens_v1 import TAG, TEXT, TITLE
from weora.modules.knowledge.search_tokens_v2 import QueryWords, compile_query, document_index, title_and_tag_terms
from weora.modules.skills.infrastructure.knowledge_catalog import matching_skill_terms

if TYPE_CHECKING:
    from _hashlib import HASH

RELEVANCE_FLOOR = 4.0
_FIELD_WEIGHTS = ((TITLE, 3), (TAG, 2), (TEXT, 1))
_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9_-]{4,}")
_MODULE_SHAPE = re.compile(r"[-_0-9]|[a-z][A-Z]")
_MODULE_WORD = re.compile(r"[a-z0-9_-]+")
_FILE_SUFFIX = re.compile(r"\.[a-z0-9]+$")
_DOMAIN_WEIGHTS = {
    "technical": {"technical": 1.0, "neutral": 0.85, "ops": 0.5, "support": 0.3},
    "ops": {"technical": 0.7, "neutral": 0.85, "ops": 1.0, "support": 0.3},
    "neutral": {"technical": 1.0, "neutral": 0.85, "ops": 0.75, "support": 0.3},
    "support": {"technical": 0.8, "neutral": 0.85, "ops": 0.5, "support": 1.0},
}


async def add_indexed_revisions(
    db: AsyncSession, revisions: Sequence[KnowledgeRevision], *, kind: str = "code", path: str | None = None,
) -> None:
    """同批保存原始版本和词项，避免发布后出现查询不到的窗口。"""
    for start in range(0, len(revisions), 16):
        postings: list[dict[str, UUID | int | str]] = []
        for version in revisions[start:start + 16]:
            domain, title_length, length, terms = document_index(
                version.title, version.body, version.tags, version.source,
            )
            version.search_domain = (
                domain if kind == "code" else resource_domain(version.tags, kind, version.source, path)
            )
            version.search_title_length = title_length
            version.search_length = length
            db.add(version)
            postings.extend(
                dict(workspace_id=version.workspace_id, entry_id=version.entry_id, revision=version.revision,
                     term=term, fields=fields, frequency=frequency)
                for term, (fields, frequency) in terms.items()
            )
        await db.flush()
        for offset in range(0, len(postings), 2000):
            await db.execute(insert(KnowledgeSearchTerm), postings[offset:offset + 2000])


@dataclass
class Candidate:
    entry: UUID
    revision: int
    kind: str
    codebase: UUID | None
    project: UUID | None
    domain: str
    title_length: int
    length: int
    population: int
    corpus_length: int
    created_at: datetime
    hidden: bool
    named_module: bool = False
    matches: dict[str, tuple[int, int]] = field(default_factory=dict)
    skill_version_id: UUID | None = None
    author: UUID | None = None
    title: str | None = None
    authored_at: datetime | None = None

    @property
    def corpus(self) -> tuple[UUID | None, str]:
        """共享知识与每个项目单独统计同领域词频。"""
        return self.project, "code" if self.kind == "code" else self.domain

    @property
    def original_date(self) -> datetime:
        return self.authored_at or self.created_at


@dataclass(frozen=True)
class SearchHit:
    candidate: Candidate
    score: float
    rank: float
    relevant: bool
    query: QueryWords
    threshold: float
    code_weight: float | None = None

    @classmethod
    def code(cls, candidate: Candidate, weight: float, query: QueryWords) -> "SearchHit":
        # 保留完整 BM25 和图谱权重，候选截取不能使用压缩后的展示分数。
        score = min(10.0, 2 * log2(1 + weight))
        rank = score * 0.8 + (0.5 if candidate.named_module else 0)
        return cls(candidate, score, rank, score >= RELEVANCE_FLOOR, query, RELEVANCE_FLOOR, code_weight=weight)

    def evidence(self, title: str, tags: list[str]) -> dict[str, object]:
        matched: list[str] = []
        missing: list[str] = []
        if self.candidate.kind != "code" and any(
            fields & (TITLE | TAG) for fields, _ in self.candidate.matches.values()
        ):
            matched, missing = self.query.coverage(title_and_tag_terms(title, tags))
        return {
            "score": round(self.score, 4), "relevant": self.relevant,
            "matched_terms": matched, "missing_terms": missing,
        }


def names_module(identifiers: set[str], path: str, title: str, repository: str) -> bool:
    """只匹配整段模块名或文件名，避免同前缀的其他服务得到加分。"""
    path_parts = {
        token
        for part in path.lower().split("/")
        for token in _FILE_SUFFIX.sub("", part).split("__")
    }
    return bool(identifiers & (path_parts | set(_MODULE_WORD.findall(f"{title} {repository}".lower()))))


async def rank_published(
    db: AsyncSession, published: Select[tuple[KnowledgeEntry, KnowledgeRevision]], workspace: UUID, query: str,
    *, recall: bool, hidden: ColumnElement[bool], with_coverage: bool, feedback: FeedbackProjection | None = None,
    registry: CTE | None = None,
) -> list[SearchHit]:
    """先限制项目和发布版本，再计算词频、反馈和候选，以免跨范围影响排序。"""
    words = compile_query(query, with_coverage=with_coverage)
    terms = words.indexed
    if not terms:
        return []
    kind = KnowledgeEntry.kind
    domain = KnowledgeRevision.search_domain
    scoped = published.with_only_columns(
        KnowledgeEntry.id, kind, KnowledgeEntry.codebase_id, KnowledgeEntry.project_id,
        KnowledgeRevision.revision, domain.label("domain"),
        KnowledgeRevision.search_title_length.label("title_length"),
        KnowledgeRevision.search_length.label("length"), KnowledgeRevision.created_at,
        hidden.label("hidden"), cast(null(), Uuid()).label("skill_version_id"),
        (case((kind != "code", KnowledgeRevision.author_id)) if recall else cast(null(), Uuid())).label("author"),
        (case((kind != "code", KnowledgeRevision.title)) if recall else cast(null(), String())).label("title"),
        KnowledgeRevision.authored_at,
    )
    identifiers = {token.lower() for token in _IDENTIFIER.findall(query) if _MODULE_SHAPE.search(token)}
    if identifiers:
        source = KnowledgeRevision.source
        path = source["path"].as_string()
        # 设计页也用空路径。仅用仓库概览取名字，并确保每个仓库只关联一行。
        overviews = published.with_only_columns(
            KnowledgeEntry.codebase_id, func.min(source["ref"].as_string()).label("name"),
        ).where(
            kind == "code", KnowledgeEntry.codebase_id.is_not(None), path == "",
            source["design_asset"].as_string().is_(None), source["wiki_page"].as_string().is_(None),
        ).group_by(KnowledgeEntry.codebase_id).cte("repository_names")
        # Wiki 与设计页的源码路径为空，使用本版页面路径识别查询点名的模块。
        page_path = func.coalesce(func.nullif(path, ""), source["wiki_page"].as_string(),
                                  source["design_asset"].as_string(), path)
        scoped = scoped.outerjoin(overviews, overviews.c.codebase_id == KnowledgeEntry.codebase_id).add_columns(
            case((kind == "code", KnowledgeRevision.title)).label("code_title"),
            case((kind == "code", page_path)).label("code_path"), overviews.c.name.label("repository_name"),
        )
    if registry is None:
        corpus = scoped.cte("knowledge_corpus")
    else:
        skills = select(
            registry.c.id, literal("skill"), cast(null(), Uuid()), registry.c.project_id,
            literal(0), registry.c.domain, registry.c.title_length, registry.c.length, registry.c.published_at,
            false(), registry.c.version_id, registry.c.author, registry.c.title,
            cast(null(), DateTime(timezone=True)),
        )
        if identifiers:
            skills = skills.add_columns(*(cast(null(), String) for _ in range(3)))
        corpus = scoped.union_all(skills).cte("knowledge_corpus")
    partition = (corpus.c.project_id, case((corpus.c.kind == "code", "code"), else_=corpus.c.domain))
    visible = select(
        corpus, func.count().over(partition_by=partition).label("population"),
        func.sum(corpus.c.length).over(partition_by=partition).label("corpus_length"),
    ).cte("visible_knowledge")

    index = KnowledgeSearchTerm
    matches = select(
        *visible.c, index.term, index.fields, index.frequency,
    ).join_from(visible, index, (index.entry_id == visible.c.id) & (index.revision == visible.c.revision)).where(
        visible.c.skill_version_id.is_(None), index.workspace_id == workspace, index.term.in_(terms),
    )
    if registry is None:
        matched = matches.cte("matching_knowledge")
    else:
        postings = matching_skill_terms(workspace, terms)
        matched = matches.union_all(select(
            *visible.c, postings.c.term, postings.c.fields, postings.c.frequency,
        ).join_from(visible, postings, postings.c.version_id == visible.c.skill_version_id)).cte("matching_knowledge")
    candidates: dict[UUID, Candidate] = {}
    df: Counter[tuple[tuple[UUID | None, str], str, int]] = Counter()
    rows = await db.stream(select(matched).execution_options(yield_per=500))
    try:
        async for row in rows:
            candidate = candidates.get(row.id)
            if candidate is None:
                candidate = Candidate(
                    row.id, row.revision, row.kind, row.codebase_id, row.project_id, row.domain,
                    row.title_length, row.length, row.population, row.corpus_length, row.created_at, row.hidden,
                    named_module=bool(identifiers and row.kind == "code" and names_module(
                        identifiers, row.code_path or "", row.code_title or "", row.repository_name or "",
                    )),
                    skill_version_id=row.skill_version_id,
                    author=row.author, title=row.title, authored_at=row.authored_at,
                )
                candidates[row.id] = candidate
            candidate.matches[row.term] = (row.fields, row.frequency)
            if candidate.kind == "code":
                df[candidate.corpus, row.term, TEXT] += 1
            else:
                for mask, _ in _FIELD_WEIGHTS:
                    if row.fields & mask:
                        df[candidate.corpus, row.term, mask] += 1
    finally:
        await rows.close()
    if not candidates:
        return []
    authored_scopes = {item.project for item in candidates.values() if item.kind != "code" and not item.hidden}
    totals_by_entry, history_scopes = await _feedback(db, visible, matched, feedback, authored_scopes)
    now = datetime.now(UTC)
    weights = _DOMAIN_WEIGHTS[query_domain(terms)]
    query_norm = sqrt(len(terms))
    hits = []
    for candidate in candidates.values():
        # 同路径的共享版本仍属于共享词频语料，但不能作为项目召回结果返回。
        if candidate.hidden:
            continue
        if candidate.kind == "code":
            hits.append(SearchHit.code(candidate, _code_bm25(candidate, df), words))
            continue
        has_context = any(fields & (TITLE | TAG) for fields, _ in candidate.matches.values())
        if recall and not has_context and candidate.kind != "doc":
            continue
        population_size = candidate.population
        threshold = max(RELEVANCE_FLOOR, 1.35 * (1 + log((population_size + 1) / 2)))
        title, tag, body = (
            sum(
                weight * (1 + log((population_size + 1) / (df[candidate.corpus, term, mask] + 1)))
                for term, (fields, _) in candidate.matches.items() if fields & mask
            )
            for mask, weight in _FIELD_WEIGHTS
        )
        totals = totals_by_entry.get(candidate.entry)
        bonus = min(5, (totals.recalls * 0.3 + totals.upvotes) * 0.5) if totals else 0
        # 每个索引范围独立启用冷热权重。共享反馈不会降低项目新知识的分值。
        confidence = totals.confidence(now) if totals else 0
        hotness = 0.8 + 0.2 * min(1, confidence / 0.5) if candidate.project in history_scopes else 1.0
        multiplier = hotness * weights[candidate.domain]
        if candidate.kind in {"rule", "skill"}:
            multiplier *= 1.1
        score = ((title + tag + body) / query_norm + bonus) * multiplier
        rank = ((title / sqrt(max(1, candidate.title_length / 4.5)) + tag + body) / query_norm + bonus) * multiplier
        rank = min(10, max(0, 4 + 2 * log2(rank / threshold)))
        exact_title = all(candidate.matches.get(term, (0, 0))[0] & TITLE for term in terms)
        relevant = ((has_context or candidate.kind == "doc") and score >= threshold) or (not recall and exact_title)
        hits.append(SearchHit(candidate, score, rank, relevant, words, threshold))
    return await _collapse_reshares(db, workspace, hits) if recall else hits


async def _collapse_reshares(db: AsyncSession, workspace: UUID, hits: list[SearchHit]) -> list[SearchHit]:
    """只比较同范围、同作者、同 UTC 日、同类型、同领域和同标题的有权命中。"""
    groups: dict[tuple[UUID | None, UUID, date, str, str, str], list[SearchHit]] = defaultdict(list)
    for hit in hits:
        item = hit.candidate
        if item.author is not None and item.title is not None:
            original_day = item.original_date.date()
            key = (item.project, item.author, original_day, item.kind, item.domain, item.title)
            groups[key].append(hit)
    repeated = [group for group in groups.values() if len(group) > 1]
    if not repeated:
        return hits

    # 候选先通过发布与授权过滤。只为可能重复的版本读取完整索引，无需载入正文。
    compared = [hit.candidate for group in repeated for hit in group]
    native = [(item.entry, item.revision) for item in compared if item.skill_version_id is None]
    skills = {item.skill_version_id: item.entry for item in compared if item.skill_version_id is not None}
    digests: dict[UUID, HASH] = defaultdict(sha256)
    terms = KnowledgeSearchTerm
    for batch in batched(native, 200):
        query = select(terms.entry_id, terms.term, terms.fields).where(
            terms.workspace_id == workspace, tuple_(terms.entry_id, terms.revision).in_(batch),
        ).order_by(terms.entry_id, terms.term)
        rows = await db.stream(query)
        try:
            async for entry, term, fields in rows.tuples():
                digests[entry].update(term.encode() + bytes((0, fields)))
        finally:
            await rows.close()
    for versions in batched(skills, 200):
        postings = matching_skill_terms(workspace, versions=versions)
        skill_query = select(postings.c.version_id, postings.c.term, postings.c.fields).order_by(
            postings.c.version_id, postings.c.term,
        )
        skill_rows = await db.stream(skill_query)
        try:
            async for version, term, fields in skill_rows.tuples():
                digests[skills[version]].update(term.encode() + bytes((0, fields)))
        finally:
            await skill_rows.close()

    omitted: set[UUID] = set()
    for group in repeated:
        seen: set[bytes] = set()
        # 匹配词项相同时，反馈差异会反映到原始分数，展示分压缩后也能保留较优记录。
        ordered = sorted(group, key=lambda hit: (
            hit.relevant, hit.rank, hit.score, hit.candidate.original_date, hit.candidate.created_at,
            hit.candidate.entry,
        ), reverse=True)
        for hit in ordered:
            identity = hit.candidate.entry
            if identity not in digests:
                continue
            digest = digests[identity].digest()
            if digest in seen:
                omitted.add(identity)
            seen.add(digest)
    return [hit for hit in hits if hit.candidate.entry not in omitted]


async def _feedback(
    db: AsyncSession, visible: CTE, matches: CTE, feedback: FeedbackProjection | None, scopes: set[UUID | None],
) -> tuple[dict[UUID, FeedbackTotals], set[UUID | None]]:
    if not scopes:
        return {}, set()
    # 聚合匹配条目和设备待送反馈的目标。后者即使未命中查询，也会启用所属范围的冷热权重。
    matched: ColumnElement[bool] = visible.c.id.in_(select(matches.c.id).where(matches.c.kind != "code").distinct())
    if feedback is not None and feedback.pending:
        matched = or_(matched, visible.c.id.in_({vote.entry for vote in feedback.pending}))
    targets = select(visible.c.id, visible.c.project_id, visible.c.skill_version_id, visible.c.hidden).where(
        visible.c.kind != "code", matched,
    ).cte("recall_feedback_targets")
    entries = select(targets.c.id)
    columns = (
        "entry_id", "user_id", "session_key", "recall_count", "last_recalled_at", "positive", "voted_at",
        "credit_source", "manual_vote_at", "manual_vote_id",
    )
    native = select(*(getattr(KnowledgeRecall, column) for column in columns)).where(
        KnowledgeRecall.entry_id.in_(entries),
    )
    skills = select(*(getattr(KnowledgeSkillRecall, column) for column in columns)).where(
        KnowledgeSkillRecall.entry_id.in_(entries),
    )
    confirmed = native.union_all(skills).cte("confirmed_recall_feedback")
    effective = projected_history(targets, confirmed, feedback) if feedback is not None else None
    history = aliased(KnowledgeRecall, effective if effective is not None else confirmed, adapt_on_names=True)
    rows = (await db.execute(select(
        history.entry_id, targets.c.project_id, *recall_aggregates(history),
    ).select_from(history).join(targets, targets.c.id == history.entry_id).group_by(
        history.entry_id, targets.c.project_id,
    ))).mappings().all()
    totals = {row["entry_id"]: feedback_totals(row) for row in rows}
    observed = {row["project_id"] for row in rows}
    if missing := scopes - observed:
        # 其余范围只判断是否有历史，无需把未命中的反馈也聚合进查询。
        known = await db.scalars(select(visible.c.project_id).where(
            visible.c.kind != "code",
            or_(*(visible.c.project_id.is_not_distinct_from(scope) for scope in missing)),
            or_(
                select(KnowledgeRecall.entry_id).where(KnowledgeRecall.entry_id == visible.c.id).exists(),
                select(KnowledgeSkillRecall.entry_id).where(KnowledgeSkillRecall.entry_id == visible.c.id).exists(),
            ),
        ).distinct())
        observed.update(known)
    return totals, observed


def _code_bm25(candidate: Candidate, df: Counter[tuple[tuple[UUID | None, str], str, int]]) -> float:
    """按项目范围的代码文档长度和词频计算原始 BM25。"""
    average = max(1.0, candidate.corpus_length / candidate.population)
    normalization = 1.2 * (0.25 + 0.75 * candidate.length / average)
    return sum(
        log(1 + (candidate.population - df[candidate.corpus, term, TEXT] + 0.5)
            / (df[candidate.corpus, term, TEXT] + 0.5))
        * (frequency * 2.2 / (frequency + normalization))
        for term, (_, frequency) in candidate.matches.items()
    )

def code_selection_key(hit: SearchHit) -> tuple[float, datetime, UUID]:
    return hit.code_weight or 0.0, hit.candidate.created_at, hit.candidate.entry


def order_hits(hits: list[SearchHit], *, code_limit: int | None, authored_date: bool) -> list[SearchHit]:
    """按原始代码分截取候选，再以相关性和展示排序分与人工知识合并。"""
    if code_limit is not None:
        code = nlargest(code_limit, (hit for hit in hits if hit.code_weight is not None), key=code_selection_key)
        hits = [hit for hit in hits if hit.code_weight is None] + code
    hits.sort(key=lambda hit: (
        hit.relevant, hit.rank, hit.candidate.original_date if authored_date else hit.candidate.created_at,
        hit.candidate.created_at, hit.candidate.entry,
    ), reverse=True)
    return diversify_code(hits)


def diversify_code(hits: list[SearchHit]) -> list[SearchHit]:
    """同仓第三条留到相关人工知识之后，跨仓前三条给相关人工知识保留一个位置。"""
    authored = [index for index, hit in enumerate(hits) if hit.relevant and hit.candidate.kind != "code"]
    if not authored:
        return hits
    last_authored = authored[-1]
    repositories: Counter[UUID] = Counter()
    ranked: list[SearchHit] = []
    deferred: list[SearchHit] = []
    for hit in hits[:last_authored + 1]:
        source = hit.candidate.codebase
        if source is not None:
            repositories[source] += 1
        (deferred if source is not None and repositories[source] > 2 else ranked).append(hit)
    ranked.extend(deferred)
    ranked.extend(hits[last_authored + 1:])
    if len(ranked) > 3 and all(hit.candidate.kind == "code" for hit in ranked[:3]):
        third = ranked[2]
        if not third.candidate.named_module:
            first_authored = next(index for index, hit in enumerate(ranked) if hit.candidate.kind != "code")
            ranked.pop(2)
            ranked.insert(first_authored, third)
    return ranked
