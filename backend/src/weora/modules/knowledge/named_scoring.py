"""按固定证据解析采纳与 Skill 路由问题，在一个事务中保存一次调用。"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import or_, select, tuple_
from sqlalchemy.orm import defer

from weora.modules.knowledge.adoption_rubric import adoption_question, render_adoption_rubric
from weora.modules.knowledge.domain import AdoptionEvidence, ArchiveRunMode, KnowledgeError, ScoringIsolation
from weora.modules.knowledge.models import (
    KnowledgeEntry,
    KnowledgeNamedScoreCall,
    KnowledgeRecallRun,
    KnowledgeRecallRunHit,
    KnowledgeRevision,
    KnowledgeRoutingAttempt,
    KnowledgeSessionScorer,
    KnowledgeSharedSessionRevision,
    ScorerLabel,
    SessionSkillEvidence,
)
from weora.modules.knowledge.routing_evidence import DeliveryCapture, freeze_deliveries
from weora.modules.knowledge.routing_rubric import (
    ROUTING_NAME_PREFIX,
    ROUTING_STANDARD,
    find_routing_question,
    routing_label,
)
from weora.modules.knowledge.scoring_policy import require_verified
from weora.modules.knowledge.session_scoring import (
    ScoreDriver,
    SessionJudgment,
    adoption_template,
    generated_template,
    lock_judge,
    rubric_plan,
    utc_datetime,
    write_label,
)
from weora.modules.knowledge.store import KnowledgeStore, visible
from weora.modules.knowledge.transcript_archive import local_score_id, register_local_evidence, transcript_metadata

ADOPTION_NAME_PREFIX = "teamai:adoption:"
GENERATED_NAME_PREFIXES = (ADOPTION_NAME_PREFIX, ROUTING_NAME_PREFIX)
_BUILTIN: list[ScorerLabel] = [
    {"value": "adopted", "score": 1.0, "description": "体现了召回知识的具体建议。"},
    {"value": "ignored", "score": 0.0, "description": "没有可确认的采纳证据。"},
]


@dataclass(frozen=True, slots=True)
class NamedArchive:
    archive_id: UUID
    revision: int


@dataclass(frozen=True, slots=True)
class NamedLocal:
    runtime: str
    subject_key: str
    digest: str
    recall_session_key: str
    captured_at: datetime
    skill_capture: DeliveryCapture | None = None


type NamedSubject = NamedArchive | NamedLocal


@dataclass(frozen=True, slots=True)
class NamedLabel:
    name: str
    label: str
    driver: ScoreDriver
    scorer_id: UUID | None
    scorer_revision: int | None
    entry_id: UUID | None
    entry_revision: int | None
    source_hash: str | None
    evidence_hash: str | None = None


@dataclass(frozen=True, slots=True)
class SubjectEvidence:
    recall_key: str | None
    captured_at: datetime | None
    archive: dict[str, object] | None
    skills: SessionSkillEvidence | None


async def named_subject_evidence(
    store: KnowledgeStore, subject: NamedSubject, *, adoption: bool, routing: bool,
) -> SubjectEvidence:
    version = KnowledgeSharedSessionRevision
    if isinstance(subject, NamedArchive):
        meta = await transcript_metadata(store, subject.archive_id, subject.revision)
        if meta["author_id"] != str(store.scope.user) or meta.get("evidence") == "local":
            raise KnowledgeError("KNOWLEDGE_ADOPTION_EVIDENCE_SCOPE", "只能点名本人归档中的生成式问题。", 404)
        key, captured, skills = (await store.db.execute(select(
            version.recall_session_key, version.captured_at, version.skill_evidence,
        ).where(version.archive_id == subject.archive_id, version.revision == subject.revision))).one()
        if adoption and (key is None or captured is None):
            raise KnowledgeError("KNOWLEDGE_ADOPTION_ARCHIVE_TIME", "该归档缺少召回归因，请重新分享。", 422)
        return SubjectEvidence(key, utc_datetime(captured) if captured is not None else None, meta, skills)
    now = datetime.now(UTC)
    if subject.captured_at > now + timedelta(minutes=5):
        raise KnowledgeError("KNOWLEDGE_TRANSCRIPT_CLOCK", "评分采集时间超前，请校准设备后重试。", 422)
    saved = (await store.db.execute(select(
        version.recall_session_key, version.captured_at, version.skill_evidence,
    ).where(version.archive_id == local_score_id(store.scope, subject.runtime, subject.subject_key),
            version.content_digest == subject.digest))).one_or_none()
    if saved and saved.recall_session_key not in (None, subject.recall_session_key):
        raise KnowledgeError("KNOWLEDGE_ADOPTION_EVIDENCE_SCOPE", "同一会话的召回归因键已经固定。", 409)
    captured = utc_datetime(saved.captured_at) if saved and saved.captured_at else min(subject.captured_at, now)
    skills = saved.skill_evidence if saved else None
    if routing and skills is None:
        skills = await freeze_deliveries(store, subject.runtime, captured, subject.skill_capture)
    return SubjectEvidence(subject.recall_session_key, captured, None, skills)


def _question(document: KnowledgeRevision, template: KnowledgeSessionScorer | None) -> dict[str, object]:
    name, _ = adoption_question(document.entry_id)
    rubric = None if template is None else render_adoption_rubric(template, document.entry_id, document.source)
    builtin = hashlib.sha256(f"adoption-v1:{document.entry_id}:{document.revision}".encode()).hexdigest()
    return {
        "name": name, "source_hash": rubric["source_hash"] if rubric else builtin,
        "adoption": {"id": str(document.entry_id), "revision": document.revision,
                     "excerpt": document.body[:1200], "rubric": rubric},
    }


async def _adoption_questions(
    store: KnowledgeStore, questions: set[str], session: str, captured: datetime,
) -> dict[str, dict[str, object]]:
    run, hit, entry = KnowledgeRecallRun, KnowledgeRecallRunHit, KnowledgeEntry
    # 同一条目被召回多次时，选证据采集前实际展示的最近版本，包括已经打开的条目。
    statement = (
        select(hit.entry_id, hit.revision)
        .join(run, run.id == hit.run_id)
        .join(entry, entry.id == hit.entry_id)
        .where(
            run.workspace_id == store.scope.workspace, run.project_id == store.scope.project,
            run.user_id == store.scope.user, run.session_key == session, run.created_at <= captured,
            hit.eligible.is_(True), entry.workspace_id == store.scope.workspace,
            entry.project_id == store.scope.project, entry.status == "active", visible(store.scope),
        )
        .order_by(run.created_at.desc(), run.id, hit.position)
    )
    history = await store.db.stream(statement.execution_options(yield_per=100))
    exact: dict[str, tuple[UUID, int]] = {}
    try:
        async for entry_id, revision in history.tuples():
            name, _ = adoption_question(entry_id)
            if name in questions:
                exact.setdefault(name, (entry_id, revision))
                if len(exact) == len(questions):
                    break
    finally:
        await history.close()
    if questions.difference(exact):
        raise KnowledgeError("KNOWLEDGE_ADOPTION_NOT_RECALLED", "所选采纳问题不属于这份会话证据的召回记录。", 404)
    document = KnowledgeRevision
    versions = await store.db.scalars(select(document).where(
        document.workspace_id == store.scope.workspace,
        tuple_(document.entry_id, document.revision).in_(exact.values()),
    ))
    template = await adoption_template(store)
    rows = {adoption_question(version.entry_id)[0]: _question(version, template) for version in versions}
    if questions.difference(rows):
        raise KnowledgeError("KNOWLEDGE_ADOPTION_NOT_RECALLED", "所选采纳问题的正文版本已不可用。", 404)
    return rows


async def named_plan(
    store: KnowledgeStore, subject: NamedSubject, names: list[str], *, manual: bool = False,
) -> dict[str, object]:
    adoption = {name for name in names if name.startswith(ADOPTION_NAME_PREFIX)}
    routing = [name for name in names if name.startswith(ROUTING_NAME_PREFIX)]
    evidence = await named_subject_evidence(store, subject, adoption=bool(adoption), routing=bool(routing))
    rows: dict[str, dict[str, object]] = {}
    regular = [name for name in names if not name.startswith(GENERATED_NAME_PREFIXES)]
    if regular:
        ordinary = (await rubric_plan(store, regular))["items"]
        assert isinstance(ordinary, list)
        rows.update((row["name"], row) for row in ordinary)
    if adoption:
        assert evidence.recall_key is not None and evidence.captured_at is not None
        rows.update(await _adoption_questions(store, adoption, evidence.recall_key, evidence.captured_at))
    if routing:
        template = await generated_template(store, ROUTING_STANDARD)
        for name in routing:
            rows[name] = dict(find_routing_question(name, evidence.skills, template, manual=manual))
    return {"items": [rows[name] for name in names], **({"archive": evidence.archive} if evidence.archive else {})}


async def _standard(
    store: KnowledgeStore, label: NamedLabel, *, adoption: bool, routing: bool,
) -> KnowledgeSessionScorer | None:
    if label.scorer_id is None:
        return None
    scorer = KnowledgeSessionScorer
    statement = select(scorer).where(
        scorer.workspace_id == store.scope.workspace,
        or_(scorer.project_id.is_(None), scorer.project_id == store.scope.project),
        scorer.id == label.scorer_id, scorer.revision == label.scorer_revision,
        scorer.is_adoption_template.is_(adoption), scorer.adoption_template_archived.is_(False),
        scorer.name == ROUTING_STANDARD if routing else scorer.name != ROUTING_STANDARD,
    ).options(defer(scorer.files))
    if routing and store.scope.namespaces is not None:
        statement = statement.where(scorer.namespace.not_in(store.scope.namespaces.inactive_scorers))
    found = await store.db.scalar(statement)
    if found is None:
        raise KnowledgeError("KNOWLEDGE_SCORER_NOT_FOUND", "当前范围内找不到所选评分标准版本。", 404)
    return found


async def _pinned_question(
    store: KnowledgeStore, label: NamedLabel, standard: KnowledgeSessionScorer | None, session: str, captured: datetime,
) -> tuple[list[ScorerLabel], float]:
    entry, version = KnowledgeEntry, KnowledgeRevision
    assert label.entry_id is not None and label.entry_revision is not None
    document = await store.db.scalar(select(version).join(entry, version.entry_id == entry.id).where(
        entry.workspace_id == store.scope.workspace, entry.project_id == store.scope.project,
        entry.status == "active", visible(store.scope),
        version.entry_id == label.entry_id, version.revision == label.entry_revision,
        store.recall_matches_capture(session, label.entry_id, label.entry_revision, captured),
    ))
    if document is None or adoption_question(label.entry_id)[0] != label.name:
        raise KnowledgeError("KNOWLEDGE_ADOPTION_NOT_RECALLED", "所选采纳问题不属于这份会话证据的召回记录。", 404)
    question = _question(document, standard)
    if label.source_hash != question["source_hash"]:
        raise KnowledgeError("KNOWLEDGE_ADOPTION_TEMPLATE_CHANGED", "采纳评分标准与计划不一致。", 409)
    rubric = None if standard is None else render_adoption_rubric(standard, label.entry_id, document.source)
    return (_BUILTIN, 1.0) if rubric is None else (rubric["labels"], rubric["passing_score"])


def _routing_outcome(
    item: NamedLabel, template: KnowledgeSessionScorer | None, skills: SessionSkillEvidence | None, operation: UUID,
    isolation: ScoringIsolation | None,
) -> dict[str, object]:
    question = find_routing_question(item.name, skills, template, manual=item.driver == "human")
    routing = question["routing"]
    if item.source_hash != question["source_hash"] or item.evidence_hash != routing["evidence_hash"]:
        raise KnowledgeError("KNOWLEDGE_ROUTING_EVIDENCE_CHANGED", "路由标准或会话 Skill 证据已变化，请重新预览。", 409)
    return routing_label(question, item.label, item.driver, operation, isolation)


def _receipt(row: KnowledgeNamedScoreCall) -> dict[str, object]:
    return {"judge_call_id": str(row.id), "subject_id": str(row.archive_id),
            "subject_revision": row.archive_revision, "items": row.items}


async def read_named_call(store: KnowledgeStore, operation: UUID) -> dict[str, object]:
    scope = store.scope
    row = await store.db.get(KnowledgeNamedScoreCall, (scope.user, operation))
    if row is None or (row.workspace_id, row.project_id) != (scope.workspace, scope.project):
        raise KnowledgeError("KNOWLEDGE_SCORE_ATTEMPT_NOT_FOUND", "当前范围内找不到该点名评分调用。", 404)
    return _receipt(row)


async def settle_named(
    store: KnowledgeStore, subject: NamedSubject, operation: UUID, labels: list[NamedLabel], request_id: str | None,
    *, offer_mode: ArchiveRunMode | None = None, isolation: ScoringIsolation | None = None,
) -> dict[str, object]:
    await lock_judge(store)
    scope = store.scope
    subject_identity = asdict(subject)
    if isinstance(subject, NamedLocal):
        # 相同转录重新采集时的时间戳会变化，收据仍绑定首次确认的证据版本。
        subject_identity.pop("captured_at")
        subject_identity.pop("skill_capture")
        if subject.skill_capture is not None:
            subject_identity["skill_capture"] = subject.skill_capture.fingerprint()
    label_identities = []
    for item in labels:
        fields = asdict(item)
        if item.evidence_hash is None:
            fields.pop("evidence_hash")
        label_identities.append(fields)
    identity: list[object] = [type(subject).__name__, subject_identity, label_identities]
    if offer_mode is not None:
        identity.append(offer_mode)
    if isolation is not None:
        identity.append({"isolation": isolation})
    request_hash = hashlib.sha256(json.dumps(
        identity,
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    saved = await store.db.get(KnowledgeNamedScoreCall, (scope.user, operation))
    if saved is not None:
        if (saved.workspace_id, saved.project_id, saved.request_hash) != (scope.workspace, scope.project, request_hash):
            raise KnowledgeError("KNOWLEDGE_SCORER_OPERATION", "请求键已用于另一份点名评分。", 409)
        return _receipt(saved)
    lease = await store.db.get(KnowledgeRoutingAttempt, operation)
    if lease is not None and lease.judge_id == scope.user:
        raise KnowledgeError("KNOWLEDGE_SCORER_OPERATION", "该请求键已绑定后台路由评分。", 409)
    evidence = await named_subject_evidence(
        store, subject, adoption=any(item.entry_id is not None for item in labels),
        routing=any(item.name.startswith(ROUTING_NAME_PREFIX) for item in labels),
    )
    preference: set[tuple[str | None, str | None, str]] = set()
    adoption: dict[UUID, tuple[int, bool]] = {}
    outcomes: dict[str, dict[str, object]] = {}
    ordinary = []
    for item in labels:
        is_adoption = item.entry_id is not None
        is_routing = item.name.startswith(ROUTING_NAME_PREFIX)
        standard = await _standard(store, item, adoption=is_adoption, routing=is_routing)
        if item.driver != "human":
            require_verified(standard is not None and standard.isolation == "verified", isolation)
        preference.add((standard.tool if standard else None, standard.model if standard else None, item.driver))
        if is_routing:
            outcomes[item.name] = _routing_outcome(item, standard, evidence.skills, operation, isolation)
            continue
        if not is_adoption:
            if standard is None or standard.name != item.name:
                raise KnowledgeError("KNOWLEDGE_SCORER_NOT_FOUND", "找不到所选评分标准。", 404)
            ordinary.append((item, standard))
            continue
        assert item.entry_id is not None and item.entry_revision is not None
        assert evidence.recall_key is not None and evidence.captured_at is not None
        choices, passing = await _pinned_question(store, item, standard, evidence.recall_key, evidence.captured_at)
        score = next((row["score"] for row in choices if row["value"] == item.label), None)
        if score is None:
            raise KnowledgeError("KNOWLEDGE_ADOPTION_LABEL", "标签须来自固定的采纳标准。", 422)
        adoption[item.entry_id] = (item.entry_revision, score >= passing)
        outcomes[item.name] = {
            "scorer": item.name, "entry_id": str(item.entry_id), "entry_revision": item.entry_revision,
            "label": item.label, "score": score, "passed": score >= passing, "driver": item.driver,
            "scorer_id": str(item.scorer_id) if item.scorer_id is not None else None,
            "scorer_revision": item.scorer_revision, "scorer_source_hash": item.source_hash,
            "judge_call_id": str(operation),
        }
        if isolation is not None and item.driver != "human":
            outcomes[item.name]["isolation"] = isolation
    if len(preference) != 1 or (len(labels) > 1 and any(item.driver == "human" for item in labels)):
        raise KnowledgeError("KNOWLEDGE_SCORER_BATCH_SCOPE", "同一次调用须使用相同模型和客户端。", 422)
    if isinstance(subject, NamedLocal):
        meta = await register_local_evidence(
            store, subject.runtime, subject.subject_key, subject.digest, request_id,
            recall_session_key=evidence.recall_key, captured_at=evidence.captured_at, skill_evidence=evidence.skills,
        )
        identifier, revision = UUID(str(meta["id"])), int(str(meta["revision"]))
        # 纯路由调用没有后续用量写入，先落库再保存引用该版本的收据。
        await store.db.flush()
    else:
        identifier, revision = subject.archive_id, subject.revision
    applied: object = []
    if adoption and await store.adoption_enabled(lock=True):
        assert evidence.recall_key is not None and evidence.captured_at is not None
        attribution = AdoptionEvidence(
            identifier, revision, evidence.captured_at, local=isinstance(subject, NamedLocal), judge_call_id=operation,
        )
        result = await store.judge_adoption_for_key(evidence.recall_key, adoption, attribution)
        applied = result["applied"]
    assert isinstance(applied, list)
    for outcome in outcomes.values():
        if "entry_id" in outcome:
            outcome["applied"] = outcome["entry_id"] in applied
    for item, standard in ordinary:
        outcomes[item.name] = await write_label(store, identifier, revision, SessionJudgment(
            standard.id, standard.revision, uuid5(operation, item.name), item.label, item.driver,
            operation if len(labels) > 1 or offer_mode is not None else None, isolation,
        ), request_id)
    stored = KnowledgeNamedScoreCall(
        judge_id=scope.user, id=operation, workspace_id=scope.workspace, project_id=scope.project,
        archive_id=identifier, archive_revision=revision, request_hash=request_hash, request_id=request_id,
        items=[outcomes[item.name] for item in labels], created_at=datetime.now(UTC),
    )
    store.db.add(stored)
    return _receipt(stored)
