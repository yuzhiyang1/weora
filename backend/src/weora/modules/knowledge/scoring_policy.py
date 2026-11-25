"""按职能和项目合并评分配置，在领取时绑定决定会话模式的策略版本。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from weora.modules.knowledge.domain import ArchiveAction, ArchiveRunMode, KnowledgeError, ScoringIsolation
from weora.modules.knowledge.models import KnowledgeScoringPolicy

if TYPE_CHECKING:
    from weora.modules.knowledge.store import KnowledgeStore

MODES: tuple[ArchiveRunMode, ...] = ("attended", "unattended")
_DEFAULT_LIMIT = 10
_RESTRICTIVENESS: dict[ArchiveAction, int] = {"off": 0, "ask": 1, "auto": 2}


@dataclass(frozen=True, slots=True)
class EffectiveScoring:
    id: int
    action: ArchiveAction
    daily_limit: int
    batch: bool
    source: str
    conflicts: tuple[str, ...] = ()
    recall_adoption: bool | None = None
    isolation: ScoringIsolation | None = None
    skill_routing: bool | None = None

    def view(self) -> dict[str, object]:
        result: dict[str, object] = {
            "action": self.action, "daily_limit": self.daily_limit, "source": self.source,
        }
        if self.batch:
            result["batch"] = True
        if self.recall_adoption is not None:
            result["recall_adoption"] = self.recall_adoption
        if self.isolation is not None:
            result["isolation"] = self.isolation
        if self.skill_routing is not None:
            result["skill_routing"] = self.skill_routing
        if self.conflicts:
            result["conflict"] = {"field": "batch", "namespaces": list(self.conflicts)}
        return result


@dataclass(frozen=True, slots=True)
class DirectoryScoring:
    sessions: dict[ArchiveRunMode, ArchiveAction] = field(default_factory=dict)
    daily_limit: int | None = None
    batch: bool | None = None
    recall_adoption: bool | None = None
    isolation: ScoringIsolation | None = None
    skill_routing: bool | None = None


async def policy_lock(store: KnowledgeStore, mode: ArchiveRunMode, *, update: bool) -> None:
    if store.db.get_bind().dialect.name == "postgresql":
        key = hashlib.sha256(f"knowledge-scoring-policy:{store.scope.workspace}:{mode}".encode()).digest()[:8]
        lock = func.pg_advisory_xact_lock if update else func.pg_advisory_xact_lock_shared
        await store.db.execute(select(lock(int.from_bytes(key, "big", signed=True))))


async def _latest(
    store: KnowledgeStore, *, mode: ArchiveRunMode | None = None, namespaces: tuple[str, ...] | None = None,
) -> list[KnowledgeScoringPolicy]:
    policy = KnowledgeScoringPolicy
    ranking = select(
        policy.id, func.row_number().over(
            partition_by=(policy.namespace, policy.run_mode), order_by=policy.id.desc(),
        ).label("position"),
    ).where(policy.workspace_id == store.scope.workspace)
    if mode is not None:
        ranking = ranking.where(policy.run_mode == mode)
    if namespaces is not None:
        ranking = ranking.where(policy.namespace.in_(namespaces))
    rows = ranking.subquery()
    return list(await store.db.scalars(select(policy).join(rows, policy.id == rows.c.id).where(
        rows.c.position == 1,
    ).order_by(policy.namespace, policy.run_mode)))


def _active_directories(store: KnowledgeStore, workspace_defaults: bool = False) -> tuple[str, ...] | None:
    if workspace_defaults:
        return ("",)
    folders = store.scope.namespaces
    return None if folders is None else ("", *folders.scorers)


def _adoption_switch(rows: list[KnowledgeScoringPolicy]) -> bool | None:
    overrides = [row.recall_adoption for row in rows if row.namespace and row.recall_adoption is not None]
    return all(overrides) if overrides else None


async def adoption_override(store: KnowledgeStore) -> bool | None:
    # 两种模式共用目录开关，取其中一个即可保留完整的目录快照。
    return _adoption_switch(await _latest(store, mode="attended", namespaces=_active_directories(store)))


def _resolve(rows: list[KnowledgeScoringPolicy], *, workspace_defaults: bool = False) -> EffectiveScoring | None:
    if not rows:
        return None
    root = next((row for row in rows if not row.namespace), None)
    directories = [row for row in rows if row.namespace]
    configured = [row for row in directories if row.batch is not None]
    groups = {row.batch for row in configured}
    if len(groups) > 1:
        return EffectiveScoring(rows[0].id, "off", 0, False, "namespace", tuple(row.namespace for row in configured))
    actions = [(row.action, row) for row in directories if row.action is not None]
    action, decision = min(actions, key=lambda item: _RESTRICTIVENESS[item[0]]) if actions else (
        root.action if root is not None and root.action is not None else "off", root or rows[0],
    )
    caps = [row.daily_limit for row in directories if row.daily_limit is not None]
    limit = min(caps) if caps else root.daily_limit if root and root.daily_limit is not None else _DEFAULT_LIMIT
    batch = bool(configured[0].batch) if configured else bool(root and root.batch)
    adoption = _adoption_switch(directories)
    directory_levels = {row.isolation for row in directories if row.isolation is not None}
    isolation: ScoringIsolation | None
    if directory_levels:
        isolation = "verified" if "verified" in directory_levels else "best-effort"
    else:
        isolation = root.isolation if root else None
    routing_overrides = [row.skill_routing for row in directories if row.skill_routing is not None]
    routing = all(routing_overrides) if routing_overrides else root.skill_routing if root else None
    has_override = bool(actions or caps or configured or directory_levels or routing_overrides) or adoption is not None
    source = "namespace" if has_override else "workspace" if root else "default"
    resolved = action if limit or workspace_defaults else "off"
    return EffectiveScoring(
        decision.id, resolved, limit, batch, source, recall_adoption=adoption, isolation=isolation,
        skill_routing=routing,
    )


async def scoring_policy(store: KnowledgeStore, mode: ArchiveRunMode) -> EffectiveScoring | None:
    rows = await _latest(store, mode=mode, namespaces=_active_directories(store))
    return _resolve(rows)


async def require_compatible_scoring(
    store: KnowledgeStore, mode: ArchiveRunMode, *, settling: bool = False,
) -> EffectiveScoring | None:
    """显式评分可独立于自动评分开关，批处理冲突仍需等待管理员处理。"""
    if settling:
        await policy_lock(store, mode, update=False)
    policy = await scoring_policy(store, mode)
    if policy and policy.conflicts:
        raise KnowledgeError("KNOWLEDGE_SCORING_SETTINGS_CONFLICT", "活跃评分目录的批处理配置冲突。", 409)
    return policy


def rubric_requires_verified(subject: object) -> bool:
    """只沿评分计划的结构检查要求，正文和提示词不参与能力判断。"""
    if isinstance(subject, list):
        return any(rubric_requires_verified(item) for item in subject)
    if not isinstance(subject, dict):
        return False
    return subject.get("isolation") == "verified" or any(
        rubric_requires_verified(subject.get(key))
        for key in (
            "items", "scorer", "rubric", "adoption", "routing",
            "batch", "adoption_batch", "routing_batch", "mixed_batch",
        )
    )


def require_verified(required: bool, reported: ScoringIsolation | None) -> None:
    if required and reported != "verified":
        raise KnowledgeError("KNOWLEDGE_SCORING_ISOLATION", "当前评分要求通过验证的客户端隔离配置。", 409)


def execution_plan(
    result: dict[str, object], policy: EffectiveScoring | None, *, accepts_isolation: bool, manual: bool = False,
) -> dict[str, object]:
    level = "verified" if result.get("isolation") == "verified" else policy.isolation if policy else None
    if not manual and not accepts_isolation:
        require_verified(level == "verified" or rubric_requires_verified(result), None)
    return result | {"isolation": level} if level is not None else result


def require_judge_tools(plan: dict[str, object], tools: set[str], *, local_verified: bool) -> None:
    if not (local_verified or rubric_requires_verified(plan)):
        return
    group = plan.get("mixed_batch") or plan.get("routing_batch") or plan.get("adoption_batch") or plan.get("batch")
    first = group[0] if isinstance(group, list) and group else plan
    rubric = first.get("scorer")
    if not isinstance(rubric, dict):
        generated = first.get("adoption") or first.get("routing")
        rubric = generated.get("rubric") if isinstance(generated, dict) else None
    # 一次合并调用的成员在规划时已校验使用同一评分工具。
    tool = rubric.get("tool") if isinstance(rubric, dict) else None
    if (tool or "codex") not in tools:
        raise KnowledgeError("KNOWLEDGE_SCORING_ISOLATION", "当前设备没有可领取该评分的已核验客户端。", 409)


async def scoring_policies(
    store: KnowledgeStore, *, workspace_defaults: bool = False,
) -> dict[ArchiveRunMode, dict[str, object]]:
    rows = await _latest(store, namespaces=_active_directories(store, workspace_defaults))
    policies: dict[ArchiveRunMode, dict[str, object]] = {}
    for mode in MODES:
        policy = _resolve([row for row in rows if row.run_mode == mode], workspace_defaults=workspace_defaults)
        policies[mode] = policy.view() if policy else {
            "action": "off", "daily_limit": _DEFAULT_LIMIT, "source": "default",
        }
    return policies


def _row(
    store: KnowledgeStore, mode: ArchiveRunMode, namespace: str,
    action: ArchiveAction | None, daily_limit: int | None, batch: bool | None,
    recall_adoption: bool | None = None, isolation: ScoringIsolation | None = None, skill_routing: bool | None = None,
) -> KnowledgeScoringPolicy:
    return KnowledgeScoringPolicy(
        workspace_id=store.scope.workspace, namespace=namespace, run_mode=mode,
        action=action, daily_limit=daily_limit, batch=batch, recall_adoption=recall_adoption, isolation=isolation,
        skill_routing=skill_routing,
        updated_by=store.scope.user, updated_at=datetime.now(UTC),
    )


def _require_workspace_edit(store: KnowledgeStore) -> None:
    store.require_reviewer()
    if store.scope.project is not None:
        raise KnowledgeError("KNOWLEDGE_INVALID_SCOPE", "评分策略在工作空间级别配置。", 422)


async def set_scoring_policy(
    store: KnowledgeStore, mode: ArchiveRunMode, action: ArchiveAction, daily_limit: int | None, batch: bool | None,
    isolation: ScoringIsolation | None = None, skill_routing: bool | None = None,
) -> dict[str, object]:
    _require_workspace_edit(store)
    await policy_lock(store, mode, update=True)
    previous = await _latest(store, mode=mode, namespaces=("",))
    root = previous[0] if previous else None
    limit = daily_limit if daily_limit is not None else root.daily_limit if root else _DEFAULT_LIMIT
    assert limit is not None  # 根目录策略有数据库非空约束。
    grouped = batch if batch is not None else bool(root and root.batch)
    level = isolation if isolation is not None else root.isolation if root else None
    routing = skill_routing if skill_routing is not None else root.skill_routing if root else None
    desired = (action, limit, grouped, level, routing)
    if root is None or (root.action, root.daily_limit, root.batch, root.isolation, root.skill_routing) != desired:
        store.db.add(_row(store, mode, "", action, limit, grouped, isolation=level, skill_routing=routing))
    return EffectiveScoring(0, action, limit, grouped, "workspace", isolation=level, skill_routing=routing).view()


async def _directory(
    store: KnowledgeStore, namespace: str,
) -> tuple[int, DirectoryScoring]:
    rows = await _latest(store, namespaces=(namespace,))
    if not rows:
        return 0, DirectoryScoring()
    ordered = max(rows, key=lambda row: row.id)
    return ordered.id, DirectoryScoring(
        {row.run_mode: row.action for row in rows if row.action is not None},
        ordered.daily_limit, ordered.batch, ordered.recall_adoption, ordered.isolation, ordered.skill_routing,
    )


def _directory_view(namespace: str, revision: int, settings: DirectoryScoring) -> dict[str, object]:
    result: dict[str, object] = {
        "namespace": namespace, "revision": revision, "sessions": settings.sessions,
        "daily_limit": settings.daily_limit, "batch": settings.batch,
    }
    if settings.recall_adoption is not None:
        result["recall_adoption"] = settings.recall_adoption
    if settings.isolation is not None:
        result["isolation"] = settings.isolation
    if settings.skill_routing is not None:
        result["skill_routing"] = settings.skill_routing
    return result


async def directory_scoring(store: KnowledgeStore, namespace: str) -> dict[str, object]:
    store.require_reviewer()
    revision, settings = await _directory(store, namespace)
    return _directory_view(namespace, revision, settings)


async def set_directory_scoring(
    store: KnowledgeStore, namespace: str, base: int, settings: DirectoryScoring,
) -> dict[str, object]:
    _require_workspace_edit(store)
    # 目录文件同时管理两种模式；同一事务保留一对设置快照。
    for mode in MODES:
        await policy_lock(store, mode, update=True)
    await store.lock_session_policy("recall-adoption", update=True)
    revision, previous = await _directory(store, namespace)
    if previous == settings:
        return _directory_view(namespace, revision, previous)
    if revision != base:
        raise KnowledgeError("KNOWLEDGE_SCORING_POLICY_VERSION", "目录评分设置已更新，请刷新后再保存。", 409)
    rows = [
        _row(
            store, mode, namespace, settings.sessions.get(mode), settings.daily_limit, settings.batch,
            settings.recall_adoption, settings.isolation, settings.skill_routing,
        )
        for mode in MODES
    ]
    store.db.add_all(rows)
    await store.db.flush()
    return _directory_view(namespace, max(row.id for row in rows), settings)
