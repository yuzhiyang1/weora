"""发放和重试已经固定证据的 Skill 路由评分任务。"""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, and_, exists, func, or_, select, tuple_, update
from sqlalchemy.orm import aliased, defer
from sqlalchemy.sql.elements import ColumnElement

from weora.modules.knowledge.domain import ArchiveRunMode, KnowledgeError, ScoringIsolation
from weora.modules.knowledge.models import (
    KnowledgeAdoptionAttempt,
    KnowledgeArchivePolicy,
    KnowledgeNamedScoreCall,
    KnowledgeRoutingAttempt,
    KnowledgeScoringAttempt,
    KnowledgeSharedSession,
    KnowledgeSharedSessionRevision,
    RoutingQuestion,
    SessionSkillEvidence,
)
from weora.modules.knowledge.routing_rubric import ROUTING_STANDARD, routing_label, routing_question
from weora.modules.knowledge.scoring_policy import EffectiveScoring, require_verified
from weora.modules.knowledge.session_scoring import (
    SCORING_ATTEMPTS,
    SCORING_LEASE,
    SCORING_RETRY_DELAY,
    SCORING_WINDOW,
    JudgeError,
    LocalScoreRef,
    generated_template,
    utc_datetime,
)
from weora.modules.knowledge.store import KnowledgeStore
from weora.modules.knowledge.transcript_archive import transcript_metadata

_MAX_ROUTING_BATCH = 20


def routing_scope(store: KnowledgeStore) -> tuple[ColumnElement[bool], ...]:
    row, scope = KnowledgeRoutingAttempt, store.scope
    return row.workspace_id == scope.workspace, row.project_id == scope.project, row.judge_id == scope.user


def _question_key(question: RoutingQuestion) -> tuple[str, str, str]:
    return question["name"], question["source_hash"], question["routing"]["evidence_hash"]


async def pending_routing_questions(
    store: KnowledgeStore, skills: SessionSkillEvidence | None, versions: Select[tuple[UUID, int]], now: datetime,
    *, suggestions: bool,
) -> list[RoutingQuestion]:
    if skills is None or skills["used"] is None or not skills["available"]:
        return []
    calls = KnowledgeNamedScoreCall
    written = await store.db.scalars(select(calls.items).where(
        calls.workspace_id == store.scope.workspace, calls.project_id == store.scope.project,
        calls.judge_id == store.scope.user, tuple_(calls.archive_id, calls.archive_revision).in_(versions),
    ))
    seen = {
        (str(item.get("scorer")), str(item.get("scorer_source_hash")), str(item.get("evidence_hash")))
        for receipt in written for item in receipt if "skill" in item
    }
    row = KnowledgeRoutingAttempt
    attempts = select(row.questions).where(
        *routing_scope(store), tuple_(row.archive_id, row.archive_revision).in_(versions),
    )
    if suggestions:
        attempts = attempts.where(or_(row.state == "completed", and_(row.state == "open", row.deadline > now)))
    for batch in await store.db.scalars(attempts):
        seen.update(_question_key(question) for question in batch)
    template = await generated_template(store, ROUTING_STANDARD)
    questions = []
    for skill in skills["available"]:
        question = routing_question(skill, skills, template)
        if _question_key(question) not in seen:
            questions.append(question)
            if len(questions) == _MAX_ROUTING_BATCH:
                break
    return questions


def _device_available(mode: ArchiveRunMode, references: list[LocalScoreRef]) -> ColumnElement[bool]:
    archive, version = KnowledgeSharedSession, KnowledgeSharedSessionRevision
    return or_(
        and_(archive.score_only.is_(False), func.coalesce(KnowledgeArchivePolicy.run_mode, "attended") == mode),
        and_(archive.score_only.is_(True), version.local_run_mode == mode,
             tuple_(archive.id, version.revision, version.content_digest).in_(references)),
    )


def _subject_versions(
    store: KnowledgeStore, mode: ArchiveRunMode, references: list[LocalScoreRef], now: datetime,
) -> Select[tuple[KnowledgeSharedSession, KnowledgeSharedSessionRevision]]:
    archive, version = KnowledgeSharedSession, KnowledgeSharedSessionRevision
    return select(archive, version).join(version, version.archive_id == archive.id).outerjoin(
        KnowledgeArchivePolicy, KnowledgeArchivePolicy.id == version.archive_policy_id,
    ).where(
        archive.workspace_id == store.scope.workspace, archive.project_id == store.scope.project,
        archive.author_id == store.scope.user, version.published_at >= now - SCORING_WINDOW,
        _device_available(mode, references),
    )


async def new_routing_questions(
    store: KnowledgeStore, mode: ArchiveRunMode, references: list[LocalScoreRef], now: datetime,
    *, target: tuple[UUID, int] | None = None, preference: tuple[str | None, str | None] | None = None,
) -> tuple[UUID, int, list[RoutingQuestion]] | None:
    archive, version = KnowledgeSharedSession, KnowledgeSharedSessionRevision
    available = _subject_versions(store, mode, references, now).where(
        version.revision == archive.latest_revision, version.skill_evidence.is_not(None),
    )
    if target is not None:
        available = available.where(archive.id == target[0], version.revision == target[1])
    rows = await store.db.stream(available.options(defer(version.turns)).order_by(
        version.published_at.desc(), archive.id,
    ).execution_options(yield_per=30))
    try:
        async for subject, captured in rows.tuples():
            reachable = and_(archive.id == subject.id, version.revision == captured.revision)
            if subject.score_source_key and captured.score_fingerprint:
                reachable = or_(reachable, and_(
                    archive.score_source_key == subject.score_source_key,
                    version.score_fingerprint == captured.score_fingerprint,
                ))
            versions = select(archive.id, version.revision).join(version, version.archive_id == archive.id).where(
                archive.workspace_id == store.scope.workspace, archive.project_id == store.scope.project,
                archive.author_id == store.scope.user, reachable,
            )
            questions = await pending_routing_questions(
                store, captured.skill_evidence, versions, now, suggestions=False,
            )
            if preference is not None:
                questions = [
                    question for question in questions
                    if (question["routing"]["rubric"]["tool"], question["routing"]["rubric"]["model"]) == preference
                ]
            if questions:
                return subject.id, captured.revision, questions
    finally:
        await rows.close()
    return None


def _fail(job: KnowledgeRoutingAttempt, reason: str, now: datetime) -> None:
    job.state, job.reason, job.settled_at = "failed", reason, now


async def routing_view(
    store: KnowledgeStore, job: KnowledgeRoutingAttempt, subject: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "status": "open", "attempt_id": str(job.id), "attempt": job.sequence,
        "deadline": utc_datetime(job.deadline).isoformat(), "routing_batch": job.questions,
        "archive": subject or await transcript_metadata(store, job.archive_id, job.archive_revision),
        **({"isolation": "verified"} if job.requires_verified else {}),
    }


async def replay_routing(
    store: KnowledgeStore, job: KnowledgeRoutingAttempt, mode: ArchiveRunMode, references: list[LocalScoreRef],
    now: datetime, *, accepted: bool, enabled: bool,
) -> dict[str, object]:
    scope = store.scope
    if (job.workspace_id, job.project_id, job.judge_id, job.run_mode) != (
        scope.workspace, scope.project, scope.user, mode,
    ) or not accepted:
        raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_SCOPE", "请求键已用于其他评分尝试。", 409)
    if job.state == "open" and utc_datetime(job.deadline) <= now:
        _fail(job, "lease_expired", utc_datetime(job.deadline))
    if job.state == "open" and not enabled:
        _fail(job, "policy_revoked", now)
    if job.state != "open":
        return {"status": job.state, "attempt_id": str(job.id)}
    subject = await transcript_metadata(store, job.archive_id, job.archive_revision)
    reference = (job.archive_id, job.archive_revision, subject.get("digest"))
    if subject.get("evidence") == "local" and reference not in references:
        return {"status": "evidence_unavailable"}
    return await routing_view(store, job, subject)


def routing_peer_exists(model: type[KnowledgeScoringAttempt] | type[KnowledgeAdoptionAttempt]) -> ColumnElement[bool]:
    row = KnowledgeRoutingAttempt
    return select(model.id).where(
        model.judge_call_id == row.id, model.workspace_id == row.workspace_id,
        model.project_id.is_not_distinct_from(row.project_id), model.judge_id == row.judge_id,
    ).correlate(row).exists()


async def routing_retry(
    store: KnowledgeStore, mode: ArchiveRunMode, references: list[LocalScoreRef], now: datetime,
    *, allow_mixed: bool, allow_adoption: bool,
) -> KnowledgeRoutingAttempt | None:
    row = KnowledgeRoutingAttempt
    await store.db.execute(update(row).where(
        *routing_scope(store), row.run_mode == mode, row.state == "open", row.deadline <= now,
    ).values(state="failed", reason="lease_expired", settled_at=row.deadline))
    newer = aliased(row)
    candidates = _subject_versions(store, mode, references, now).with_only_columns(
        KnowledgeSharedSession.id, KnowledgeSharedSessionRevision.revision,
    ).subquery()
    retry = select(row).join(candidates, and_(
        candidates.c.id == row.archive_id, candidates.c.revision == row.archive_revision,
    )).where(
        *routing_scope(store), row.run_mode == mode, row.state == "failed", row.sequence < SCORING_ATTEMPTS,
        row.settled_at <= now - SCORING_RETRY_DELAY,
        ~exists(select(newer.id).where(newer.series_id == row.series_id, newer.sequence > row.sequence)),
    )
    if not allow_mixed:
        retry = retry.where(~routing_peer_exists(KnowledgeScoringAttempt))
    if not allow_mixed or not allow_adoption:
        retry = retry.where(~routing_peer_exists(KnowledgeAdoptionAttempt))
    return (await store.db.scalars(retry.order_by(row.settled_at, row.id).limit(1))).first()


async def lease_routing(
    store: KnowledgeStore, mode: ArchiveRunMode, operation: UUID, policy: EffectiveScoring, now: datetime,
    target: tuple[UUID, int, list[RoutingQuestion]], *, previous: KnowledgeRoutingAttempt | None = None,
) -> KnowledgeRoutingAttempt:
    scope = store.scope
    if await store.db.get(KnowledgeNamedScoreCall, (scope.user, operation)) is not None:
        raise KnowledgeError("KNOWLEDGE_SCORER_OPERATION", "该请求键已绑定点名评分。", 409)
    archive_id, revision, questions = target
    job = KnowledgeRoutingAttempt(
        id=operation, workspace_id=scope.workspace, project_id=scope.project, judge_id=scope.user,
        archive_id=archive_id, archive_revision=revision, policy_id=policy.id, run_mode=mode,
        series_id=previous.series_id if previous else operation, sequence=previous.sequence + 1 if previous else 1,
        questions=questions, state="open", started_at=now, deadline=now + SCORING_LEASE,
        requires_verified=policy.isolation == "verified" or bool(previous and previous.requires_verified),
    )
    store.db.add(job)
    return job


async def finish_routing(
    store: KnowledgeStore, job: KnowledgeRoutingAttempt, labels: dict[str, str] | None, error: JudgeError | None,
    isolation: ScoringIsolation | None, request_id: str | None, now: datetime, *,
    policy_enabled: bool, scope_verified: bool,
) -> dict[str, object]:
    scope, operation = store.scope, job.id
    if job.state == "completed":
        if job.labels != labels:
            raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_SETTLED", "该调用已使用其他标签结算。", 409)
        receipt = await store.db.get(KnowledgeNamedScoreCall, (scope.user, operation))
        assert receipt is not None
        return {"status": "completed", "judge_call_id": str(operation), "items": receipt.items}
    if job.state == "failed":
        return {"status": "failed", "reason": job.reason}
    reason: str | None
    settled_at = now
    if utc_datetime(job.deadline) <= now:
        reason, settled_at = "lease_expired", utc_datetime(job.deadline)
    elif not policy_enabled:
        reason = "policy_revoked"
    else:
        reason = error
    if reason is not None:
        _fail(job, reason, settled_at)
        return {"status": "failed", "reason": reason}

    if labels is None or labels.keys() != {question["name"] for question in job.questions}:
        raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "请为整组 Skill 路由问题提供标签。", 422)
    verified = (
        job.requires_verified or scope_verified
        or any(question["routing"]["rubric"].get("isolation") == "verified" for question in job.questions)
    )
    require_verified(verified, isolation)
    items = [
        routing_label(question, labels[question["name"]], question["routing"]["rubric"]["tool"] or "codex",
                      operation, isolation)
        for question in job.questions
    ]
    encoded = json.dumps([str(operation), labels], sort_keys=True, separators=(",", ":")).encode()
    receipt = KnowledgeNamedScoreCall(
        judge_id=scope.user, id=operation, workspace_id=scope.workspace, project_id=scope.project,
        archive_id=job.archive_id, archive_revision=job.archive_revision, created_at=now, request_id=request_id,
        request_hash=hashlib.sha256(encoded).hexdigest(), items=items,
    )
    # 同一 ID 同时用于领取和收据，两个入口共用一个额度。
    store.db.add(receipt)
    job.state, job.labels, job.settled_at = "completed", labels, now
    return {"status": "completed", "judge_call_id": str(operation), "items": items}
