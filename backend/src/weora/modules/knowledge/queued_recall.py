"""用本机待发送经验的标识计算当前召回范围，正文始终由设备保管。"""

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.markdown_paths_v1 import entry_path
from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeMarkdownPath, KnowledgeRevision
from weora.modules.knowledge.resource_paths import normalize_resource_path


@dataclass(frozen=True, slots=True)
class QueuedLearning:
    operation_id: UUID
    project_id: UUID | None
    path: str
    matches: bool


@dataclass(frozen=True, slots=True)
class QueueRecall:
    visible: tuple[UUID, ...] = ()
    hidden: frozenset[UUID] = frozenset()
    matches: int = 0


async def resolve_queue(
    db: AsyncSession, published: Select[tuple[KnowledgeEntry, KnowledgeRevision]],
    workspace: UUID, user: UUID, project: UUID | None, directories: tuple[str, ...] | None,
    hints: list[QueuedLearning],
) -> QueueRecall:
    """平台确认队列范围和已提交请求，设备负责渲染未发布的命中。"""
    candidates: list[tuple[str, QueuedLearning]] = []
    for item in hints:
        if item.project_id is not None and item.project_id != project:
            continue
        if normalize_resource_path(item.path) is None:
            continue
        path = entry_path(item.operation_id, "learning", {"resource_path": item.path})
        if path is None:
            continue
        directory = path.split("/", 2)
        if (
            directories is not None and len(directory) == 3
            and directory[0] == "learnings" and directory[1] not in directories
        ):
            continue
        candidates.append((sha256(path.encode()).hexdigest(), item))
    if not candidates:
        return QueueRecall()

    operation_ids = {item.operation_id for _, item in candidates}
    received = await db.execute(
        select(KnowledgeRevision.operation_id, KnowledgeEntry.project_id)
        .join(KnowledgeEntry, KnowledgeEntry.id == KnowledgeRevision.entry_id)
        .where(KnowledgeRevision.workspace_id == workspace, KnowledgeRevision.author_id == user,
               KnowledgeRevision.operation_id.in_(operation_ids))
    )
    delivered = set(received.tuples())
    indexed = KnowledgeMarkdownPath
    alternates = await db.execute(
        published.join(indexed, (indexed.entry_id == KnowledgeEntry.id)
                       & (indexed.revision == KnowledgeEntry.published_revision))
        .where(KnowledgeEntry.kind == "learning", indexed.position == 0,
               indexed.key.in_({key for key, _ in candidates}))
        .with_only_columns(indexed.key, KnowledgeEntry.id, KnowledgeEntry.project_id)
    )
    entries = alternates.tuples().all()
    own_paths = {key for key, _, owner in entries if owner is not None}
    winners: dict[str, QueuedLearning] = {}
    for key, item in sorted(candidates, key=lambda row: row[1].project_id is None):
        if (item.operation_id, item.project_id) in delivered or (item.project_id is None and key in own_paths):
            continue
        winners.setdefault(key, item)
    return QueueRecall(
        tuple(dict.fromkeys(item.operation_id for item in winners.values())),
        frozenset(ident for key, ident, _ in entries if key in winners),
        sum(item.matches for item in winners.values()),
    )
