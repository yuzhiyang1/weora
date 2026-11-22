"""工作空间归档策略与自动写入在同一 PostgreSQL 锁下校验。"""

import hashlib
from datetime import UTC, datetime

from sqlalchemy import Select, func, select

from weora.modules.knowledge.domain import ArchiveAction, ArchiveRunMode, KnowledgeError
from weora.modules.knowledge.models import KnowledgeArchivePolicy
from weora.modules.knowledge.store import KnowledgeStore

_DEFAULTS: dict[ArchiveRunMode, ArchiveAction] = {"attended": "ask", "unattended": "off"}


async def _lock(store: KnowledgeStore, mode: ArchiveRunMode, *, publishing: bool) -> None:
    if store.db.get_bind().dialect.name == "postgresql":
        key = hashlib.sha256(f"knowledge-archive:{store.scope.workspace}:{mode}".encode()).digest()[:8]
        lock = func.pg_advisory_xact_lock_shared if publishing else func.pg_advisory_xact_lock
        await store.db.execute(select(lock(int.from_bytes(key, "big", signed=True))))


def _latest(store: KnowledgeStore, mode: ArchiveRunMode) -> Select[tuple[KnowledgeArchivePolicy]]:
    return select(KnowledgeArchivePolicy).where(
        KnowledgeArchivePolicy.workspace_id == store.scope.workspace, KnowledgeArchivePolicy.run_mode == mode,
    ).order_by(KnowledgeArchivePolicy.id.desc()).limit(1)


async def archive_policies(store: KnowledgeStore) -> dict[ArchiveRunMode, dict[str, str]]:
    version = select(func.max(KnowledgeArchivePolicy.id)).where(
        KnowledgeArchivePolicy.workspace_id == store.scope.workspace,
    ).group_by(KnowledgeArchivePolicy.run_mode)
    rows = await store.db.scalars(select(KnowledgeArchivePolicy).where(KnowledgeArchivePolicy.id.in_(version)))
    policies = {mode: {"action": action, "source": "default"} for mode, action in _DEFAULTS.items()}
    for row in rows:
        policies[row.run_mode] = {"action": row.action, "source": "workspace"}
    return policies


async def set_archive_policy(store: KnowledgeStore, mode: ArchiveRunMode, action: ArchiveAction) -> dict[str, str]:
    store.require_reviewer()
    if store.scope.project is not None:
        raise KnowledgeError("KNOWLEDGE_INVALID_SCOPE", "归档策略在工作空间级别配置。", 422)
    await _lock(store, mode, publishing=False)
    latest = await store.db.scalar(_latest(store, mode))
    if latest is None or latest.action != action:
        store.db.add(KnowledgeArchivePolicy(
            workspace_id=store.scope.workspace, run_mode=mode, action=action,
            updated_by=store.scope.user, updated_at=datetime.now(UTC),
        ))
    return {"action": action, "source": "workspace"}


async def automatic_archive_policy(store: KnowledgeStore, mode: ArchiveRunMode) -> int:
    # 禁用策略与在途发布按顺序提交，后台任务重试会重新检查策略。
    await _lock(store, mode, publishing=True)
    row = await store.db.scalar(_latest(store, mode))
    if row is None or row.action != "auto":
        raise KnowledgeError("KNOWLEDGE_ARCHIVE_AUTOMATIC_DISABLED", "工作空间当前未允许此模式自动归档。", 403)
    return row.id
