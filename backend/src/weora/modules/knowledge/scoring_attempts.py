"""按策略领取归档评分，租约和每日用量由 PostgreSQL 保存。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import and_, false, func, or_, select, tuple_, update
from sqlalchemy.orm import aliased, defer
from sqlalchemy.sql.elements import ColumnElement

from weora.modules.knowledge.domain import ArchiveRunMode, KnowledgeError, ScoringIsolation
from weora.modules.knowledge.models import (
    KnowledgeAdoptionAttempt,
    KnowledgeArchivePolicy,
    KnowledgeNamedScoreCall,
    KnowledgeRoutingAttempt,
    KnowledgeScoringAttempt,
    KnowledgeSessionLabel,
    KnowledgeSessionScorer,
    KnowledgeSharedSession,
    KnowledgeSharedSessionRevision,
    RoutingQuestion,
)
from weora.modules.knowledge.named_scoring import NamedSubject, named_plan, named_subject_evidence
from weora.modules.knowledge.scoring_adoption import (
    AdoptionBatchTarget,
    AdoptionCandidate,
    adoption_call_view,
    adoption_candidates,
    adoption_group,
    adoption_members,
    finish_adoption,
    lease_adoption,
    replay_adoption,
)
from weora.modules.knowledge.scoring_policy import (
    EffectiveScoring,
    execution_plan,
    require_compatible_scoring,
    require_verified,
)
from weora.modules.knowledge.scoring_policy import (
    policy_lock as _policy_lock,
)
from weora.modules.knowledge.scoring_policy import (
    scoring_policy as _policy,
)
from weora.modules.knowledge.scoring_routing import (
    finish_routing,
    lease_routing,
    new_routing_questions,
    pending_routing_questions,
    replay_routing,
    routing_retry,
    routing_scope,
    routing_view,
)
from weora.modules.knowledge.session_scoring import (
    SCORING_ATTEMPTS,
    SCORING_LEASE,
    SCORING_RETRY_DELAY,
    SCORING_WINDOW,
    JudgeError,
    LocalScoreRef,
    SessionJudgment,
    _judgment,
    _rubric,
    list_scorers,
    load_scorer_contents,
    lock_judge,
    rubric_plan,
    utc_datetime,
    write_label,
)
from weora.modules.knowledge.store import KnowledgeStore
from weora.modules.knowledge.transcript_archive import local_score_id, transcript_metadata

type Candidate = tuple[UUID, int, KnowledgeSessionScorer, int]


@dataclass(frozen=True, slots=True)
class NativeOfferSubject:
    runtime: str
    subject_key: str
    digest: str


@dataclass(frozen=True, slots=True)
class ArchivedOfferSubject:
    archive_id: UUID
    revision: int


async def batch_reply(
    store: KnowledgeStore, result: dict[str, object], mode: ArchiveRunMode, *, accepts_isolation: bool, manual: bool,
) -> dict[str, object]:
    """声明团队批量和隔离要求，冲突时只保留人工标签入口。"""
    policy = await _policy(store, mode) if manual else await require_compatible_scoring(store, mode)
    if policy and policy.batch and not manual:
        result |= {"batch": True}
    return execution_plan(result, policy, accepts_isolation=accepts_isolation, manual=manual)


async def require_local_scoring_policy(store: KnowledgeStore, mode: ArchiveRunMode) -> None:
    await _policy_lock(store, mode, update=False)
    policy = await _policy(store, mode)
    if policy is None or policy.action != "auto" or policy.daily_limit == 0:
        raise KnowledgeError("KNOWLEDGE_SCORING_DISABLED", "工作空间当前未允许此运行模式自动评分。", 403)


async def require_offer_policy(store: KnowledgeStore, mode: ArchiveRunMode) -> EffectiveScoring:
    # 与策略更新互斥，避免核验后到提交前撤销授权。
    await _policy_lock(store, mode, update=False)
    policy = await _policy(store, mode)
    if policy is None or policy.action == "off" or policy.daily_limit == 0:
        raise KnowledgeError("KNOWLEDGE_SCORE_OFFER_CHANGED", "工作空间的评分授权已撤销。", 403)
    return policy


def _sampled(name: str, platform: str, key: str, rate: float) -> bool:
    # 同一原生会话在本机与归档入口共享采样桶，平台只持有该会话的哈希。
    subject = json.dumps({"platform": platform, "scorer": name, "sessionId": key}, separators=(",", ":"))
    bucket = int.from_bytes(hashlib.sha256(subject.encode()).digest()[:4], "big") % 10_000
    return bucket < rate * 100


async def scoring_offer(
    store: KnowledgeStore, mode: ArchiveRunMode, evidence: NativeOfferSubject | ArchivedOfferSubject,
    names: list[str], *, named_subject: NamedSubject | None = None, accept_routing: bool = False,
    recommended_routing: bool = False,
) -> dict[str, object]:
    """询问模式只列建议，不建立租约或写入证据。点名标准由成员主动覆盖采样。"""
    policy = await _policy(store, mode)
    if policy is None or policy.action == "off" or policy.daily_limit == 0:
        result: dict[str, object] = {"status": "off", "items": []}
        if policy and policy.conflicts:
            result["conflict"] = policy.view()["conflict"]
        return result
    if recommended_routing and not policy.skill_routing:
        return {"status": "off", "items": []}
    if names and named_subject is not None:
        result = {"status": policy.action, **await named_plan(store, named_subject, names)}
        return result | {"batch": True} if policy.batch else result
    archive, version = KnowledgeSharedSession, KnowledgeSharedSessionRevision
    fingerprint: str | None
    source: str | None
    owned = True
    if isinstance(evidence, NativeOfferSubject):
        runtime, fingerprint, source = evidence.runtime, evidence.digest, evidence.subject_key
        identifier = local_score_id(store.scope, runtime, source)
        exact = and_(archive.id == identifier, version.content_digest == fingerprint)
    else:
        identifier, revision = evidence.archive_id, evidence.revision
        subject = await transcript_metadata(store, identifier, revision)
        if subject.get("evidence") == "local":
            raise KnowledgeError("KNOWLEDGE_SCORE_OFFER_LOCAL", "本机证据须由持有转录的设备预览。", 422)
        owned = subject.get("author_id") == str(store.scope.user)
        exact = and_(archive.id == identifier, version.revision == revision)
        pair = (await store.db.execute(select(
            archive.runtime, archive.score_source_key, version.score_fingerprint,
        ).join(version, version.archive_id == archive.id).where(exact))).tuples().one()
        runtime, source, fingerprint = pair

    if names:
        result = {"status": policy.action, **await rubric_plan(store, names)}
        return result | {"batch": True} if policy.batch else result
    equivalent = exact
    if source and fingerprint:
        equivalent = or_(exact, and_(archive.score_source_key == source, version.score_fingerprint == fingerprint))
    versions = select(version.archive_id, version.revision).join(archive, archive.id == version.archive_id).where(
        archive.workspace_id == store.scope.workspace, archive.project_id == store.scope.project, equivalent,
    )
    label, attempt = KnowledgeSessionLabel, KnowledgeScoringAttempt
    scope = store.scope
    labeled = select(label.scorer_id, label.scorer_revision).where(
        label.judge_id == scope.user, tuple_(label.archive_id, label.archive_revision).in_(versions),
    )
    outstanding = select(attempt.scorer_id, attempt.scorer_revision).where(
        attempt.judge_id == scope.user, tuple_(attempt.archive_id, attempt.archive_revision).in_(versions),
        or_(attempt.state == "completed", and_(attempt.state == "open", attempt.deadline > datetime.now(UTC))),
    )
    seen = set((await store.db.execute(labeled.union(outstanding))).tuples())
    matched = [
        row for row in await list_scorers(store, 100, automatic=True, with_prompt=False)
        if (row.id, row.revision) not in seen
        and _sampled(row.name, runtime, source or str(identifier), row.sampling_rate)
    ][:20]
    await load_scorer_contents(store, matched)
    items = [_rubric(row, with_prompt=True) for row in matched]
    if accept_routing and policy.skill_routing and owned and len(items) < 20:
        if named_subject is not None:
            resolved = await named_subject_evidence(store, named_subject, adoption=False, routing=True)
            skills = resolved.skills
        else:
            skills = await store.db.scalar(select(version.skill_evidence).select_from(archive).join(
                version, version.archive_id == archive.id,
            ).where(exact, archive.author_id == scope.user))
        generated = await pending_routing_questions(store, skills, versions, datetime.now(UTC), suggestions=True)
        items.extend(dict(question) for question in generated[:20 - len(items)])
    result = {"status": policy.action, "items": items}
    return result | {"batch": True} if policy.batch else result


async def _used_today(store: KnowledgeStore, now: datetime) -> int:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    scope, attempt, label, archive = store.scope, KnowledgeScoringAttempt, KnowledgeSessionLabel, KnowledgeSharedSession
    adoption = KnowledgeAdoptionAttempt
    routing = KnowledgeRoutingAttempt
    named_call = KnowledgeNamedScoreCall
    # 普通与采纳记录可共用一次调用，跨表去重后再核算额度。
    launch_calls = select(func.coalesce(attempt.judge_call_id, attempt.id)).where(
        attempt.workspace_id == scope.workspace, attempt.judge_id == scope.user, attempt.started_at >= day,
    ).union(select(func.coalesce(adoption.judge_call_id, adoption.id)).where(
        adoption.workspace_id == scope.workspace, adoption.judge_id == scope.user, adoption.started_at >= day,
    ), select(routing.id).where(
        routing.workspace_id == scope.workspace, routing.judge_id == scope.user, routing.started_at >= day,
    ), select(named_call.id).where(
        named_call.workspace_id == scope.workspace, named_call.judge_id == scope.user, named_call.created_at >= day,
        ~select(routing.id).where(routing.id == named_call.id, routing.judge_id == named_call.judge_id).exists(),
    )).subquery()
    launches = await store.db.scalar(select(func.count()).select_from(launch_calls))
    # 点名和手工评分不受限额阻止，但计入当天已发生的评分。已按领取计费的标签只计一次。
    named_key = func.coalesce(label.judge_call_id, label.operation_id)
    earlier = aliased(KnowledgeSessionLabel)
    # 同组标签可能跨天补交，仍归入首个已写入标签的日期。
    previous_day = select(earlier.id).where(
        earlier.judge_id == label.judge_id, earlier.judge_call_id == label.judge_call_id, earlier.created_at < day,
    ).correlate(label).exists()
    named = await store.db.scalar(select(func.count(func.distinct(named_key)))
        .select_from(label).join(archive, archive.id == label.archive_id)
        .where(archive.workspace_id == scope.workspace, label.judge_id == scope.user, label.created_at >= day,
               ~previous_day, ~select(attempt.id).where(attempt.label_id == label.id).exists(),
               ~select(named_call.id).where(
                   named_call.judge_id == label.judge_id, named_call.id == label.judge_call_id,
               ).exists()))
    return (launches or 0) + (named or 0)


def _failure(row: KnowledgeScoringAttempt | KnowledgeAdoptionAttempt, reason: str, now: datetime) -> None:
    row.state, row.reason, row.settled_at = "failed", reason, now


async def _expire_leases(store: KnowledgeStore, mode: ArchiveRunMode, now: datetime) -> None:
    attempt, scope = KnowledgeScoringAttempt, store.scope
    await store.db.execute(update(attempt).where(
        attempt.workspace_id == scope.workspace, attempt.project_id == scope.project, attempt.judge_id == scope.user,
        attempt.run_mode == mode, attempt.state == "open", attempt.deadline <= now,
    ).values(state="failed", reason="lease_expired", settled_at=attempt.deadline))


def _device_has_evidence(mode: ArchiveRunMode, references: list[LocalScoreRef]) -> ColumnElement[bool]:
    version = KnowledgeSharedSessionRevision
    if not references:
        return false()
    return (version.local_run_mode == mode) & tuple_(
        version.archive_id, version.revision, version.content_digest,
    ).in_(references)


async def _available(
    store: KnowledgeStore, mode: ArchiveRunMode, rubrics: list[KnowledgeSessionScorer], now: datetime,
    references: list[LocalScoreRef], *, batch: bool,
) -> list[Candidate]:
    archive, version, mode_row = KnowledgeSharedSession, KnowledgeSharedSessionRevision, KnowledgeArchivePolicy
    attempt, label = KnowledgeScoringAttempt, KnowledgeSessionLabel
    scorer_by_key = {(row.id, row.revision): row for row in rubrics}
    scorer = aliased(KnowledgeSessionScorer)
    chosen = tuple_(scorer.id, scorer.revision).in_(list(scorer_by_key))
    already_labeled = select(label.id).where(
        label.archive_id == archive.id, label.archive_revision == version.revision,
        label.scorer_id == scorer.id, label.scorer_revision == scorer.revision, label.judge_id == store.scope.user,
    ).correlate(archive, version, scorer).exists()
    already_started = select(attempt.id).where(
        attempt.archive_id == archive.id, attempt.archive_revision == version.revision,
        attempt.scorer_id == scorer.id, attempt.scorer_revision == scorer.revision,
        attempt.judge_id == store.scope.user,
    ).correlate(archive, version, scorer).exists()
    # 归档和本机标识可独立读写，但同一份正文的相同后台判断只领取一次。
    sibling, body = aliased(KnowledgeSharedSession), aliased(KnowledgeSharedSessionRevision)
    sibling_label = select(label.id).where(
        label.archive_id == sibling.id, label.archive_revision == body.revision, label.judge_id == store.scope.user,
        label.scorer_id == scorer.id, label.scorer_revision == scorer.revision,
    ).correlate(sibling, body, scorer).exists()
    sibling_attempt = select(attempt.id).where(
        attempt.archive_id == sibling.id, attempt.archive_revision == body.revision,
        attempt.judge_id == store.scope.user,
        attempt.scorer_id == scorer.id, attempt.scorer_revision == scorer.revision,
    ).correlate(sibling, body, scorer).exists()
    evidence_scored_elsewhere = select(sibling.id).join(body, body.archive_id == sibling.id).where(
        sibling.workspace_id == archive.workspace_id, sibling.project_id.is_not_distinct_from(archive.project_id),
        sibling.author_id == archive.author_id, sibling.score_only != archive.score_only,
        sibling.score_source_key == archive.score_source_key, body.score_fingerprint == version.score_fingerprint,
        or_(sibling_label, sibling_attempt),
    ).correlate(archive, version, scorer).exists()
    shared_mode = and_(archive.score_only.is_(False), func.coalesce(mode_row.run_mode, "attended") == mode)
    available_mode = or_(shared_mode, and_(archive.score_only.is_(True), _device_has_evidence(mode, references)))
    work = (
        select(archive.id, archive.runtime, version.revision, scorer.id, scorer.revision, archive.score_source_key)
        .select_from(archive)
        .join(version, and_(version.archive_id == archive.id, version.revision == archive.latest_revision))
        .outerjoin(mode_row, mode_row.id == version.archive_policy_id)
        .join(scorer, chosen)
        .where(
            archive.workspace_id == store.scope.workspace, archive.project_id == store.scope.project,
            archive.author_id == store.scope.user, version.published_at >= now - SCORING_WINDOW,
            available_mode, ~already_labeled, ~already_started, ~evidence_scored_elsewhere,
        )
        .order_by(version.published_at, archive.id, scorer.name)
    )
    rows = await store.db.stream(work.execution_options(yield_per=100))
    chosen_group: list[Candidate] = []
    try:
        async for identifier, runtime, revision, scorer_id, scorer_revision, source_key in rows.tuples():
            if chosen_group and (identifier, revision) != chosen_group[0][:2]:
                break
            rubric = scorer_by_key[(scorer_id, scorer_revision)]
            if not _sampled(rubric.name, runtime, source_key or str(identifier), rubric.sampling_rate):
                continue
            if chosen_group and (rubric.tool, rubric.model) != (chosen_group[0][2].tool, chosen_group[0][2].model):
                continue
            chosen_group.append((identifier, revision, rubric, 1))
            if not batch or len(chosen_group) == 20:
                break
    finally:
        await rows.close()
    return chosen_group


async def _resume(
    store: KnowledgeStore, mode: ArchiveRunMode, now: datetime, references: list[LocalScoreRef], *, batch: bool,
    accept_mixed: bool, routing_call: UUID | None = None,
) -> tuple[list[Candidate], UUID | None]:
    # 失败续做沿用原归档和原标准，团队更新内容后也不会换掉正在处理的证据。
    attempt, label = KnowledgeScoringAttempt, KnowledgeSessionLabel
    version, scorer = KnowledgeSharedSessionRevision, KnowledgeSessionScorer
    archive = KnowledgeSharedSession
    later = aliased(KnowledgeScoringAttempt)
    superseded = select(later.id).where(
        later.archive_id == attempt.archive_id, later.archive_revision == attempt.archive_revision,
        later.scorer_id == attempt.scorer_id, later.scorer_revision == attempt.scorer_revision,
        later.judge_id == attempt.judge_id, later.sequence > attempt.sequence,
    ).correlate(attempt).exists()
    already_labeled = select(label.id).where(
        label.archive_id == attempt.archive_id, label.archive_revision == attempt.archive_revision,
        label.scorer_id == attempt.scorer_id, label.scorer_revision == attempt.scorer_revision,
        label.judge_id == attempt.judge_id,
    ).correlate(attempt).exists()
    adoption = KnowledgeAdoptionAttempt
    mixed = select(adoption.id).where(
        adoption.workspace_id == attempt.workspace_id, adoption.judge_id == attempt.judge_id,
        adoption.judge_call_id == attempt.judge_call_id,
    ).correlate(attempt).exists()
    query = (
        select(attempt.archive_id, attempt.archive_revision, scorer, attempt.sequence, attempt.judge_call_id, mixed)
        .select_from(attempt)
        .join(scorer, and_(scorer.id == attempt.scorer_id, scorer.revision == attempt.scorer_revision))
        .join(version, and_(version.archive_id == attempt.archive_id, version.revision == attempt.archive_revision))
        .join(archive, archive.id == attempt.archive_id)
        .where(
            attempt.workspace_id == store.scope.workspace, attempt.project_id == store.scope.project,
            attempt.judge_id == store.scope.user, attempt.run_mode == mode, attempt.state == "failed",
            attempt.sequence < SCORING_ATTEMPTS, attempt.settled_at <= now - SCORING_RETRY_DELAY,
            version.published_at >= now - SCORING_WINDOW, ~superseded, ~already_labeled,
            or_(archive.score_only.is_(False), _device_has_evidence(mode, references)),
        )
        .options(defer(scorer.files), defer(scorer.prompt))
        .order_by(attempt.settled_at, attempt.id)
    )
    routing = KnowledgeRoutingAttempt
    belongs_to_routing = select(routing.id).where(
        routing.id == attempt.judge_call_id, routing.workspace_id == attempt.workspace_id,
        routing.project_id.is_not_distinct_from(attempt.project_id), routing.judge_id == attempt.judge_id,
    ).correlate(attempt).exists()
    if routing_call is not None:
        pinned = (await store.db.execute(query.where(attempt.judge_call_id == routing_call).limit(20))).tuples()
        return [(identifier, revision, standard, previous + 1)
                for identifier, revision, standard, previous, _, _ in pinned], routing_call
    query = query.where(~belongs_to_routing)
    if not accept_mixed:
        query = query.where(~mixed)
    row = (await store.db.execute(query.limit(1))).tuples().first()
    if row is None:
        return [], None
    archive_id, revision, rubric, sequence, call_id, mixed_origin = row
    first: Candidate = (archive_id, revision, rubric, sequence + 1)
    if mixed_origin:
        assert call_id is not None
        # 混合调用失败后，普通部分也只从原调用中选仍需要判定的标准。
        pinned = (await store.db.execute(query.where(attempt.judge_call_id == call_id).limit(20))).tuples()
        return [(identifier, version_id, standard, previous + 1)
                for identifier, version_id, standard, previous, _, _ in pinned], call_id
    if not batch:
        return [first], None
    peers = await store.db.execute(query.where(
        attempt.archive_id == archive_id, attempt.archive_revision == revision,
        scorer.tool == rubric.tool, scorer.model == rubric.model, ~mixed,
    ).limit(100))
    # 同一标准可能在不同时间失败，合并时每次只选一个固定版本。
    group: dict[str, Candidate] = {rubric.name: first}
    for identifier, version_id, standard, previous, _, _ in peers.tuples():
        group.setdefault(standard.name, (identifier, version_id, standard, previous + 1))
    return list(group.values())[:20], None


def _scope_matches(store: KnowledgeStore, row: KnowledgeScoringAttempt) -> bool:
    scope = store.scope
    return row.workspace_id == scope.workspace and row.project_id == scope.project and row.judge_id == scope.user


async def _lease_view(
    store: KnowledgeStore, job: KnowledgeScoringAttempt, rubric: KnowledgeSessionScorer,
    *, subject: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "status": "open", "attempt_id": str(job.id), "attempt": job.sequence,
        "deadline": utc_datetime(job.deadline).isoformat(),
        "archive": subject or await transcript_metadata(store, job.archive_id, job.archive_revision),
        "scorer": _rubric(rubric, with_prompt=True),
        **({"isolation": "verified"} if job.requires_verified else {}),
    }


async def _group_rows(store: KnowledgeStore, operation: UUID) -> list[KnowledgeScoringAttempt]:
    attempt, scope = KnowledgeScoringAttempt, store.scope
    return list(await store.db.scalars(select(attempt).where(
        attempt.judge_call_id == operation, attempt.workspace_id == scope.workspace,
        attempt.project_id == scope.project, attempt.judge_id == scope.user,
    ).order_by(attempt.id)))


def _group_view(
    operation: UUID, group: list[tuple[KnowledgeScoringAttempt, KnowledgeSessionScorer]], subject: dict[str, object],
    generated: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    regular = [{"attempt_id": str(row.id), "attempt": row.sequence, "scorer": _rubric(rubric, with_prompt=True)}
               for row, rubric in sorted(group, key=lambda member: member[1].name)]
    return {
        "status": "open", "attempt_id": str(operation), "archive": subject,
        "deadline": utc_datetime(group[0][0].deadline).isoformat(),
        **({"mixed_batch": regular + generated} if generated else {"batch": regular}),
        **({"isolation": "verified"} if any(row.requires_verified for row, _ in group) else {}),
    }


async def _mixed_routing_view(
    store: KnowledgeStore, job: KnowledgeRoutingAttempt, regular: list[KnowledgeScoringAttempt],
    adoption: list[KnowledgeAdoptionAttempt], *, plan: dict[str, object] | None = None,
) -> dict[str, object]:
    plan = plan or await routing_view(store, job)
    if not regular and not adoption:
        return plan
    standards = []
    for row in regular:
        standard = await store.db.get(KnowledgeSessionScorer, (row.scorer_id, row.scorer_revision))
        assert standard is not None
        standards.append((row, standard))
    standard_members = [
        {"attempt_id": str(row.id), "attempt": row.sequence, "scorer": _rubric(standard, with_prompt=True)}
        for row, standard in sorted(standards, key=lambda pair: pair[1].name)
    ]
    routed = [{"attempt_id": question["name"], "attempt": job.sequence, **question} for question in job.questions]
    adopted = await adoption_members(store, adoption) if adoption else []
    del plan["routing_batch"]
    plan["mixed_batch"] = [*standard_members, *adopted, *routed]
    peers: list[KnowledgeScoringAttempt | KnowledgeAdoptionAttempt] = [*regular, *adoption]
    if any(row.requires_verified for row in peers):
        plan["isolation"] = "verified"
    return plan


async def _lease_regular(
    store: KnowledgeStore, mode: ArchiveRunMode, operation: UUID, policy: EffectiveScoring, now: datetime,
    candidates: list[Candidate], group_id: UUID | None,
) -> list[tuple[KnowledgeScoringAttempt, KnowledgeSessionScorer]]:
    await load_scorer_contents(store, [rubric for _, _, rubric, _ in candidates])
    leased = []
    for index, (archive_id, revision, rubric, sequence) in enumerate(candidates):
        row = KnowledgeScoringAttempt(
            id=operation if index == 0 else uuid5(operation, f"{rubric.id}:{rubric.revision}"),
            workspace_id=store.scope.workspace, project_id=store.scope.project, judge_id=store.scope.user,
            archive_id=archive_id, archive_revision=revision, scorer_id=rubric.id, scorer_revision=rubric.revision,
            policy_id=policy.id, run_mode=mode, sequence=sequence, state="open",
            started_at=now, deadline=now + SCORING_LEASE, judge_call_id=group_id,
        )
        store.db.add(row)
        leased.append((row, rubric))
    return leased


async def _lease_routing_call(
    store: KnowledgeStore, mode: ArchiveRunMode, operation: UUID, policy: EffectiveScoring, now: datetime,
    target: tuple[UUID, int, list[RoutingQuestion]], regular: list[Candidate], adoption: list[AdoptionCandidate],
    *, previous: KnowledgeRoutingAttempt | None = None,
) -> dict[str, object]:
    route = await lease_routing(store, mode, operation, policy, now, target, previous=previous)
    leased = await _lease_regular(store, mode, operation, policy, now, regular, operation) if regular else []
    peers = lease_adoption(store, mode, operation, adoption, policy.id, now, judge_call_id=operation, mixed=True)
    return await _mixed_routing_view(store, route, [item for item, _ in leased], peers)


async def pin_verified_claim(store: KnowledgeStore, operation: UUID) -> None:
    """在领取事务内固定最低要求。重放只允许提高隔离级别。"""
    scope = store.scope
    for attempt in (KnowledgeScoringAttempt, KnowledgeAdoptionAttempt):
        await store.db.execute(update(attempt).where(
            attempt.workspace_id == scope.workspace, attempt.project_id == scope.project,
            attempt.judge_id == scope.user, attempt.state == "open", attempt.requires_verified.is_(False),
            or_(attempt.id == operation, attempt.judge_call_id == operation),
        ).values(requires_verified=True))
    route = KnowledgeRoutingAttempt
    await store.db.execute(update(route).where(
        *routing_scope(store), route.state == "open", route.id == operation, route.requires_verified.is_(False),
    ).values(requires_verified=True))


async def claim_scoring(
    store: KnowledgeStore, mode: ArchiveRunMode, operation: UUID, references: list[LocalScoreRef],
    *, accept_batch: bool = False, accept_adoption: bool = False, accept_adoption_template: bool = False,
    accept_adoption_batch: bool = False, accept_mixed_batch: bool = False, accept_routing: bool = False,
    accept_routing_mix: bool = False,
) -> dict[str, object]:
    await _policy_lock(store, mode, update=False)
    policy = await _policy(store, mode)
    if policy and policy.action == "ask" and policy.daily_limit > 0:
        return {"status": "ask"}
    if policy is None or policy.action != "auto" or policy.daily_limit == 0:
        result: dict[str, object] = {"status": "off"}
        if policy and policy.conflicts:
            result["conflict"] = policy.view()["conflict"]
        return result
    await lock_judge(store)
    now = datetime.now(UTC)
    mixed = (
        accept_mixed_batch and accept_batch and accept_adoption and accept_adoption_batch and accept_adoption_template
    )
    await _expire_leases(store, mode, now)
    route_batch = accept_routing and accept_routing_mix and accept_batch
    routing = await store.db.get(KnowledgeRoutingAttempt, operation)
    if routing is not None:
        if (
            routing.workspace_id, routing.project_id, routing.judge_id, routing.run_mode
        ) != (store.scope.workspace, store.scope.project, store.scope.user, mode):
            raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_SCOPE", "请求键已用于其他评分尝试。", 409)
        regular = await _group_rows(store, operation)
        generated = await adoption_group(store, operation)
        if (regular or generated) and (not route_batch or generated and not mixed):
            raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "当前客户端无法领取包含 Skill 路由的混合评分。", 409)
        enabled = bool(policy.skill_routing) and (
            not generated or await store.adoption_enabled(lock=True, automatic=True)
        )
        reply = await replay_routing(store, routing, mode, references, now, accepted=accept_routing, enabled=enabled)
        if reply["status"] == "failed":
            open_peers: list[KnowledgeScoringAttempt | KnowledgeAdoptionAttempt] = [*regular, *generated]
            for peer in open_peers:
                if peer.state == "open":
                    _failure(peer, routing.reason or "policy_revoked", routing.settled_at or now)
        if reply["status"] == "open":
            return await _mixed_routing_view(store, routing, regular, generated, plan=reply)
        return reply
    existing = await store.db.get(KnowledgeScoringAttempt, operation)
    if existing is not None:
        if not _scope_matches(store, existing) or existing.run_mode != mode:
            raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_SCOPE", "请求键已用于其他评分尝试。", 409)
        if existing.state != "open":
            return {"status": existing.state, "attempt_id": str(existing.id)}
        if existing.judge_call_id is not None and (not accept_batch or existing.judge_call_id != operation):
            raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "请使用原分组请求键领取合并评分。", 409)
        subject = await transcript_metadata(store, existing.archive_id, existing.archive_revision)
        if subject.get("evidence") == "local" and (
            existing.archive_id, existing.archive_revision, subject.get("digest"),
        ) not in references:
            return {"status": "evidence_unavailable"}
        if existing.judge_call_id is None:
            rubric = await store.db.get(KnowledgeSessionScorer, (existing.scorer_id, existing.scorer_revision))
            assert rubric is not None
            return await _lease_view(store, existing, rubric, subject=subject)
        generated = await adoption_group(store, operation)
        if generated and not mixed:
            raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "当前客户端无法领取包含采纳问题的混合评分。", 409)
        group = await _group_rows(store, operation)
        if generated and not await store.adoption_enabled(lock=True, automatic=True):
            # 两类成员必须共享同一个调用终态。
            revoked: list[KnowledgeScoringAttempt | KnowledgeAdoptionAttempt] = [*group, *generated]
            for member in revoked:
                _failure(member, "policy_revoked", now)
            return {"status": "failed", "attempt_id": str(operation)}
        replayed = []
        for item in group:
            saved = await store.db.get(KnowledgeSessionScorer, (item.scorer_id, item.scorer_revision))
            assert saved is not None
            replayed.append((item, saved))
        members = await adoption_members(store, generated) if generated else []
        return _group_view(operation, replayed, subject, members)
    adoption = await store.db.get(KnowledgeAdoptionAttempt, operation)
    if adoption is not None:
        return await replay_adoption(
            store, adoption, mode, references, now, accept=accept_adoption, accept_template=accept_adoption_template,
            accept_batch=accept_adoption_batch,
        )
    if await _used_today(store, now) >= policy.daily_limit:
        return {"status": "daily_limit"}
    batch = policy.batch and accept_batch
    routing_enabled = accept_routing and bool(policy.skill_routing)
    if routing_enabled:
        adoption_allowed = route_batch and mixed and await store.adoption_enabled(lock=True, automatic=True)
        prior_route = await routing_retry(
            store, mode, references, now, allow_mixed=route_batch, allow_adoption=adoption_allowed,
        )
        if prior_route is not None:
            pinned_regular, _ = await _resume(
                store, mode, now, references, batch=batch, accept_mixed=mixed, routing_call=prior_route.id,
            )
            generated_retry: list[AdoptionCandidate] = []
            if adoption_allowed:
                route_rubric = prior_route.questions[0]["routing"]["rubric"]
                target = AdoptionBatchTarget(
                    prior_route.archive_id, prior_route.archive_revision, (route_rubric["tool"], route_rubric["model"]),
                    20 - len(pinned_regular) - len(prior_route.questions), prior_route.id,
                )
                generated_retry = await adoption_candidates(
                    store, mode, references, now, accept_template=True, accept_batch=True, accept_mixed=True,
                    target=target,
                )
            return await _lease_routing_call(
                store, mode, operation, policy, now,
                (prior_route.archive_id, prior_route.archive_revision, prior_route.questions),
                pinned_regular, generated_retry, previous=prior_route,
            )
    candidates, previous = await _resume(store, mode, now, references, batch=batch, accept_mixed=mixed)
    if not candidates:
        rubrics = await list_scorers(store, 100, automatic=True, with_prompt=False)
        candidates = await _available(store, mode, rubrics, now, references, batch=batch) if rubrics else []
    if not candidates:
        if accept_adoption:
            choices = await adoption_candidates(
                store, mode, references, now, accept_template=accept_adoption_template,
                accept_batch=accept_adoption_batch, accept_mixed=mixed,
            )
            if choices:
                if routing_enabled and route_batch and mixed and batch and len(choices) < 20 and all(
                    item[5] == 1 for item in choices
                ):
                    identifier, revision, _, _, _, _, adoption_rubric = choices[0]
                    preference = (
                        (adoption_rubric["tool"], adoption_rubric["model"]) if adoption_rubric else (None, None)
                    )
                    match = await new_routing_questions(
                        store, mode, references, now, target=(identifier, revision), preference=preference,
                    )
                    if match:
                        route_target = (match[0], match[1], match[2][:20 - len(choices)])
                        return await _lease_routing_call(store, mode, operation, policy, now, route_target, [], choices)
                return await adoption_call_view(store, mode, operation, choices, policy.id, now)
        if routing_enabled:
            standalone = await new_routing_questions(store, mode, references, now)
            if standalone is not None:
                return await _lease_routing_call(store, mode, operation, policy, now, standalone, [], [])
        return {"status": "empty"}
    generated_choices: list[AdoptionCandidate] = []
    if mixed and (batch or previous is not None) and len(candidates) < 20:
        archive, revision, standard, _ = candidates[0]
        target = AdoptionBatchTarget(archive, revision, (standard.tool, standard.model), 20 - len(candidates), previous)
        generated_choices = await adoption_candidates(
            store, mode, references, now, accept_template=True, accept_batch=True, accept_mixed=True, target=target,
        )
    remaining = 20 - len(candidates) - len(generated_choices)
    if routing_enabled and route_batch and batch and remaining > 0 and all(item[3] == 1 for item in candidates):
        identifier, revision, rubric, _ = candidates[0]
        match = await new_routing_questions(
            store, mode, references, now, target=(identifier, revision), preference=(rubric.tool, rubric.model),
        )
        if match:
            route_target = (match[0], match[1], match[2][:remaining])
            return await _lease_routing_call(store, mode, operation, policy, now, route_target, candidates,
                                             generated_choices)
    group_id = operation if len(candidates) + len(generated_choices) > 1 else None
    leased = await _lease_regular(store, mode, operation, policy, now, candidates, group_id)
    first, standard = leased[0]
    if group_id is None:
        return await _lease_view(store, first, standard)
    subject = await transcript_metadata(store, first.archive_id, first.archive_revision)
    peers = lease_adoption(
        store, mode, operation, generated_choices, policy.id, now, judge_call_id=group_id, mixed=True,
    )
    members = await adoption_members(store, peers) if peers else []
    return _group_view(operation, leased, subject, members)


async def settle_adoption(
    store: KnowledgeStore, operation: UUID, label: str | None, error: JudgeError | None, request_id: str | None,
    *, isolation: ScoringIsolation | None = None,
) -> dict[str, object]:
    job = await store.db.get(KnowledgeAdoptionAttempt, operation)
    scope = store.scope
    if job is None or (job.workspace_id, job.project_id, job.judge_id) != (scope.workspace, scope.project, scope.user):
        raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_NOT_FOUND", "当前范围内找不到本人的采纳评分。", 404)
    if job.judge_call_id is not None:
        raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "请一并提交该次采纳评分的全部标签。", 409)
    await _policy_lock(store, job.run_mode, update=False)
    await lock_judge(store)
    await store.db.refresh(job)
    policy = await _policy(store, job.run_mode)
    return await finish_adoption(
        store, job, label, error, request_id, datetime.now(UTC),
        policy_enabled=policy is not None and policy.action == "auto" and policy.daily_limit > 0,
        scope_verified=policy is not None and policy.isolation == "verified", isolation=isolation,
    )


async def settle_adoption_group(
    store: KnowledgeStore, operation: UUID, verdicts: dict[UUID, str], error: JudgeError | None,
    request_id: str | None, *, isolation: ScoringIsolation | None = None,
) -> dict[str, object]:
    jobs = await adoption_group(store, operation)
    if not jobs:
        raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_NOT_FOUND", "当前范围内找不到本人的合并采纳评分。", 404)
    mode = jobs[0].run_mode
    await _policy_lock(store, mode, update=False)
    await lock_judge(store)
    if await _group_rows(store, operation) or await store.db.scalar(
        select(KnowledgeRoutingAttempt.id).where(*routing_scope(store), KnowledgeRoutingAttempt.id == operation),
    ) is not None:
        raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "请通过混合评分入口一并提交本次调用。", 409)
    if error is None and set(verdicts) != {job.id for job in jobs}:
        raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "合并采纳评分须为每条知识提交唯一标签。", 422)
    policy = await _policy(store, mode)
    allowed = policy is not None and policy.action == "auto" and policy.daily_limit > 0
    now = datetime.now(UTC)
    outcomes = []
    # 按条目顺序加锁，和手动采纳并发时保持相同的锁顺序。任一标签无效则整个事务回滚。
    for job in jobs:
        await store.db.refresh(job)
        result = await finish_adoption(
            store, job, verdicts.get(job.id), error, request_id, now, policy_enabled=allowed,
            scope_verified=policy is not None and policy.isolation == "verified", isolation=isolation,
        )
        outcomes.append({"attempt_id": str(job.id), **result})
    status = "completed" if all(row["status"] == "completed" for row in outcomes) else "failed"
    return {"status": status, "items": outcomes}


async def settle_scoring(
    store: KnowledgeStore, operation: UUID, label: str | None,
    error: JudgeError | None, request_id: str | None, *, isolation: ScoringIsolation | None = None,
) -> dict[str, object]:
    db = store.db
    found = await db.get(KnowledgeScoringAttempt, operation)
    if found is None or not _scope_matches(store, found):
        raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_NOT_FOUND", "当前范围内找不到本人的评分尝试。", 404)
    if found.judge_call_id is not None:
        raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "请一并提交该评分调用的全部标签。", 409)
    await _policy_lock(store, found.run_mode, update=False)
    await lock_judge(store)
    await db.refresh(found)
    policy = await _policy(store, found.run_mode)
    return await _finish_scorer(
        store, found, label, error, request_id, datetime.now(UTC),
        policy_enabled=policy is not None and policy.action == "auto" and policy.daily_limit > 0,
        scope_verified=policy is not None and policy.isolation == "verified", isolation=isolation,
    )


async def _finish_scorer(
    store: KnowledgeStore, found: KnowledgeScoringAttempt, label: str | None, error: JudgeError | None,
    request_id: str | None, now: datetime, *, policy_enabled: bool,
    scope_verified: bool, isolation: ScoringIsolation | None,
) -> dict[str, object]:
    db = store.db
    rubric = await db.get(KnowledgeSessionScorer, (found.scorer_id, found.scorer_revision))
    assert rubric is not None
    if found.state == "completed":
        receipt = await db.get(KnowledgeSessionLabel, found.label_id)
        assert receipt is not None
        if label != receipt.label:
            raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_SETTLED", "评分已结算为另一个标签。", 409)
        result = _judgment(receipt, rubric)
        subject = await transcript_metadata(store, found.archive_id, found.archive_revision)
        if subject.get("evidence") == "local":
            result["evidence"] = "local"
        return {"status": "completed", "label": result}
    if found.state == "open":
        if utc_datetime(found.deadline) <= now:
            _failure(found, "lease_expired", now)
        elif error is not None:
            _failure(found, error, now)
        elif not policy_enabled:
            _failure(found, "policy_revoked", now)
        else:
            assert label is not None
            require_verified(scope_verified or found.requires_verified or rubric.isolation == "verified", isolation)
            result = await write_label(store, found.archive_id, found.archive_revision, SessionJudgment(
                rubric.id, rubric.revision, found.id, label, rubric.tool or "codex", found.judge_call_id, isolation,
            ), request_id)
            label_id = result["id"]
            assert isinstance(label_id, int)
            found.state, found.label_id, found.settled_at = "completed", label_id, now
            return {"status": "completed", "label": result}
    return {"status": "failed", "reason": found.reason}


async def settle_routing(
    store: KnowledgeStore, operation: UUID, labels: dict[str, str] | None, error: JudgeError | None,
    isolation: ScoringIsolation | None, request_id: str | None,
) -> dict[str, object]:
    route = await store.db.scalar(select(KnowledgeRoutingAttempt).where(
        *routing_scope(store), KnowledgeRoutingAttempt.id == operation,
    ))
    if route is None:
        raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_NOT_FOUND", "当前范围内找不到该 Skill 路由任务。", 404)
    await _policy_lock(store, route.run_mode, update=False)
    await lock_judge(store)
    await store.db.refresh(route)
    regular = await _group_rows(store, operation)
    adoption = await adoption_group(store, operation)
    route_keys = {question["name"] for question in route.questions}
    participants: list[KnowledgeScoringAttempt | KnowledgeAdoptionAttempt] = [*regular, *adoption]
    if labels is not None and participants:
        expected = route_keys | {str(row.id) for row in participants}
        if labels.keys() != expected:
            raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "请为整个混合评分调用提供标签。", 422)

    policy = await _policy(store, route.run_mode)
    verified = policy is not None and policy.isolation == "verified"
    allowed = policy is not None and policy.action == "auto" and bool(policy.skill_routing) and policy.daily_limit > 0
    if allowed and adoption:
        allowed = await store.adoption_enabled(lock=True, automatic=True)
    now = datetime.now(UTC)
    verdicts = {key: label for key, label in labels.items() if key in route_keys} if labels is not None else None
    if not regular and not adoption:
        verdicts = labels
    result = await finish_routing(
        store, route, verdicts, error, isolation, request_id, now, policy_enabled=allowed, scope_verified=verified,
    )
    if result["status"] == "failed":
        for peer in participants:
            if peer.state == "open":
                _failure(peer, route.reason or "policy_revoked", route.settled_at or now)
        return result
    if not regular and not adoption:
        return result

    # 调用、普通标签和采纳账本在同一事务结算。任一家族的标签无效时整组回滚。
    assert labels is not None
    members = []
    for entry in regular:
        outcome = await _finish_scorer(
            store, entry, labels[str(entry.id)], None, request_id, now,
            policy_enabled=allowed, scope_verified=verified, isolation=isolation,
        )
        members.append({"attempt_id": str(entry.id), **outcome})
    for question in adoption:
        outcome = await finish_adoption(
            store, question, labels[str(question.id)], None, request_id, now,
            policy_enabled=allowed, scope_verified=verified, isolation=isolation,
        )
        members.append({"attempt_id": str(question.id), **outcome})
    return result | {"members": members}


async def settle_group(
    store: KnowledgeStore, operation: UUID, verdicts: dict[UUID, str], error: JudgeError | None,
    request_id: str | None, *, isolation: ScoringIsolation | None = None,
) -> dict[str, object]:
    group = await _group_rows(store, operation)
    if not group:
        raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_NOT_FOUND", "当前范围内找不到本人的合并评分。", 404)
    mode = group[0].run_mode
    await _policy_lock(store, mode, update=False)
    await lock_judge(store)
    if await store.db.scalar(select(KnowledgeRoutingAttempt.id).where(
        *routing_scope(store), KnowledgeRoutingAttempt.id == operation,
    )) is not None:
        raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "请一并提交 Skill 路由标签。", 409)
    generated = await adoption_group(store, operation)
    expected = {item.id for item in group} | {item.id for item in generated}
    if error is None and set(verdicts) != expected:
        raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "合并评分须为每项标准提交唯一标签。", 422)
    policy = await _policy(store, mode)
    allowed = policy is not None and policy.action == "auto" and policy.daily_limit > 0
    if allowed and generated:
        # 采纳被撤销时，整个混合调用先终止，避免普通标签先提交。
        allowed = await store.adoption_enabled(lock=True, automatic=True)
    now = datetime.now(UTC)
    outcomes = []
    for item in group:
        await store.db.refresh(item)
        outcome = await _finish_scorer(
            store, item, verdicts.get(item.id), error, request_id, now, policy_enabled=allowed,
            scope_verified=policy is not None and policy.isolation == "verified", isolation=isolation,
        )
        outcomes.append({"attempt_id": str(item.id), **outcome})
    for question in generated:
        await store.db.refresh(question)
        outcome = await finish_adoption(
            store, question, verdicts.get(question.id), error, request_id, now, policy_enabled=allowed,
            scope_verified=policy is not None and policy.isolation == "verified", isolation=isolation,
        )
        outcomes.append({"attempt_id": str(question.id), **outcome})
    status = "completed" if all(row["status"] == "completed" for row in outcomes) else "failed"
    return {"status": status, "items": outcomes}
