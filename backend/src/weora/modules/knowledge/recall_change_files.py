"""从最终召回命中的已发布代码证据提取改动入口和直接依赖。"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.cross_repository import PublishedCrossEdge
from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeRevision
from weora.modules.knowledge.recall_code import CodeNode, Published, code_rows
from weora.modules.knowledge.search import SearchHit

_ROOT_LIMIT = 3
_FILE_LIMIT = 10
_EDGE_LIMIT = 10
_DIRECT_RELATIONS = frozenset({"DEPENDS_ON", "REFERENCES", "IMPLEMENTS"})


@dataclass(frozen=True, slots=True)
class SourceGroup:
    """一条命中的源码范围。候选文件来自该范围以外的直接依赖。"""

    page: UUID
    revision: int
    files: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ChangeSources:
    groups: tuple[SourceGroup, ...]
    nodes: dict[UUID, CodeNode]
    rank: dict[UUID, int]

    @property
    def file_ids(self) -> set[UUID]:
        return {identity for group in self.groups for identity in group.files}


async def source_groups(
    db: AsyncSession, published: Published, hits: Sequence[tuple[KnowledgeEntry, KnowledgeRevision]],
    ranked: Sequence[SearchHit],
) -> ChangeSources:
    """从直接命中文件和发布页的源码引用选取入口，略过仓库概览的包含边。"""
    direct = [hit for hit in ranked if hit.candidate.matches or hit.candidate.named_module]
    rank = {hit.candidate.entry: position for position, hit in enumerate(direct)}
    relevant = {hit.candidate.entry for hit in direct if hit.relevant}
    groups = []
    for entry, revision in hits:
        if entry.codebase_id is None:
            continue
        files = (entry.id,) if revision.source.get("path") else tuple(dict.fromkeys(
            UUID(edge["target"]) for edge in revision.relations if edge["kind"] == "REFERENCES"
        ))
        if files:
            groups.append((entry.id, revision.revision, entry.codebase_id, files))
    if not groups:
        return ChangeSources((), {}, rank)
    identities = {page for page, _, _, _ in groups}
    identities.update(identity for _, _, _, files in groups for identity in files)
    nodes = {
        row.id: row for row in (await db.execute(code_rows(published).where(KnowledgeEntry.id.in_(identities)))).all()
    }
    sources = []
    for page, revision_number, codebase, files in groups:
        if page not in nodes or nodes[page].revision != revision_number:
            continue
        visible_files = tuple(
            identity for identity in files if (row := nodes.get(identity)) and row.path and row.codebase_id == codebase
        )
        if not visible_files:
            continue
        # 事实汇总页可能引用整个仓库。优先用查询实际命中的源码范围，避免反向或远端节点混入入口。
        relevant_files = tuple(identity for identity in visible_files if identity in relevant)
        matching_files = tuple(identity for identity in visible_files if identity in rank)
        selected = relevant_files or matching_files or visible_files[:_ROOT_LIMIT]
        sources.append(SourceGroup(page, revision_number, selected))
    return ChangeSources(tuple(sources), nodes, rank)


def _file(row: CodeNode) -> dict[str, str]:
    assert row.path and row.codebase_id is not None
    return {"id": str(row.id), "codebase_id": str(row.codebase_id), "path": row.path}


async def change_evidence(
    db: AsyncSession, published: Published, sources: ChangeSources, cross: Sequence[PublishedCrossEdge],
) -> tuple[dict[str, object], dict[UUID, list[str]]]:
    """只展示从入口发出的直接边。两跳加权只用于排序，不能成为改动依据。"""
    outgoing: dict[UUID, list[tuple[UUID, str, int | None]]] = defaultdict(list)
    file_ids = sources.file_ids
    for identity in file_ids:
        for link in sources.nodes[identity].relations:
            if link["kind"] in _DIRECT_RELATIONS:
                target = UUID(link["target"])
                if target != identity:
                    outgoing[identity].append((target, link["kind"], None))
    for link in sorted(cross, key=lambda edge: (edge.evidence.target.path, edge.target.identity)):
        left, right = link.source.identity, link.target.identity
        if left in file_ids and sources.nodes[left].revision == link.source.revision and left != right:
            outgoing[left].append((right, "DEPENDS_ON", link.target.revision))

    # 在同一数据库快照核对原页、入口和依赖仍处于发布状态。
    identities = file_ids | {group.page for group in sources.groups}
    identities.update(target for edges in outgoing.values() for target, _, _ in edges)
    visible = {
        row.id: row for row in (await db.execute(code_rows(published).where(KnowledgeEntry.id.in_(identities)))).all()
    }
    groups = [
        group for group in sources.groups if group.page in visible and visible[group.page].revision == group.revision
    ]
    origins = {
        start: visible[start] for group in groups for start in group.files
        if start in visible and visible[start].path and visible[start].revision == sources.nodes[start].revision
    }
    # 文件自身的检索排名优先，其余锚点沿用命中页的顺序。
    fallback = max(sources.rank.values(), default=-1) + 1
    ordered = sorted(origins, key=lambda start: sources.rank.get(start, fallback))
    roots = {start: _file(origins[start]) for start in ordered[:_ROOT_LIMIT]}
    candidates: dict[UUID, dict[str, str]] = {}
    edges: dict[tuple[UUID, UUID, str], dict[str, object]] = {}
    shown_by_page: dict[UUID, dict[UUID, None]] = defaultdict(dict)
    for group in groups:
        for start in group.files:
            if (origin := origins.get(start)) is None:
                continue
            for target, kind, revision in outgoing[start]:
                node = visible.get(target)
                if node is None or not node.path:
                    continue
                if revision is None and node.codebase_id != origin.codebase_id:
                    continue
                if revision is not None and node.revision != revision:
                    continue
                if target not in group.files:
                    candidates.setdefault(target, _file(node))
                if start in roots:
                    edges.setdefault((start, target, kind), {"from": roots[start], "to": _file(node), "kind": kind})
                if target not in group.files or start in roots:
                    shown_by_page[group.page][target] = None
    if not roots:
        return {}, {}
    visible_edges = list(edges)[:_EDGE_LIMIT]
    visible_candidates = list(candidates)[:_FILE_LIMIT]
    result: dict[str, object] = {"change_entry_points": {
        "files": list(roots.values()), "edges": [edges[edge] for edge in visible_edges],
        "omitted_edges": max(0, len(edges) - _EDGE_LIMIT),
    }}
    if candidates:
        result["candidate_change_files"] = [candidates[target] for target in visible_candidates]
        result["omitted_candidate_change_files"] = max(0, len(candidates) - _FILE_LIMIT)
    # 只将实际展示的依赖写入召回归因，内部候选和截断部分不计关系使用。
    shown = set(visible_candidates) | {target for _, target, _ in visible_edges}
    return result, {page: [str(target) for target in targets if target in shown]
                    for page, targets in shown_by_page.items()}
