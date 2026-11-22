"""会话归档与可召回知识独立，按批准的范围保存脱敏正文。"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import defer

from weora.modules.knowledge.domain import AdoptionEvidence, ArchiveRunMode, KnowledgeError
from weora.modules.knowledge.models import (
    KnowledgeSessionLabel,
    KnowledgeSharedSession,
    KnowledgeSharedSessionRevision,
    SessionSkillEvidence,
    TranscriptTurn,
)
from weora.modules.knowledge.recall_journal import session_digest
from weora.modules.knowledge.session_keys import score_source_key as score_source_key
from weora.modules.knowledge.store import KnowledgeStore, Scope

MAX_TRANSCRIPT_BYTES = 512_000
MAX_TRANSCRIPT_TURNS = 500
MAX_TURN_TEXT = 12_000
_RAW_SECRET = re.compile(
    r"(?i)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|\bBearer\s+[\w.+/=-]{8,}|\bBasic\s+[A-Za-z0-9+/=]{12,}"
    r"|\b(?:sk-|gh[pousr]_|github_pat_|glpat-|xox[baprs]-|xai-|gsk_|pplx-|dop_v1_|shpat_)[\w-]{12,}"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bAIza[A-Za-z0-9_-]{35}\b|\bya29\.[\w-]{20,}"
    r"|\b(?:npm_|hf_)[A-Za-z0-9]{16,}\b|\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|https?://[^\s/@<>]+@"
    r"|\b\w*(?:token|password|passwd|(?:api|access|private)[_-]?key|secret|credential)\b[\"']?\s*[:=]\s*[\"']?"
    r"(?!<redacted>)[A-Za-z0-9._+/%=-]{8,}",
)
_RAW_PATH = re.compile(
    r"(?i)(?<![\w/])(?:[a-z]:[\\/]+|\\\\[^\\\s]+\\|/(?:[a-z]/)?"
    r"(?:Users|home|private/tmp|tmp|mnt|srv|workspace|workspaces|root)/)[^\s<>\x00\"']",
)


def _iso_utc(value: datetime) -> str:
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()


def _archive_id(scope: Scope, runtime: str, session: UUID) -> UUID:
    return uuid5(scope.workspace, f"knowledge-transcript:{scope.project}:{scope.user}:{runtime}:{session}")


def _receipt(archive: KnowledgeSharedSession, version: KnowledgeSharedSessionRevision) -> dict[str, object]:
    receipt: dict[str, object] = {
        "id": str(archive.id), "project_id": str(archive.project_id) if archive.project_id else None,
        "author_id": str(archive.author_id), "runtime": archive.runtime, "revision": version.revision,
        "title": version.title, "turn_count": version.turn_count, "published_at": _iso_utc(version.published_at),
        "digest": version.content_digest,
        "captured_at": _iso_utc(version.captured_at) if version.captured_at is not None else None,
        "archive_policy_id": version.archive_policy_id,
    }
    if archive.score_only:
        receipt["evidence"] = "local"
        if version.local_run_mode is not None:
            receipt["run_mode"] = version.local_run_mode
    return receipt


def _validate(title: str, turns: list[TranscriptTurn], *, captured: bool) -> str:
    try:
        serialized = json.dumps([title, turns], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    except UnicodeError:
        raise KnowledgeError("KNOWLEDGE_TRANSCRIPT_INVALID", "会话包含无效 Unicode 字符。", 422) from None
    if len(serialized) > MAX_TRANSCRIPT_BYTES:
        raise KnowledgeError("KNOWLEDGE_TRANSCRIPT_TOO_LARGE", "分享会话超过归档大小限制。", 422)
    for text in [title, *(turn["text"] for turn in turns)]:
        if _RAW_SECRET.search(text) or _RAW_PATH.search(text):
            raise KnowledgeError("KNOWLEDGE_TRANSCRIPT_UNSCRUBBED", "检测到未脱敏的凭据或本机路径，请重新预览。", 422)
    # 旧归档可在重新预览后补发带采集证明的版本。同格式、同正文的重试继续去重。
    digest_input = serialized + b"\x00captured-v1" if captured else serialized
    return hashlib.sha256(digest_input).hexdigest()


def safe_skill_description(text: str | None) -> str | None:
    if not text or _RAW_SECRET.search(text) or _RAW_PATH.search(text):
        return None
    try:
        text.encode("utf-8")
    except UnicodeError:
        return None
    return text


async def publish_transcript(
    store: KnowledgeStore, runtime: str, session: UUID, title: str, turns: list[TranscriptTurn],
    request_id: str | None, captured_at: datetime | None,
    *, archive_policy_id: int | None = None, skill_evidence: SessionSkillEvidence | None = None,
    skill_capture_digest: str | None = None,
) -> dict[str, object]:
    digest = _validate(title, turns, captured=captured_at is not None)
    now = datetime.now(UTC)
    if captured_at is not None:
        if captured_at > now + timedelta(minutes=5):
            raise KnowledgeError("KNOWLEDGE_TRANSCRIPT_CLOCK", "归档采集时间超前，请校准设备后重新预览。", 422)
        captured_at = min(captured_at, now)
    if skill_capture_digest is not None:
        # 同一转录可以发布新的 Skill 快照。服务端随后收到的用量不参与幂等键。
        digest = hashlib.sha256(f"{digest}\0skill-evidence-v1:{skill_capture_digest}".encode()).hexdigest()
    # 两条发布路径使用相同的正文指纹，从而识别同一会话的重复评分证据。
    source_key = score_source_key(runtime, session)
    encoded = json.dumps(turns, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hashlib.sha256(f"weora-score-evidence:{source_key}\0".encode() + encoded).hexdigest()
    return await _save_transcript_version(
        store, _archive_id(store.scope, runtime, session), runtime, title, turns, digest, request_id, now,
        captured_at=captured_at, archive_policy_id=archive_policy_id, source_key=source_key, fingerprint=fingerprint,
        recall_session_key=session_digest(str(session)) if captured_at is not None else None,
        skill_evidence=skill_evidence,
    )


async def register_local_evidence(
    store: KnowledgeStore, runtime: str, subject_key: str, digest: str, request_id: str | None,
    *, mode: ArchiveRunMode | None = None,
    recall_session_key: str | None = None, captured_at: datetime | None = None,
    skill_evidence: SessionSkillEvidence | None = None,
) -> dict[str, object]:
    """登记设备提供的指纹，为本机与公开归档生成分开的身份。"""
    identifier = local_score_id(store.scope, runtime, subject_key)
    now = datetime.now(UTC)
    if captured_at is not None:
        if captured_at > now + timedelta(minutes=5):
            raise KnowledgeError("KNOWLEDGE_TRANSCRIPT_CLOCK", "评分采集时间超前，请校准设备后重试。", 422)
        captured_at = min(captured_at, now)
    return await _save_transcript_version(
        store, identifier, runtime, "本机会话", [], digest, request_id, now,
        score_only=True, source_key=subject_key, fingerprint=digest, local_mode=mode,
        recall_session_key=recall_session_key, captured_at=captured_at, skill_evidence=skill_evidence,
    )


def local_score_id(scope: Scope, runtime: str, subject_key: str) -> UUID:
    return uuid5(scope.workspace, f"knowledge-local-score:{scope.project}:{scope.user}:{runtime}:{subject_key}")


async def _save_transcript_version(
    store: KnowledgeStore, identifier: UUID, runtime: str, title: str, turns: list[TranscriptTurn],
    digest: str, request_id: str | None, now: datetime, *, captured_at: datetime | None = None,
    archive_policy_id: int | None = None, score_only: bool = False,
    source_key: str, fingerprint: str, local_mode: ArchiveRunMode | None = None,
    recall_session_key: str | None = None, skill_evidence: SessionSkillEvidence | None = None,
) -> dict[str, object]:
    db, scope = store.db, store.scope
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    # 先创建稳定身份，再在同一事务串行分配版本。跨设备重试相同正文也返回原收据。
    await db.execute(insert(KnowledgeSharedSession).values(
        id=identifier, workspace_id=scope.workspace, project_id=scope.project,
        author_id=scope.user, runtime=runtime, score_only=score_only, latest_revision=0, updated_at=now,
    ).on_conflict_do_nothing(index_elements=["id"]))
    archive = await db.get(KnowledgeSharedSession, identifier, with_for_update=True, populate_existing=True)
    assert archive is not None
    if (archive.workspace_id, archive.project_id, archive.author_id, archive.runtime, archive.score_only) != (
        scope.workspace, scope.project, scope.user, runtime, score_only,
    ):
        raise KnowledgeError("KNOWLEDGE_TRANSCRIPT_SCOPE", "会话版本标识与当前范围不一致。", 409)
    archive.score_source_key = source_key
    versions = KnowledgeSharedSessionRevision
    if score_only and recall_session_key is not None and await db.scalar(select(versions.revision).where(
        versions.archive_id == identifier, versions.recall_session_key != recall_session_key,
    ).limit(1)):
        raise KnowledgeError("KNOWLEDGE_ADOPTION_EVIDENCE_SCOPE", "同一会话的召回归因键已经固定。", 409)
    prior = await db.scalar(select(KnowledgeSharedSessionRevision).where(
        KnowledgeSharedSessionRevision.archive_id == identifier,
        KnowledgeSharedSessionRevision.content_digest == digest,
    ))
    if prior is not None:
        prior.score_fingerprint = fingerprint
        if local_mode is not None and prior.local_run_mode not in (None, local_mode):
            raise KnowledgeError("KNOWLEDGE_LOCAL_SCORE_MODE", "该版本已登记到另一种后台运行模式。", 409)
        if prior.local_run_mode is None:
            prior.local_run_mode = local_mode
        if prior.skill_evidence is None and skill_evidence is not None and (
            prior.captured_at is None
            or (captured_at is not None and _iso_utc(prior.captured_at) == _iso_utc(captured_at))
        ):
            # 只有未采集的本机快照可补传 Skill 证据，且不能移动既有截止点。
            prior.skill_evidence = skill_evidence
        if prior.recall_session_key is None and recall_session_key is not None:
            prior.recall_session_key = recall_session_key
            prior.captured_at = prior.captured_at or captured_at
        return _receipt(archive, prior)
    revision = archive.latest_revision + 1
    version = KnowledgeSharedSessionRevision(
        archive_id=identifier, revision=revision, title=title, turns=turns, turn_count=len(turns),
        content_digest=digest, request_id=request_id, captured_at=captured_at,
        archive_policy_id=archive_policy_id, published_at=now, score_fingerprint=fingerprint, local_run_mode=local_mode,
        recall_session_key=recall_session_key, skill_evidence=skill_evidence,
    )
    archive.latest_revision, archive.updated_at = revision, now
    db.add(version)
    return _receipt(archive, version)


async def list_transcripts(store: KnowledgeStore, limit: int) -> dict[str, object]:
    archive, version = KnowledgeSharedSession, KnowledgeSharedSessionRevision
    rows = await store.db.execute(select(
        archive, version.revision, version.title, version.turn_count, version.published_at, version.content_digest,
        version.captured_at, version.archive_policy_id,
    ).join(
        version, (version.archive_id == archive.id) & (version.revision == archive.latest_revision),
    ).where(
        archive.workspace_id == store.scope.workspace, archive.project_id == store.scope.project,
        archive.score_only.is_(False),
    ).order_by(archive.updated_at.desc(), archive.id).limit(limit))
    return {"items": [
        {
            "id": str(item.id), "project_id": str(item.project_id) if item.project_id else None,
            "author_id": str(item.author_id), "runtime": item.runtime, "revision": revision, "title": title,
            "turn_count": count, "published_at": _iso_utc(published), "digest": digest,
            "captured_at": _iso_utc(captured) if captured is not None else None,
            "archive_policy_id": policy_id,
        }
        for item, revision, title, count, published, digest, captured, policy_id in rows
    ]}


async def read_transcript(store: KnowledgeStore, identifier: UUID, revision: int | None) -> dict[str, object]:
    archive, version = await _find_transcript(store, identifier, revision)
    return _receipt(archive, version) | {"turns": version.turns}


async def transcript_metadata(
    store: KnowledgeStore, identifier: UUID, revision: int, *, read_labels: bool = False,
) -> dict[str, object]:
    """评分计划允许本人使用本地证据，团队成员只读其已提交的标签。"""
    archive, version = await _find_transcript(store, identifier, revision, include_turns=False, scoring=True)
    if archive.score_only and archive.author_id != store.scope.user:
        label = KnowledgeSessionLabel
        published = read_labels and await store.db.scalar(select(label.id).where(
            label.archive_id == identifier, label.archive_revision == revision,
        ).limit(1))
        if not published:
            raise KnowledgeError("KNOWLEDGE_TRANSCRIPT_NOT_FOUND", "该范围内找不到会话评分证据。", 404)
    return _receipt(archive, version)


async def read_skill_evidence(
    store: KnowledgeStore, identifier: UUID, revision: int,
) -> SessionSkillEvidence | None:
    archive, version = await _find_transcript(store, identifier, revision, include_turns=False, scoring=True)
    if archive.author_id != store.scope.user:
        raise KnowledgeError("KNOWLEDGE_TRANSCRIPT_NOT_FOUND", "当前范围内找不到本人的会话证据。", 404)
    return version.skill_evidence


async def adoption_archive(
    store: KnowledgeStore, session: str, identifier: UUID, revision: int,
) -> tuple[AdoptionEvidence, dict[str, object]]:
    """只允许本人、当前范围、同一原生根会话的归档作为采纳评分依据。"""
    archive, version = await _find_transcript(store, identifier, revision, include_turns=False)
    try:
        native = UUID(session)
    except ValueError:
        raise KnowledgeError("KNOWLEDGE_ADOPTION_ARCHIVE_SCOPE", "归档与评分根会话不一致。", 422) from None
    if archive.author_id != store.scope.user or _archive_id(store.scope, archive.runtime, native) != archive.id:
        raise KnowledgeError("KNOWLEDGE_ADOPTION_ARCHIVE_SCOPE", "归档与评分根会话不一致。", 422)
    captured = version.captured_at
    if captured is None:
        raise KnowledgeError("KNOWLEDGE_ADOPTION_ARCHIVE_TIME", "该版本缺少采集时间，请重新预览归档。", 422)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=UTC)
    return AdoptionEvidence(archive.id, revision, captured), _receipt(archive, version)


async def _find_transcript(
    store: KnowledgeStore, identifier: UUID, revision: int | None, *, include_turns: bool = True, scoring: bool = False,
) -> tuple[KnowledgeSharedSession, KnowledgeSharedSessionRevision]:
    archive, version = KnowledgeSharedSession, KnowledgeSharedSessionRevision
    sequence = version.revision == (revision if revision is not None else archive.latest_revision)
    statement = select(archive, version).join(
        version, (version.archive_id == archive.id) & sequence,
    ).where(
        archive.id == identifier, archive.workspace_id == store.scope.workspace,
        archive.project_id == store.scope.project,
    )
    if not scoring:
        statement = statement.where(archive.score_only.is_(False))
    if not include_turns:
        statement = statement.options(defer(version.turns))
    row = (await store.db.execute(statement)).tuples().one_or_none()
    if row is None:
        raise KnowledgeError("KNOWLEDGE_TRANSCRIPT_NOT_FOUND", "该范围内找不到已分享会话。", 404)
    entry, body = row
    return entry, body
