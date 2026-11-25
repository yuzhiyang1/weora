"""为 A/B 回归筛选评分失败，并关联同一根会话贡献的已发布经验。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import and_, func, select

from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import (
    KnowledgeEntry,
    KnowledgeRevision,
    KnowledgeSessionLabel,
    KnowledgeSessionOrigin,
    KnowledgeSessionScorer,
    KnowledgeSharedSession,
    KnowledgeSharedSessionRevision,
)
from weora.modules.knowledge.scoring_report import SCORING_WINDOW_DAYS, latest_label_ids
from weora.modules.knowledge.session_origin import start_state
from weora.modules.knowledge.session_scoring import list_scorers
from weora.modules.knowledge.store import KnowledgeStore, published
from weora.modules.knowledge.transcript_archive import score_source_key

type SessionKey = tuple[UUID, str, str]


@dataclass
class SubjectFailures:
    subject_id: UUID
    latest: dict[str, object]
    source_key: str | None = None
    recorded: dict[str, object] | None = None
    learnings: list[dict[str, object]] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "subject_id": self.subject_id, "latest": self.latest, "recorded": self.recorded,
            "learnings": self.learnings,
        }


async def regression_candidates(
    store: KnowledgeStore, name: str, *, label_id: int | None = None,
) -> tuple[UUID, list[SubjectFailures]]:
    """报告明细有上限。回归选择独立扫描所选标准的全部窗口内标签。"""
    scope, db = store.scope, store.db
    if scope.project is None:
        raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_PROJECT_REQUIRED", "回归计划需要选择项目。", 422)
    choices = await list_scorers(store, 1, [name], with_prompt=False)
    if not choices or choices[0].is_adoption_template:
        raise KnowledgeError("KNOWLEDGE_SCORER_NOT_FOUND", "当前项目找不到所选评分标准。", 404)
    selected = choices[0]
    now = datetime.now(UTC)
    since = now - timedelta(days=SCORING_WINDOW_DAYS)
    latest = latest_label_ids(store, since, now, scorer_id=selected.id)
    label, rubric = KnowledgeSessionLabel, KnowledgeSessionScorer
    archive, version, origin = KnowledgeSharedSession, KnowledgeSharedSessionRevision, KnowledgeSessionOrigin
    connections = dict(scope.code_connections)

    query = (
        select(
            label.id, label.archive_id, label.archive_revision, label.scorer_revision, label.driver, label.label,
            label.created_at, archive.author_id, archive.runtime, archive.score_source_key, archive.score_only,
            rubric.labels, rubric.passing_score, origin, func.coalesce(version.captured_at, version.published_at),
        )
        .select_from(label)
        .join(latest, and_(latest.c.id == label.id, latest.c.position == 1))
        .join(archive, archive.id == label.archive_id)
        .join(version, and_(version.archive_id == label.archive_id, version.revision == label.archive_revision))
        .join(rubric, and_(rubric.id == label.scorer_id, rubric.revision == label.scorer_revision))
        .outerjoin(origin, and_(
            origin.workspace_id == archive.workspace_id, origin.project_id == archive.project_id,
            origin.author_id == archive.author_id, origin.subject_key == archive.score_source_key,
        ))
        .order_by(label.created_at.desc(), label.id.desc())
    )
    if label_id is not None:
        query = query.where(label.id == label_id)
    rows = await db.stream(query)
    subjects: dict[SessionKey, SubjectFailures] = {}
    async for row in rows.tuples():
        (label_key, archive_id, archive_revision, scorer_revision, driver, value, scored_at, author, runtime,
         source, score_only, labels, passing, start, captured_at) = row
        score = next((option["score"] for option in labels if option["value"] == value), None)
        if score is None or score >= passing:
            continue
        key = (author, runtime, source or str(archive_id))
        git = start_state(start, connections, captured_at)
        failure: dict[str, object] = {
            "label_id": label_key, "archive_id": archive_id, "archive_revision": archive_revision,
            "scorer_revision": scorer_revision, "runtime": runtime, "driver": driver, "label": value, "score": score,
            "evidence": "local" if score_only else "archive", "scored_at": scored_at, "git_start": git,
        }
        subject = subjects.get(key)
        if subject is None:
            # 相同根会话的本机评分和发布归档合并。旧归档保留各自的稳定身份。
            subject = subjects[key] = SubjectFailures(
                uuid5(scope.project, f"knowledge-regression:{key}"), failure, source,
            )
        # 起点每个会话唯一。不同证据版本可能缺采集时点，保留最新的可用版本即可。
        if subject.recorded is None and git["status"] == "recorded":
            assert start is not None
            subject.recorded = failure | {"connection_version": start.connection_version}

    if subjects and label_id is None:
        await _link_learnings(store, subjects)
    return selected.id, list(subjects.values())


async def _link_learnings(store: KnowledgeStore, subjects: dict[SessionKey, SubjectFailures]) -> None:
    revision = KnowledgeRevision
    ref = revision.source["ref"].as_string()
    rows = await store.db.stream(
        published(store.scope).with_only_columns(
            revision.entry_id, revision.revision, revision.title, revision.author_id, ref,
            maintain_column_froms=True,
        ).where(
            KnowledgeEntry.kind == "learning", revision.source["type"].as_string() == "session",
        ).order_by(revision.entry_id)
    )
    async for entry, number, title, author, source in rows.tuples():
        runtime, sep, native = (source or "").partition(":")
        if not sep or runtime not in {"codex", "claude-code"}:
            continue
        try:
            key = (author, runtime, score_source_key(runtime, UUID(native)))
        except ValueError:
            continue
        if (subject := subjects.get(key)) is not None:
            subject.learnings.append({"id": entry, "revision": number, "title": title})
