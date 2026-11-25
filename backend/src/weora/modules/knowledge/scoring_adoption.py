"""从结束会话中生成采纳问题，判定仍写入已有的知识反馈账本。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import and_, false, func, or_, select, tuple_, update
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from weora.modules.knowledge.adoption_rubric import adoption_question, render_adoption_rubric
from weora.modules.knowledge.domain import AdoptionEvidence, ArchiveRunMode, KnowledgeError, ScoringIsolation
from weora.modules.knowledge.models import (
    AdoptionRubric,
    KnowledgeAdoptionAttempt,
    KnowledgeArchivePolicy,
    KnowledgeEntry,
    KnowledgeRecall,
    KnowledgeRevision,
    KnowledgeRoutingAttempt,
    KnowledgeScoringAttempt,
    KnowledgeSharedSession,
    KnowledgeSharedSessionRevision,
)
from weora.modules.knowledge.scoring_policy import require_verified, rubric_requires_verified
from weora.modules.knowledge.session_scoring import (
    SCORING_ATTEMPTS,
    SCORING_LEASE,
    SCORING_RETRY_DELAY,
    SCORING_WINDOW,
    JudgeError,
    LocalScoreRef,
    adoption_template,
    utc_datetime,
)
from weora.modules.knowledge.store import KnowledgeStore, visible
from weora.modules.knowledge.transcript_archive import transcript_metadata

_MAX_BATCH = 20
type AdoptionCandidate = tuple[UUID, int, str, UUID, int | None, int, AdoptionRubric | None]


@dataclass(frozen=True, slots=True)
class AdoptionBatchTarget:
    archive_id: UUID
    revision: int
    preference: tuple[str | None, str | None]
    capacity: int
    previous_call: UUID | None = None


def _candidate_conditions(
    store: KnowledgeStore, mode: ArchiveRunMode, references: list[LocalScoreRef], now: datetime,
) -> tuple[ColumnElement[bool], ...]:
    archive, version, recall = KnowledgeSharedSession, KnowledgeSharedSessionRevision, KnowledgeRecall
    policy = KnowledgeArchivePolicy
    local = (
        (version.local_run_mode == mode)
        & tuple_(version.archive_id, version.revision, version.content_digest).in_(references)
    ) if references else false()
    return (
        archive.workspace_id == store.scope.workspace, archive.project_id == store.scope.project,
        archive.author_id == store.scope.user, version.published_at >= now - SCORING_WINDOW,
        version.captured_at.is_not(None),
        or_((archive.score_only.is_(False)) & (func.coalesce(policy.run_mode, "attended") == mode),
            archive.score_only.is_(True) & local),
        KnowledgeEntry.workspace_id == store.scope.workspace, KnowledgeEntry.project_id == store.scope.project,
        visible(store.scope), KnowledgeEntry.status == "active",
        recall.recalled_revision.is_not(None), recall.recall_count > 0, recall.positive.is_(None),
        store.recall_matches_capture(version.recall_session_key.expression, KnowledgeEntry.id.expression,
                                     recall.recalled_revision.expression, version.captured_at.expression),
    )


async def _adoption_view(
    store: KnowledgeStore, jobs: list[KnowledgeAdoptionAttempt], *, subject: dict[str, object] | None = None,
) -> dict[str, object]:
    first = jobs[0]
    subject = subject or await transcript_metadata(store, first.archive_id, first.archive_revision)
    members = await adoption_members(store, jobs)
    isolation = {"isolation": "verified"} if any(job.requires_verified for job in jobs) else {}
    if first.judge_call_id is None:
        return {
            "status": "open", "attempt_id": str(first.id), "attempt": first.sequence,
            "deadline": utc_datetime(first.deadline).isoformat(), "archive": subject,
            "adoption": members[0]["adoption"], **isolation,
        }
    return {
        "status": "open", "attempt_id": str(first.judge_call_id),
        "deadline": utc_datetime(first.deadline).isoformat(), "archive": subject,
        "adoption_batch": members, **isolation,
    }


async def adoption_members(
    store: KnowledgeStore, jobs: list[KnowledgeAdoptionAttempt],
) -> list[dict[str, object]]:
    entry, version = KnowledgeEntry, KnowledgeRevision
    keys = [(job.entry_id, job.entry_revision) for job in jobs]
    rows = await store.db.execute(
        select(version.entry_id, version.revision, version.title, func.substr(version.body, 1, 1200))
        .join(entry, entry.id == version.entry_id)
        .where(entry.workspace_id == store.scope.workspace, entry.project_id == store.scope.project,
               visible(store.scope), tuple_(version.entry_id, version.revision).in_(keys)),
    )
    documents = {(identifier, revision): (title, excerpt) for identifier, revision, title, excerpt in rows.tuples()}
    if len(documents) != len(keys):
        raise KnowledgeError("KNOWLEDGE_ADOPTION_EVIDENCE_SCOPE", "当前范围内无法读取全部采纳证据。", 404)
    return [
        {"attempt_id": str(job.id), "attempt": job.sequence,
         "adoption": _adoption_item(job, *documents[(job.entry_id, job.entry_revision)])}
        for job in sorted(jobs, key=lambda row: row.entry_id)
    ]


def _adoption_item(job: KnowledgeAdoptionAttempt, title: str, excerpt: str) -> dict[str, object]:
    name, _ = adoption_question(job.entry_id)
    candidate: dict[str, object] = {
        "name": name, "id": str(job.entry_id), "revision": job.entry_revision, "title": title, "excerpt": excerpt,
    }
    if job.rubric is not None:
        candidate["rubric"] = job.rubric
    return candidate


async def adoption_group(store: KnowledgeStore, operation: UUID) -> list[KnowledgeAdoptionAttempt]:
    job, scope = KnowledgeAdoptionAttempt, store.scope
    return list(await store.db.scalars(select(job).where(
        job.judge_call_id == operation, job.workspace_id == scope.workspace,
        job.project_id == scope.project, job.judge_id == scope.user,
    ).order_by(job.entry_id)))


async def replay_adoption(
    store: KnowledgeStore, job: KnowledgeAdoptionAttempt, mode: ArchiveRunMode, references: list[LocalScoreRef],
    now: datetime, *, accept: bool, accept_template: bool, accept_batch: bool,
) -> dict[str, object]:
    if not accept or not _owns(store, job) or job.run_mode != mode:
        raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_SCOPE", "请求键已用于其他评分尝试。", 409)
    if job.judge_call_id is not None and (not accept_batch or job.judge_call_id != job.id):
        raise KnowledgeError("KNOWLEDGE_SCORE_BATCH_REQUEST", "请使用原分组请求键领取合并采纳评分。", 409)
    jobs = await adoption_group(store, job.id) if job.judge_call_id is not None else [job]
    if job.state == "open" and utc_datetime(job.deadline) <= now:
        for row in jobs:
            _fail(row, "lease_expired", now)
    if job.state != "open":
        return {"status": job.state, "attempt_id": str(job.id)}
    if not accept_template and any(row.rubric is not None for row in jobs):
        return {"status": "unsupported_rubric"}
    if not await store.adoption_enabled(lock=True, automatic=True):
        for row in jobs:
            _fail(row, "policy_revoked", now)
        return {"status": "failed", "attempt_id": str(job.id)}
    subject = await transcript_metadata(store, job.archive_id, job.archive_revision)
    if subject.get("evidence") == "local" and (
        job.archive_id, job.archive_revision, subject["digest"],
    ) not in references:
        return {"status": "evidence_unavailable"}
    return await _adoption_view(store, jobs, subject=subject)


async def adoption_call_view(
    store: KnowledgeStore, mode: ArchiveRunMode, operation: UUID, choices: list[AdoptionCandidate],
    policy_id: int, now: datetime,
) -> dict[str, object]:
    leased = lease_adoption(
        store, mode, operation, choices, policy_id, now, judge_call_id=operation if len(choices) > 1 else None,
    )
    return await _adoption_view(store, leased)


async def adoption_candidates(
    store: KnowledgeStore, mode: ArchiveRunMode, references: list[LocalScoreRef], now: datetime,
    *, accept_template: bool, accept_batch: bool, accept_mixed: bool, target: AdoptionBatchTarget | None = None,
) -> list[AdoptionCandidate]:
    if not await store.adoption_enabled(lock=True, automatic=True):
        return []
    jobs, archive = KnowledgeAdoptionAttempt, KnowledgeSharedSession
    version, recall, policy = KnowledgeSharedSessionRevision, KnowledgeRecall, KnowledgeArchivePolicy
    scope = store.scope
    await store.db.execute(update(jobs).where(
        jobs.workspace_id == scope.workspace, jobs.project_id == scope.project, jobs.judge_id == scope.user,
        jobs.run_mode == mode, jobs.state == "open", jobs.deadline <= now,
    ).values(state="failed", reason="lease_expired", settled_at=jobs.deadline))

    # 同一条目和证据版本的失败问题优先续做，保持会话与条目内容固定。
    eligible = (
        select(
            version.archive_id, version.revision, recall.session_key, recall.entry_id, recall.recalled_revision,
        )
        .select_from(version)
        .join(archive, version.archive_id == archive.id)
        .outerjoin(policy, policy.id == version.archive_policy_id)
        .join(recall, and_(recall.session_key == version.recall_session_key, recall.user_id == scope.user))
        .join(KnowledgeEntry, KnowledgeEntry.id == recall.entry_id)
        .where(*_candidate_conditions(store, mode, references, now))
    )
    if target is not None:
        eligible = eligible.where(version.archive_id == target.archive_id, version.revision == target.revision)
    newer = aliased(jobs)
    later = select(newer.id).where(
        newer.judge_id == jobs.judge_id, newer.session_key == jobs.session_key,
        newer.entry_id == jobs.entry_id, newer.entry_revision == jobs.entry_revision, newer.sequence > jobs.sequence,
    ).correlate(jobs).exists()
    retry = eligible.add_columns(jobs.sequence, jobs.rubric, jobs.judge_call_id).join(jobs, and_(
        jobs.archive_id == version.archive_id, jobs.archive_revision == version.revision,
        jobs.session_key == recall.session_key, jobs.entry_id == recall.entry_id,
        jobs.entry_revision == recall.recalled_revision,
    )).where(
        jobs.workspace_id == scope.workspace, jobs.project_id == scope.project, jobs.judge_id == scope.user,
        jobs.run_mode == mode, jobs.state == "failed", jobs.sequence < SCORING_ATTEMPTS,
        jobs.settled_at <= now - SCORING_RETRY_DELAY, ~later,
    ).order_by(jobs.settled_at, jobs.id)
    if not accept_template:
        peer = aliased(jobs)
        requires_template = select(peer.id).where(
            peer.judge_call_id == jobs.judge_call_id, peer.rubric.is_not(None),
        ).correlate(jobs).exists()
        retry = retry.where(jobs.rubric.is_(None), ~requires_template)
    if not accept_batch:
        retry = retry.where(jobs.judge_call_id.is_(None))
    if not accept_mixed:
        regular = KnowledgeScoringAttempt
        mixed = select(regular.id).where(
            regular.workspace_id == jobs.workspace_id, regular.judge_id == jobs.judge_id,
            regular.judge_call_id == jobs.judge_call_id,
        ).correlate(jobs).exists()
        retry = retry.where(~mixed)
    if target is not None and target.previous_call is not None:
        retry = retry.where(jobs.judge_call_id == target.previous_call)
    else:
        routing = KnowledgeRoutingAttempt
        retry = retry.where(~select(routing.id).where(
            routing.id == jobs.judge_call_id, routing.workspace_id == jobs.workspace_id,
            routing.project_id.is_not_distinct_from(jobs.project_id), routing.judge_id == jobs.judge_id,
        ).correlate(jobs).exists())
    resumed = None
    if target is None or target.previous_call is not None:
        resumed = (await store.db.execute(retry.limit(1))).tuples().first()
    chosen: list[AdoptionCandidate] = []
    if resumed is not None:
        previous_call = resumed[-1]
        retries = [resumed] if previous_call is None else list((await store.db.execute(
            retry.where(jobs.judge_call_id == previous_call).limit(_MAX_BATCH),
        )).tuples())
        chosen = [(identifier, revision, key, entry_id, entry_revision, previous + 1, rubric)
                  for identifier, revision, key, entry_id, entry_revision, previous, rubric, _ in retries]
    elif target is None or target.previous_call is None:
        # 本机与共享版本共用同一根会话及条目键，任一来源已尝试时通过原租约续做。
        seen = select(jobs.id).where(
            jobs.workspace_id == scope.workspace, jobs.judge_id == scope.user, jobs.session_key == recall.session_key,
            jobs.entry_id == recall.entry_id, jobs.entry_revision == recall.recalled_revision,
        ).correlate(recall).exists()
        fresh = eligible.where(version.revision == archive.latest_revision, ~seen).order_by(
            version.published_at, archive.id, recall.entry_id,
        )
        row = (await store.db.execute(fresh.limit(1))).tuples().first()
        if row is None:
            return []
        identifier, revision, *_ = row
        template = await adoption_template(store)
        if template is not None and not accept_template:
            return []
        fresh_rows = [row] if not accept_batch else list((await store.db.execute(
            fresh.where(version.archive_id == identifier, version.revision == revision).limit(_MAX_BATCH),
        )).tuples())
        sources: dict[tuple[UUID, int], dict[str, str]] = {}
        if template is not None:
            document = KnowledgeRevision
            source_rows = await store.db.execute(select(document.entry_id, document.revision, document.source).where(
                document.workspace_id == scope.workspace, tuple_(document.entry_id, document.revision).in_([
                    (entry, entry_version) for _, _, _, entry, entry_version in fresh_rows
                ]),
            ))
            sources = {(entry, entry_version): source for entry, entry_version, source in source_rows.tuples()}
        preferred = target.preference if target is not None else None
        for archive_id, archive_revision, key, entry_id, entry_revision in fresh_rows:
            assert entry_revision is not None
            rubric = None
            if template is not None:
                rubric = render_adoption_rubric(template, entry_id, sources[(entry_id, entry_revision)])
            preference = _judge_preference(rubric)
            if preferred is None:
                preferred = preference
            if preferred == preference:
                chosen.append((archive_id, archive_revision, key, entry_id, entry_revision, 1, rubric))
    return chosen if target is None else chosen[:target.capacity]


def lease_adoption(
    store: KnowledgeStore, mode: ArchiveRunMode, operation: UUID, chosen: list[AdoptionCandidate],
    policy_id: int, now: datetime, *, judge_call_id: UUID | None, mixed: bool = False,
) -> list[KnowledgeAdoptionAttempt]:
    scope = store.scope
    leased = []
    for index, (identifier, revision, key, entry_id, entry_revision, sequence, rubric) in enumerate(chosen):
        assert entry_revision is not None
        job = KnowledgeAdoptionAttempt(
            id=operation if index == 0 and not mixed else uuid5(operation, f"adoption:{entry_id}:{entry_revision}"),
            workspace_id=scope.workspace, project_id=scope.project, judge_id=scope.user,
            archive_id=identifier, archive_revision=revision, session_key=key, entry_id=entry_id,
            entry_revision=entry_revision, policy_id=policy_id, run_mode=mode, sequence=sequence, state="open",
            started_at=now, deadline=now + SCORING_LEASE, rubric=rubric, judge_call_id=judge_call_id,
        )
        store.db.add(job)
        leased.append(job)
    return leased


def _judge_preference(rubric: AdoptionRubric | None) -> tuple[str | None, str | None]:
    return (rubric["tool"], rubric["model"]) if rubric is not None else (None, None)


def _owns(store: KnowledgeStore, job: KnowledgeAdoptionAttempt) -> bool:
    scope = store.scope
    return (job.workspace_id, job.project_id, job.judge_id) == (scope.workspace, scope.project, scope.user)


def _fail(job: KnowledgeAdoptionAttempt, reason: str, now: datetime) -> None:
    job.state, job.reason, job.settled_at = "failed", reason, now


async def finish_adoption(
    store: KnowledgeStore, job: KnowledgeAdoptionAttempt, label: str | None, error: JudgeError | None,
    request_id: str | None, now: datetime, *, policy_enabled: bool,
    scope_verified: bool, isolation: ScoringIsolation | None,
) -> dict[str, object]:
    rubric = job.rubric
    adopted = label == "adopted"
    if label is not None:
        if rubric is None and label not in ("adopted", "ignored"):
            raise KnowledgeError("KNOWLEDGE_ADOPTION_LABEL", "采纳评分使用 adopted 或 ignored。", 422)
        if rubric is not None:
            selected = next((row for row in rubric["labels"] if row["value"] == label), None)
            if selected is None:
                raise KnowledgeError("KNOWLEDGE_ADOPTION_LABEL", "标签须来自领取时固定的采纳标准。", 422)
            adopted = selected["score"] >= rubric["passing_score"]
    if job.state == "completed" and (job.scorer_label or job.label) != label:
        raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_SETTLED", "评分已结算为另一个标签。", 409)
    if job.state == "open":
        job.request_id = request_id
        if utc_datetime(job.deadline) <= now:
            _fail(job, "lease_expired", now)
        elif error is not None:
            _fail(job, error, now)
        elif not policy_enabled or not await store.adoption_enabled(lock=True, automatic=True):
            _fail(job, "policy_revoked", now)
        else:
            require_verified(scope_verified or job.requires_verified or rubric_requires_verified(rubric), isolation)
            version = await store.db.get(KnowledgeSharedSessionRevision, (job.archive_id, job.archive_revision))
            archive = await store.db.get(KnowledgeSharedSession, job.archive_id)
            assert version is not None and version.captured_at is not None and archive is not None
            assert label is not None
            evidence = AdoptionEvidence(
                job.archive_id, job.archive_revision, utc_datetime(version.captured_at), job.id, archive.score_only,
                judge_call_id=job.judge_call_id,
            )
            result = await store.judge_adoption_for_key(
                job.session_key, {job.entry_id: (job.entry_revision, adopted)}, evidence, automatic=True,
            )
            job.state, job.settled_at, job.isolation = "completed", now, isolation
            job.label = "adopted" if adopted else "ignored"
            if rubric is not None:
                job.scorer_label = label
            job.applied = bool(result["applied"])
    if job.state == "completed":
        receipt: dict[str, object] = {
            "id": str(job.entry_id), "revision": job.entry_revision, "label": job.label, "applied": job.applied,
        }
        if job.isolation is not None:
            receipt["isolation"] = job.isolation
        if job.judge_call_id is not None:
            receipt["judge_call_id"] = str(job.judge_call_id)
        if rubric is not None:
            receipt["scorer"] = {
                "id": rubric["id"], "revision": rubric["revision"], "source_hash": rubric["source_hash"],
                "label": job.scorer_label, "driver": rubric["tool"] or "codex", "requested_model": rubric["model"],
            }
        return {"status": "completed", "adoption": receipt}
    return {"status": "failed", "reason": job.reason}
