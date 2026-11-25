"""关联当前项目可读且仍处于发布状态的跨仓库代码证据。"""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import batched
from uuid import UUID, uuid5

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.models import KnowledgeCodebase, KnowledgeEntry, KnowledgeRevision
from weora_code_graph.cross_repository import CodeOrigin, CrossRepositoryEdge, cross_repository_edges


@dataclass(frozen=True, slots=True)
class IndexedPage:
    identity: UUID
    revision: int
    title: str
    blob: str


@dataclass(frozen=True, slots=True)
class PublishedCrossEdge:
    source: IndexedPage
    target: IndexedPage
    evidence: CrossRepositoryEdge

    def view(self, *, inbound: bool) -> dict[str, str]:
        counterpart = self.source if inbound else self.target
        return {
            "kind": "DEPENDS_ON", "direction": "in" if inbound else "out",
            "target": str(counterpart.identity), "title": counterpart.title,
            "derived": "cross_repository", "method": self.evidence.method, "evidence": self.evidence.evidence,
            "source_revision": self.evidence.source.revision, "target_revision": self.evidence.target.revision,
        }


def _namespace(row: KnowledgeCodebase, active: dict[str, str]) -> str:
    if row.repository_id is not None:
        return str(row.repository_id)
    if checkout := row.local_checkout:
        repo = checkout.get("repository_id")
        version = checkout.get("connection_version")
        if repo and version and active.get(repo) == version:
            return repo
    return str(row.id)


async def published_cross_repository_edges(
    db: AsyncSession, scoped: Select[tuple[KnowledgeEntry, KnowledgeRevision]], project: UUID,
    connections: Sequence[tuple[UUID, int]], pages: set[UUID] | None = None,
) -> list[PublishedCrossEdge]:
    path = KnowledgeRevision.source["path"].as_string()
    files = scoped.where(
        KnowledgeEntry.project_id == project, KnowledgeEntry.codebase_id.is_not(None), path != "",
    )
    sources = files.with_only_columns(KnowledgeEntry.codebase_id).distinct()
    # 常见的单仓项目无需传输和扫描代码事实清单。
    if len((await db.scalars(sources.limit(2))).all()) < 2:
        return []
    rows = (
        await db.scalars(select(KnowledgeCodebase).where(
            KnowledgeCodebase.project_id == project, KnowledgeCodebase.id.in_(sources),
        ))
    ).all()
    active = {str(repo): str(version) for repo, version in connections}
    origins = [CodeOrigin(str(row.id), _namespace(row, active), row.revision, row.manifest) for row in rows]
    edges = []
    needed: set[UUID] = set()
    for edge in cross_repository_edges(origins):
        source = uuid5(UUID(edge.source.origin), edge.source.path)
        target = uuid5(UUID(edge.target.origin), edge.target.path)
        if pages is None or source in pages or target in pages:
            edges.append((source, target, edge))
            needed.update((source, target))

    current: dict[UUID, IndexedPage] = {}
    selection = files.with_only_columns(
        KnowledgeEntry.id, KnowledgeRevision.revision, KnowledgeRevision.title,
        KnowledgeRevision.source["blob"].as_string(),
    )
    for group in batched(sorted(needed), 2000):
        for key, revision, title, blob in (await db.execute(selection.where(KnowledgeEntry.id.in_(group)))).tuples():
            if blob:
                current[key] = IndexedPage(key, revision, title, blob)

    # 两端都需要可读的当前发布页，且其对象哈希与生成关系时的证据一致。
    return [
        PublishedCrossEdge(left, right, edge)
        for source, target, edge in edges
        if (left := current.get(source)) and (right := current.get(target))
        and left.blob == edge.source.blob and right.blob == edge.target.blob
    ]
