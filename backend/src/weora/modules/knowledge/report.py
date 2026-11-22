"""从当前授权范围读取知识健康与最近发布摘要。"""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.activity import conversation_digest, intervention_digest
from weora.modules.knowledge.cross_repository import published_cross_repository_edges
from weora.modules.knowledge.domain import FeedbackTotals
from weora.modules.knowledge.models import (
    KnowledgeArchivePolicy,
    KnowledgeEntry,
    KnowledgeEvent,
    KnowledgeRecall,
    KnowledgeRevision,
    KnowledgeSharedSession,
    KnowledgeSharedSessionRevision,
)
from weora.modules.knowledge.recall_metrics import feedback_totals, recall_aggregates
from weora.modules.knowledge.registry_quality import registry_quality_catalog, registry_quality_feedback
from weora.modules.knowledge.rule_paths import derived_path_edges
from weora.modules.knowledge.session_outcomes import outcome_digest
from weora.modules.knowledge.session_trends import session_trends
from weora.modules.knowledge.store import (
    Scope,
    lifecycle_date,
    lifecycle_projects,
    lifecycle_published,
    overridden,
    published,
)
from weora.modules.knowledge.workflow_activity import workflow_digest
from weora.modules.skills.infrastructure.distribution import team_usage_digest

_KINDS = ("learning", "doc", "rule", "skill")


@dataclass(frozen=True, slots=True)
class Resource:
    id: UUID
    kind: str
    title: str
    project_id: UUID | None
    revision: int
    authored_at: datetime
    author: UUID
    relations: list[dict[str, str]]
    totals: FeedbackTotals
    last_recalled: datetime | None

    def view(self, now: datetime, *, include_shared: bool) -> dict[str, object]:
        return {
            "id": str(self.id),
            "title": self.title,
            "kind": self.kind,
            **({"project_id": self.project_id} if include_shared else {}),
            "recalls": self.totals.recalls,
            "upvotes": self.totals.upvotes,
            "judged": self.totals.judged,
            "judge_upvotes": self.totals.judge_upvotes,
            "recallers": self.totals.recallers,
            "confidence": self.totals.confidence(now),
        }


async def _resources(db: AsyncSession, scope: Scope, *, include_shared: bool) -> list[Resource]:
    metrics = (
        select(KnowledgeRecall.entry_id, *recall_aggregates())
        .join(KnowledgeEntry, KnowledgeEntry.id == KnowledgeRecall.entry_id)
        .where(KnowledgeEntry.workspace_id == scope.workspace, lifecycle_projects(scope, include_shared))
        .group_by(KnowledgeRecall.entry_id)
        .subquery()
    )
    statement = (
        lifecycle_published(scope, include_shared)
        .outerjoin(metrics, metrics.c.entry_id == KnowledgeEntry.id)
        .where(
            KnowledgeEntry.kind.in_(_KINDS),
            KnowledgeRevision.source["wiki_page"].as_string().is_(None),
        )
        .with_only_columns(
            KnowledgeEntry.id,
            KnowledgeEntry.kind,
            KnowledgeRevision.title,
            KnowledgeEntry.project_id,
            KnowledgeRevision.revision,
            lifecycle_date().label("authored_at"),
            KnowledgeEntry.created_by,
            KnowledgeRevision.relations,
            *(column for column in metrics.c if column.key != "entry_id"),
        )
    )
    resources: list[Resource] = []
    for row in (await db.execute(statement)).mappings():
        resources.append(Resource(
            row["id"], row["kind"], row["title"], row["project_id"], row["revision"], row["authored_at"],
            row["created_by"], row["relations"], feedback_totals(row), row["last_recalled"],
        ))
    return resources


async def _maintenance(
    db: AsyncSession,
    scope: Scope,
    resources: list[Resource],
    now: datetime,
    limit: int, *, include_shared: bool,
) -> dict[str, object]:
    pending, promoted = set[UUID](), set[UUID]()
    rows = await db.execute(
        select(KnowledgeEntry.id, KnowledgeEntry.promoted_from, KnowledgeRevision.status)
        .join(KnowledgeRevision, KnowledgeRevision.entry_id == KnowledgeEntry.id)
        .where(
            KnowledgeEntry.workspace_id == scope.workspace,
            lifecycle_projects(scope, include_shared),
            KnowledgeEntry.status.in_(("pending", "active")),
            or_(KnowledgeRevision.status == "pending", KnowledgeEntry.promoted_from.is_not(None)),
        )
        .distinct()
    )
    for ident, original, status in rows:
        if status == "pending":
            pending.add(ident)
        if original is not None:
            promoted.add(original)
    candidates: dict[str, list[dict[str, object]]] = {"promote": [], "prune": [], "quality": []}
    for row in sorted(resources, key=lambda item: (-item.totals.recalls, item.id)):
        if row.kind == "learning":
            if row.id not in promoted and row.totals.can_promote(row.authored_at, now):
                candidates["promote"].append(row.view(now, include_shared=include_shared))
            if row.id not in pending and (reason := row.totals.prune_reason(row.authored_at, now)):
                candidates["prune"].append(row.view(now, include_shared=include_shared) | {"reason": reason})
        elif row.id not in pending and row.totals.needs_quality_revision():
            candidates["quality"].append(row.view(now, include_shared=include_shared))
    catalog = registry_quality_catalog(
        scope.workspace, scope.project, include_shared=include_shared,
        directories=scope.skill_directories if include_shared else None,
    )
    for skill in (await db.execute(registry_quality_feedback(catalog))).mappings():
        totals = feedback_totals(skill)
        if totals.needs_quality_revision():
            candidates["quality"].append({
                "id": str(skill["id"]), "title": skill["title"], "kind": "skill",
                **({"project_id": skill["project_id"]} if include_shared else {}),
                "skill_version_id": str(skill["version_id"]), "recalls": totals.recalls, "upvotes": totals.upvotes,
                "recallers": totals.recallers, "judged": totals.judged, "judge_upvotes": totals.judge_upvotes,
                "confidence": totals.confidence(now),
            })
    candidates["quality"].sort(key=lambda item: (-int(str(item["recalls"])), str(item["id"])))
    return {key: {"total": len(items), "items": items[:limit]} for key, items in candidates.items()}


async def _graph(db: AsyncSession, scope: Scope, resources: list[Resource]) -> dict[str, int]:
    targets = {str(row.id): str(row.revision) for row in resources}
    ids = set(targets)
    edges = {
        (str(row.id), link["kind"], link["target"])
        for row in resources
        for link in row.relations
        if link.get("kind") and link.get("target") and link.get("derived") != "iwiki"
    }
    wiki = {
        (str(row.id), link["target"], link.get("target_revision"))
        for row in resources for link in row.relations
        if link.get("derived") == "iwiki" and link.get("source_revision") == str(row.revision)
    }
    # 额外核实项目条目引用的共享条目，报告只公开聚合数，不返回不可读的目标。
    references = {UUID(target) for _, _, target in edges if target not in ids}
    references.update(UUID(target) for _, target, _ in wiki if target not in ids)
    scoped = published(scope).where(~overridden(scope))
    if references:
        rows = await db.execute(scoped.where(KnowledgeEntry.id.in_(references)).with_only_columns(
            KnowledgeEntry.id, KnowledgeRevision.revision,
        ))
        targets.update({str(identity): str(revision) for identity, revision in rows.tuples()})
    resolved = {(origin, kind, target) for origin, kind, target in edges if target in targets}
    missing = len(edges - resolved)
    # 自动关系随两端版本失效，不作为人工配置的断链计数。
    resolved.update(
        (origin, "MAPS_TO", target) for origin, target, revision in wiki
        if revision is not None and targets.get(target) == revision
    )
    if scope.project is not None and (rules := {row.id for row in resources if row.kind == "rule"}):
        declared = {(origin, target) for origin, _, target in resolved}
        for edge in await derived_path_edges(db, scoped, scope.project, rules):
            pair = str(edge.rule), str(edge.page)
            if pair not in declared:
                resolved.add((pair[0], "MAPS_TO", pair[1]))
    cross_repository = (
        await published_cross_repository_edges(db, scoped, scope.project, scope.code_connections)
        if scope.project is not None else []
    )
    resolved.update((str(link.source.identity), "DEPENDS_ON", str(link.target.identity)) for link in cross_repository)
    connected = {ident for origin, _, target in resolved for ident in (origin, target)}
    isolated = ids - connected
    return {
        "relations": len(resolved),
        "cross_repository_relations": len(cross_repository),
        "unresolved": missing,
        "without_relations": len(isolated),
        "silent_without_relations": sum(row.totals.recalls == 0 and str(row.id) in isolated for row in resources),
    }


def _percent(covered: int, total: int) -> int:
    return (200 * covered + total) // (2 * total) if total else 0


async def health(
    db: AsyncSession,
    scope: Scope,
    names: dict[UUID, str],
    limit: int, *, include_shared: bool = False,
) -> dict[str, object]:
    now = datetime.now(UTC)
    rows = await _resources(db, scope, include_shared=include_shared)
    seen = sorted((row for row in rows if row.totals.recalls), key=lambda row: (-row.totals.recalls, row.id))
    silent = sorted((row for row in rows if not row.totals.recalls), key=lambda row: row.id)
    counts = Counter(row.kind for row in rows)
    covered = Counter(row.kind for row in seen)
    month = Counter(row.last_recalled.isoformat()[:7] for row in seen if row.last_recalled)
    contributed = Counter(row.author for row in rows)
    author_recalls: Counter[UUID] = Counter()
    for row in rows:
        author_recalls[row.author] += row.totals.recalls
    authors = sorted(contributed, key=lambda ident: (-author_recalls[ident], -contributed[ident], ident))
    hot = sum(row.totals.observations > 0 and row.totals.confidence(now) >= 0.5 for row in rows)
    cold = sum(row.totals.observations > 0 and row.totals.confidence(now) < 0.5 for row in rows)
    return {
        "generated_at": now.isoformat(),
        "summary": {
            "entries": len(rows),
            "recalls": sum(row.totals.recalls for row in rows),
            "upvotes": sum(row.totals.upvotes for row in rows),
            "coverage": _percent(len(seen), len(rows)),
            "contributors": len(authors),
            "hot": hot,
            "cold": cold,
            "silent": len(silent),
        },
        "adoption": {
            "judged": sum(row.totals.judged for row in rows),
            "judge_upvotes": sum(row.totals.judge_upvotes for row in rows),
            "direct_upvotes": sum(row.totals.upvotes - row.totals.judge_upvotes for row in rows),
        },
        "coverage": [
            {
                "kind": kind,
                "total": counts[kind],
                "covered": covered[kind],
                "percent": _percent(covered[kind], counts[kind]),
            }
            for kind in _KINDS
        ],
        "top_recalled": [row.view(now, include_shared=include_shared) for row in seen[:limit]],
        "silent": [row.view(now, include_shared=include_shared) for row in silent[:limit]],
        "last_recall_months": [{"month": key, "entries": month[key]} for key in sorted(month)],
        "authors": [
            {
                "id": str(ident),
                "name": names.get(ident, "历史成员"),
                "entries": contributed[ident],
                "recalls": author_recalls[ident],
            }
            for ident in authors[:limit]
        ],
        "graph": await _graph(db, scope, rows),
        "maintenance": await _maintenance(db, scope, rows, now, limit, include_shared=include_shared),
    }


async def _activity_period(
    db: AsyncSession, scope: Scope, start: datetime, end: datetime,
) -> dict[str, object]:
    """共享会话只在首次发布时计入，避免修订被算作另一次协作。"""
    archive, version, policy = KnowledgeSharedSession, KnowledgeSharedSessionRevision, KnowledgeArchivePolicy
    shared = (
        select(
            archive.id, archive.author_id, archive.runtime, version.title, version.turn_count,
            version.published_at, policy.run_mode,
        )
        .select_from(archive)
        .join(version, version.archive_id == archive.id)
        .outerjoin(policy, (policy.id == version.archive_policy_id) & (policy.workspace_id == scope.workspace))
        .where(
            archive.workspace_id == scope.workspace, archive.project_id == scope.project, archive.score_only.is_(False),
            version.revision == 1, version.published_at >= start, version.published_at < end,
        )
    )
    rows = shared.subquery()
    groups = await db.execute(select(
        rows.c.author_id, rows.c.runtime, rows.c.run_mode, func.count(), func.sum(rows.c.turn_count),
    ).group_by(rows.c.author_id, rows.c.runtime, rows.c.run_mode))
    runtimes: Counter[str] = Counter()
    modes: Counter[str] = Counter({"attended": 0, "unattended": 0, "not_recorded": 0})
    members: set[UUID] = set()
    turns = 0
    for author, runtime, mode, count, turn_count in groups.tuples():
        members.add(author)
        runtimes[runtime] += count
        modes[mode or "not_recorded"] += count
        turns += turn_count
    recent = await db.execute(shared.order_by(version.published_at.desc(), archive.id).limit(5))
    return {
        "since": start.isoformat(), "until": end.isoformat(), "archive_contributors": len(members),
        "shared_sessions": sum(runtimes.values()), "shared_turns": turns, "by_runtime": dict(runtimes),
        "by_run_mode": dict(modes),
        "highlights": [
            {"id": str(identifier), "runtime": runtime, "title": title, "turn_count": count}
            for identifier, _, runtime, title, count, _, _ in recent.tuples()
        ],
        # 团队轮次统计仍限管理员，与 Skill 使用详情权限一致。
        "skill_usage": await team_usage_digest(db, scope.workspace, scope.project, start, end)
        if scope.can_review else None,
    }


async def digest(db: AsyncSession, scope: Scope, *, include_shared: bool = False) -> dict[str, object]:
    """周报读取授权范围的知识动态及两个相邻的七天活动窗口。"""
    now = datetime.now(UTC)
    since = now - timedelta(days=7)
    current = lifecycle_published(scope, include_shared)
    learnings = (
        (
            await db.execute(
                current.where(
                    KnowledgeEntry.kind == "learning",
                )
                .with_only_columns(
                    KnowledgeEntry.id, KnowledgeEntry.project_id, KnowledgeRevision.title, KnowledgeEntry.created_at,
                )
                .order_by(
                    KnowledgeEntry.created_at.desc(),
                    KnowledgeEntry.id,
                )
            )
        )
        .tuples()
        .all()
    )
    first = [
        {"id": str(ident), "title": title, "created_at": at.isoformat(),
         **({"project_id": origin} if include_shared else {})}
        for ident, origin, title, at in learnings
        if (at if at.tzinfo else at.replace(tzinfo=UTC)) >= since
    ]
    releases = await db.execute(
        current.join(KnowledgeEvent, KnowledgeEvent.entry_id == KnowledgeEntry.id)
        .where(
            KnowledgeEntry.kind == "skill",
            KnowledgeEvent.action == "skill_published",
            KnowledgeEvent.occurred_at >= since,
        )
        .with_only_columns(
            KnowledgeEntry.id,
            KnowledgeEntry.project_id,
            KnowledgeRevision.title,
            KnowledgeEvent.revision,
            KnowledgeEvent.occurred_at,
        )
        .order_by(KnowledgeEvent.occurred_at.desc(), KnowledgeEntry.id)
    )
    return {
        "since": since.isoformat(),
        "until": now.isoformat(),
        "total_learnings": len(learnings),
        "interventions": await intervention_digest(db, scope),
        "conversation": await conversation_digest(db, scope),
        "outcomes": await outcome_digest(db, scope),
        "session_trends": await session_trends(db, scope, now),
        "workflows": await workflow_digest(db, scope),
        "new_learnings": first,
        "skill_releases": [
            {"id": str(ident), "title": title, "revision": version, "published_at": at.isoformat(),
             **({"project_id": origin} if include_shared else {})}
            for ident, origin, title, version, at in releases
        ],
        "team_activity": {
            "current": await _activity_period(db, scope, since, now),
            "previous": await _activity_period(db, scope, since - timedelta(days=7), since),
        },
    }
