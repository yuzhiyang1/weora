"""汇总团队可见的历史评分，按标准版本和证据口径隔离比较。"""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.sql.selectable import Subquery

from weora.modules.knowledge.models import (
    KnowledgeArchivePolicy,
    KnowledgeSessionLabel,
    KnowledgeSessionOrigin,
    KnowledgeSessionScorer,
    KnowledgeSharedSession,
    KnowledgeSharedSessionRevision,
)
from weora.modules.knowledge.session_origin import start_state
from weora.modules.knowledge.session_scoring import _visible
from weora.modules.knowledge.store import KnowledgeStore

SCORING_WINDOW_DAYS = 30


@dataclass(frozen=True)
class Cohort:
    scorer: str
    scorer_id: UUID
    scorer_revision: int
    source_hash: str | None
    evidence: str
    run_mode: str
    driver: str
    requested_model: str | None


@dataclass(frozen=True)
class Result:
    subject: tuple[UUID, int]
    passed: bool
    scored_at: datetime
    runtime: str
    label: str
    score: float


@dataclass(frozen=True, slots=True)
class MergedResources:
    """发布核验后的两份资源提交和相同长度的对比窗口。"""

    scorer_id: UUID
    repository_id: UUID
    connection_version: int
    base_commit: str
    merge_commit: str
    settled_at: datetime


def score_counts(total: int, passed: int) -> dict[str, int | float | None]:
    return {"total": total, "passed": passed, "failed": total - passed, "pass_rate": passed / total if total else None}


def _monday(value: datetime) -> date:
    return value.date() - timedelta(days=value.weekday())


def weekly_score_counts(
    results: Iterable[tuple[datetime, bool]], since: datetime, until: datetime,
) -> list[dict[str, object]]:
    grouped: dict[date, list[bool]] = defaultdict(list)
    for scored_at, passed in results:
        grouped[_monday(scored_at)].append(passed)
    week, last = _monday(since), _monday(until)
    trend: list[dict[str, object]] = []
    while week <= last:
        samples = grouped[week]
        trend.append({"week": week.isoformat(), **score_counts(len(samples), sum(samples))})
        week += timedelta(days=7)
    return trend


def latest_label_ids(
    store: KnowledgeStore, since: datetime, until: datetime, *, scorer_id: UUID | None = None,
) -> Subquery:
    """同一证据版本、标准版本和评委只使用窗口结束前的最新标签。"""
    scope = store.scope
    label, archive = KnowledgeSessionLabel, KnowledgeSharedSession
    rank = func.row_number().over(
        partition_by=(label.archive_id, label.archive_revision, label.scorer_id, label.scorer_revision,
                      label.judge_id, label.driver),
        order_by=(label.created_at.desc(), label.id.desc()),
    )
    scoped = (archive.workspace_id == scope.workspace, archive.project_id == scope.project)
    statement = select(label.id, rank.label("position")).join(archive, archive.id == label.archive_id).where(
        *scoped, label.created_at < until,
    )
    if scorer_id is not None:
        return statement.where(label.scorer_id == scorer_id, label.created_at >= since).subquery()

    # 趋势比较可读取更早的作者标签；回归候选只使用时间窗内的失败。
    version_key = label.archive_id, label.archive_revision, label.scorer_id, label.scorer_revision
    recent_models = select(*version_key).join(archive, archive.id == label.archive_id).where(
        *scoped, label.driver != "human", label.created_at >= since, label.created_at < until,
    )
    return statement.where(or_(
        label.created_at >= since,
        and_(label.driver == "human", label.judge_id == archive.author_id, tuple_(*version_key).in_(recent_models)),
    )).subquery()


def _dashboard_breakdown(samples: list[Result]) -> dict[str, object]:
    """只汇总当前度量的标签，避免跨版本或人工与模型样本混计。"""
    labels = Counter((sample.label, sample.score) for sample in samples)
    runtimes: dict[str, list[Result]] = defaultdict(list)
    for sample in samples:
        runtimes[sample.runtime].append(sample)
    return {
        "labels": [
            {"value": value, "score": score, "count": count}
            for (value, score), count in sorted(labels.items(), key=lambda item: (-item[0][1], item[0][0]))
        ],
        "by_runtime": [
            {"runtime": runtime, **score_counts(len(items), sum(item.passed for item in items))}
            for runtime, items in sorted(runtimes.items())
        ],
    }


async def scoring_failures(
    store: KnowledgeStore, limit: int, *, days: int = SCORING_WINDOW_DAYS, dashboard: bool = False,
    scorer: str | None = None,
) -> dict[str, object]:
    """近期失败使用最近的可见标签。分组及总数独立于明细条数。"""
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    scope, db = store.scope, store.db
    label, rubric = KnowledgeSessionLabel, KnowledgeSessionScorer
    archive, version, policy = KnowledgeSharedSession, KnowledgeSharedSessionRevision, KnowledgeArchivePolicy
    origin = KnowledgeSessionOrigin
    connections = dict(scope.code_connections)
    latest = latest_label_ids(store, since, now)
    statement = (
        select(
            label.id, label.archive_id, label.archive_revision, archive.author_id, label.judge_id,
            archive.runtime, archive.score_only, label.label, label.driver, label.created_at,
            rubric.id, rubric.name, rubric.revision, rubric.source_hash, rubric.model, rubric.labels,
            rubric.passing_score, func.coalesce(version.local_run_mode, policy.run_mode),
            origin, func.coalesce(version.captured_at, version.published_at),
        )
        .select_from(label)
        .join(latest, and_(latest.c.id == label.id, latest.c.position == 1))
        .join(archive, archive.id == label.archive_id)
        .join(version, and_(version.archive_id == label.archive_id, version.revision == label.archive_revision))
        .join(rubric, and_(rubric.id == label.scorer_id, rubric.revision == label.scorer_revision))
        .outerjoin(policy, and_(policy.id == version.archive_policy_id, policy.workspace_id == scope.workspace))
        .outerjoin(origin, and_(
            origin.workspace_id == archive.workspace_id, origin.project_id == archive.project_id,
            origin.author_id == archive.author_id, origin.subject_key == archive.score_source_key,
        ))
        .where(_visible(store), rubric.is_adoption_template.is_(False))
        .order_by(label.created_at.desc(), label.id.desc())
    )
    if scorer:
        statement = statement.where(rubric.name == scorer)
    rows = await db.stream(statement)
    results: dict[Cohort, list[Result]] = defaultdict(list)
    hand: dict[tuple[UUID, int, UUID, int], bool] = {}
    failures: list[dict[str, object]] = []
    total_failed = 0
    async for row in rows.tuples():
        (
            ident, archive_id, archive_revision, author, judge, runtime, score_only, value, driver, when,
            scorer_id, scorer_name, revision, source_hash, model, labels, threshold, mode, git_start, captured_at,
        ) = row
        score = next((option["score"] for option in labels if option["value"] == value), None)
        if score is None:
            continue
        when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
        cohort = Cohort(
            scorer_name, scorer_id, revision, source_hash, "local" if score_only else "archive",
            mode or "not_recorded", driver, model if driver != "human" else None,
        )
        result = Result((archive_id, archive_revision), score >= threshold, when, runtime, value, score)
        if driver == "human" and judge == author:
            hand[(scorer_id, revision, *result.subject)] = result.passed
        # 较早的会话作者标签可作为比较基准，近期统计仍只计入窗口内的样本。
        if when < since:
            continue
        results[cohort].append(result)
        if not result.passed:
            total_failed += 1
            if len(failures) < limit:
                # 明细只公开标签与可定位的版本。此查询不读取正文、评分提示词或文件。
                failures.append(asdict(cohort) | {
                    "label_id": ident, "archive_id": archive_id, "archive_revision": archive_revision,
                    "runtime": runtime, "label": value, "score": score, "scored_at": when.isoformat(),
                    "git_start": start_state(git_start, connections, captured_at),
                })

    cohorts: list[dict[str, object]] = []
    for cohort, samples in sorted(results.items(), key=lambda item: (
        item[0].scorer, -item[0].scorer_revision, item[0].scorer_id, item[0].evidence, item[0].run_mode, item[0].driver,
    )):
        summary = asdict(cohort) | {
            "stats": score_counts(len(samples), sum(sample.passed for sample in samples)),
            "weeks": weekly_score_counts(((sample.scored_at, sample.passed) for sample in samples), since, now),
        }
        if dashboard:
            summary.update(_dashboard_breakdown(samples))
        if cohort.driver != "human":
            pairs = [
                (sample.passed, verdict) for sample in samples
                if (verdict := hand.get((cohort.scorer_id, cohort.scorer_revision, *sample.subject))) is not None
            ]
            summary["agreement"] = {"paired": len(pairs), "agree": sum(model == manual for model, manual in pairs)}
        cohorts.append(summary)
    return {
        "window": {"days": days, "since": since.isoformat(), "until": now.isoformat()},
        "cohorts": cohorts, "failures": failures,
        "total_failed": total_failed, "truncated": total_failed > len(failures),
    }


async def merged_resource_scores(store: KnowledgeStore, change: MergedResources) -> dict[str, object]:
    """只有明确记录了同一资源仓库、无本地差异和目标提交的样本才进入比较。"""
    merged = change.settled_at.astimezone(UTC) if change.settled_at.tzinfo else change.settled_at.replace(tzinfo=UTC)
    until = min(datetime.now(UTC), merged + timedelta(days=SCORING_WINDOW_DAYS))
    since = merged - (until - merged)
    if until <= merged or change.base_commit == change.merge_commit:
        return {"status": "insufficient_evidence", "cohorts": []}
    scope = store.scope
    label, archive, rubric = KnowledgeSessionLabel, KnowledgeSharedSession, KnowledgeSessionScorer
    version, origin, policy = KnowledgeSharedSessionRevision, KnowledgeSessionOrigin, KnowledgeArchivePolicy
    latest = latest_label_ids(store, since, until, scorer_id=change.scorer_id)
    rows = await store.db.stream(select(
        origin.commit, archive.runtime, archive.score_only, label.label, label.driver, rubric.revision, rubric.model,
        rubric.labels, rubric.passing_score, func.coalesce(version.local_run_mode, policy.run_mode),
    ).select_from(label).join(latest, and_(latest.c.id == label.id, latest.c.position == 1))
    .join(archive, archive.id == label.archive_id)
    .join(version, and_(version.archive_id == label.archive_id, version.revision == label.archive_revision))
    .join(rubric, and_(rubric.id == label.scorer_id, rubric.revision == label.scorer_revision))
    .join(origin, and_(origin.workspace_id == scope.workspace, origin.project_id == scope.project,
                       origin.author_id == archive.author_id, origin.subject_key == archive.score_source_key))
    .outerjoin(policy, and_(policy.id == version.archive_policy_id, policy.workspace_id == scope.workspace))
    .where(
        _visible(store), origin.repository_id == change.repository_id,
        origin.connection_version == change.connection_version, origin.tracked_changes.is_(False),
        or_(
            and_(origin.commit == change.base_commit, origin.observed_at >= since, origin.observed_at < merged),
            and_(origin.commit == change.merge_commit, origin.observed_at >= merged, origin.observed_at < until),
        ),
    ))
    groups: dict[tuple[int, str, str, str, str, str], dict[str, list[bool]]] = defaultdict(
        lambda: {"before": [], "after": []},
    )
    async for commit, runtime, local, label_value, driver, revision, model, labels, passing, mode in rows.tuples():
        score = next((item["score"] for item in labels if item["value"] == label_value), None)
        if score is None:
            continue
        group = groups[revision, runtime, "local" if local else "archive", mode or "not_recorded",
                       driver, (model or "") if driver != "human" else ""]
        group["before" if commit == change.base_commit else "after"].append(score >= passing)
    return {
        "status": "measured" if any(group["before"] and group["after"] for group in groups.values())
        else "insufficient_evidence",
        "cohorts": [
            {"scorer_revision": revision, "runtime": runtime, "evidence": evidence, "run_mode": mode, "driver": driver,
             "requested_model": model or None, "before": score_counts(len(group["before"]), sum(group["before"])),
             "after": score_counts(len(group["after"]), sum(group["after"]))}
            for (revision, runtime, evidence, mode, driver, model), group in sorted(groups.items())
        ],
    }
