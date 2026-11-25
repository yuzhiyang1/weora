"""团队评分采用已发布的评分标准和归档版本，标签独立于知识采纳。"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid5

from sqlalchemy import and_, case, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import defer
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from weora.modules.knowledge.adoption_rubric import ADOPTION_STANDARD, validate_adoption_template
from weora.modules.knowledge.domain import KnowledgeError, ScoringIsolation
from weora.modules.knowledge.models import (
    KnowledgeSessionLabel,
    KnowledgeSessionScorer,
    KnowledgeSharedSession,
    ScorerFile,
    ScorerLabel,
)
from weora.modules.knowledge.routing_rubric import ROUTING_STANDARD, validate_routing_template
from weora.modules.knowledge.scoring_policy import require_verified
from weora.modules.knowledge.store import KnowledgeStore
from weora.modules.knowledge.transcript_archive import transcript_metadata

ScorerTool = Literal["codex", "claude"]
ScoreDriver = Literal["human", ScorerTool]
SCORING_ATTEMPTS = 4
SCORING_WINDOW = timedelta(days=14)
SCORING_LEASE = timedelta(minutes=15)
SCORING_RETRY_DELAY = timedelta(minutes=1)
type LocalScoreRef = tuple[UUID, int, str]
type JudgeError = Literal["evidence_unavailable", "judge_failed", "invalid_answer"]


def utc_datetime(value: datetime) -> datetime:
    """SQLite 测试读出无时区时间，生产数据库保留实际时区。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ScorerDefinition:
    prompt: str
    labels: list[ScorerLabel]
    passing_score: float
    sampling_rate: float
    sessions: bool
    model: str | None
    tool: ScorerTool | None
    files: list[ScorerFile] | None = None
    isolation: ScoringIsolation | None = None


@dataclass(frozen=True, slots=True)
class SessionJudgment:
    scorer_id: UUID
    scorer_revision: int
    operation_id: UUID
    label: str
    driver: ScoreDriver
    judge_call_id: UUID | None = None
    isolation: ScoringIsolation | None = None


async def lock_judge(store: KnowledgeStore) -> None:
    """串行同一成员的评分领取与标签提交，统一每日用量的观察顺序。"""
    if store.db.get_bind().dialect.name == "postgresql":
        scope = store.scope
        key = hashlib.sha256(f"knowledge-score-judge:{scope.workspace}:{scope.user}".encode()).digest()[:8]
        await store.db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(key, "big", signed=True))))


def _rubric(row: KnowledgeSessionScorer, *, with_prompt: bool = False) -> dict[str, object]:
    receipt: dict[str, object] = {
        "id": str(row.id), "name": row.name, "revision": row.revision, "labels": row.labels,
        "project_id": str(row.project_id) if row.project_id else None, "passing_score": row.passing_score,
        "sampling_rate": row.sampling_rate, "sessions": row.sessions, "published_by": str(row.published_by),
        "model": row.model, "tool": row.tool,
    }
    if row.namespace:
        receipt["namespace"] = row.namespace
    if row.isolation is not None:
        receipt["isolation"] = row.isolation
    if with_prompt:
        receipt["prompt"] = row.prompt
        if row.files is not None:
            receipt["files"] = row.files
    if row.source_hash is not None:
        receipt["source_hash"] = row.source_hash
    if row.is_adoption_template:
        receipt["adoption_template"] = True
    if row.name == ROUTING_STANDARD:
        receipt["routing_template"] = True
    if row.adoption_template_archived:
        receipt["archived"] = True
    return receipt


def _judgment(row: KnowledgeSessionLabel, rubric: KnowledgeSessionScorer) -> dict[str, object]:
    score = next(value["score"] for value in rubric.labels if value["value"] == row.label)
    result: dict[str, object] = {
        "id": row.id, "scorer": rubric.name, "scorer_id": str(row.scorer_id), "scorer_revision": row.scorer_revision,
        "archive_id": str(row.archive_id), "archive_revision": row.archive_revision, "judge_id": str(row.judge_id),
        "label": row.label, "score": score, "passed": score >= rubric.passing_score, "driver": row.driver,
        "requested_model": rubric.model if row.driver != "human" else None,
        "operation_id": str(row.operation_id), "created_at": row.created_at.isoformat(),
    }
    if row.isolation is not None:
        result["isolation"] = row.isolation
    if row.judge_call_id is not None:
        result["judge_call_id"] = str(row.judge_call_id)
    if rubric.source_hash is not None:
        result["scorer_source_hash"] = rubric.source_hash
    return result


async def _lock_scorer(
    store: KnowledgeStore, name: str, namespace: str,
) -> tuple[UUID, KnowledgeSessionScorer | None]:
    db, scope = store.db, store.scope
    scorer = KnowledgeSessionScorer
    # 根目录沿用旧主键，目录内同名评分器拥有独立历史及有效锁。
    location = f"{namespace}/{name}" if namespace else name
    identifier = uuid5(scope.workspace, f"knowledge-session-scorer:{scope.project}:{location}")
    if db.get_bind().dialect.name == "postgresql":
        key = hashlib.sha256(identifier.bytes).digest()[:8]
        await db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(key, "big", signed=True))))
    current = await db.scalar(select(scorer).where(scorer.id == identifier).order_by(scorer.revision.desc()).limit(1))
    return identifier, current


async def publish_scorer(
    store: KnowledgeStore, name: str, definition: ScorerDefinition, base_revision: int | None, *, namespace: str = "",
) -> dict[str, object]:
    store.require_reviewer()
    is_adoption_template = name == ADOPTION_STANDARD
    if is_adoption_template:
        validate_adoption_template(definition.prompt, definition.labels, directory=definition.files is not None)
    elif name == ROUTING_STANDARD:
        validate_routing_template(definition.prompt, definition.labels, directory=definition.files is not None)
    identifier, current = await _lock_scorer(store, name, namespace)
    if (
        current and current.is_adoption_template == is_adoption_template and not current.adoption_template_archived
        and definition == ScorerDefinition(
            current.prompt, current.labels, current.passing_score, current.sampling_rate, current.sessions,
            current.model, current.tool, current.files, current.isolation,
        )
    ):
        return _rubric(current, with_prompt=True)
    revision = current.revision if current else 0
    if base_revision is not None and base_revision != revision:
        raise KnowledgeError("KNOWLEDGE_SCORER_VERSION", "评分标准已更新，请核对当前版本后再发布。", 409)
    source_hash = None
    if definition.files is not None or is_adoption_template or name == ROUTING_STANDARD:
        source = asdict(definition)
        if definition.isolation is None:
            # 未声明隔离级别的历史目录沿用原有摘要。
            del source["isolation"]
        packaged = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        source_hash = hashlib.sha256(packaged.encode("utf-8")).hexdigest()
    scope = store.scope
    row = KnowledgeSessionScorer(
        id=identifier, revision=revision + 1, workspace_id=scope.workspace, project_id=scope.project, name=name,
        namespace=namespace,
        prompt=definition.prompt, labels=definition.labels, passing_score=definition.passing_score,
        sampling_rate=definition.sampling_rate, sessions=definition.sessions,
        model=definition.model, tool=definition.tool, files=definition.files, source_hash=source_hash,
        isolation=definition.isolation, is_adoption_template=is_adoption_template,
        adoption_template_archived=False,
        published_by=scope.user, published_at=datetime.now(UTC),
    )
    store.db.add(row)
    return _rubric(row, with_prompt=True)


async def reset_generated_template(
    store: KnowledgeStore, name: str, base_revision: int | None, *, namespace: str = "",
) -> dict[str, object]:
    """关闭当前作用域的覆盖，为已领取的问题保留其旧版本。"""
    store.require_reviewer()
    identifier, current = await _lock_scorer(store, name, namespace)
    if current is None or (name == ADOPTION_STANDARD and not current.is_adoption_template):
        if base_revision not in (None, 0):
            raise KnowledgeError("KNOWLEDGE_SCORER_VERSION", "当前范围没有所选生成式评分模板。", 409)
        return {"name": name, "archived": True, "revision": 0} | (
            {"namespace": namespace} if namespace else {}
        )
    if current.adoption_template_archived:
        return _rubric(current)
    if base_revision is not None and base_revision != current.revision:
        raise KnowledgeError("KNOWLEDGE_SCORER_VERSION", "评分标准已更新，请核对当前版本后再恢复默认。", 409)
    row = KnowledgeSessionScorer(
        id=identifier, revision=current.revision + 1, workspace_id=current.workspace_id, project_id=current.project_id,
        name=current.name, namespace=current.namespace, prompt=current.prompt, labels=current.labels,
        passing_score=current.passing_score,
        sampling_rate=current.sampling_rate, sessions=current.sessions, model=current.model, tool=current.tool,
        source_hash=current.source_hash, isolation=current.isolation,
        is_adoption_template=current.is_adoption_template, adoption_template_archived=True,
        published_by=store.scope.user, published_at=datetime.now(UTC),
    )
    store.db.add(row)
    return _rubric(row)


def _visible(store: KnowledgeStore) -> ColumnElement[bool]:
    scorer = KnowledgeSessionScorer
    return (scorer.workspace_id == store.scope.workspace) & or_(
        scorer.project_id.is_(None), scorer.project_id == store.scope.project,
    )


def _selected_scorers(
    store: KnowledgeStore, names: list[str] | None, *, generated_template: bool = False,
) -> Subquery:
    scorer = KnowledgeSessionScorer
    folders = store.scope.namespaces
    revisions = select(
        scorer.id, scorer.revision, scorer.name, scorer.namespace, scorer.project_id,
        scorer.is_adoption_template, scorer.adoption_template_archived,
        func.row_number().over(partition_by=scorer.id, order_by=scorer.revision.desc()).label("latest"),
    ).where(_visible(store))
    if names:
        revisions = revisions.where(scorer.name.in_(names))
    if folders is not None:
        revisions = revisions.where(scorer.namespace.not_in(folders.inactive_scorers))
    historical = revisions.subquery()

    # 先固定每个来源的版本，再选择同一目录内的项目覆盖。
    same_path = select(
        historical.c.id, historical.c.revision, historical.c.name, historical.c.namespace,
        func.row_number().over(
            partition_by=(historical.c.name, historical.c.namespace),
            order_by=(historical.c.project_id.is_(None), historical.c.id),
        ).label("preferred"),
    ).where(historical.c.latest == 1)
    if generated_template:
        same_path = same_path.where(historical.c.adoption_template_archived.is_(False))
        if names == [ADOPTION_STANDARD]:
            same_path = same_path.where(historical.c.is_adoption_template.is_(True))
    sources = same_path.subquery()
    named = sources.c.namespace != "" if folders is None else sources.c.namespace.in_(folders.scorers)
    ranking = select(
        sources.c.id, sources.c.revision,
        func.row_number().over(
            partition_by=sources.c.name, order_by=(case((named, 0), else_=1), sources.c.id),
        ).label("position"),
        func.count().filter(named).over(partition_by=sources.c.name).label("namespace_copies"),
        func.count().filter(~named).over(partition_by=sources.c.name).label("shared_copies"),
    ).where(sources.c.preferred == 1).subquery()
    # 同名的多个活跃目录或共享来源都会形成歧义，该名称不进入自动或点名评分。
    return select(ranking.c.id, ranking.c.revision).where(
        ranking.c.position == 1, ranking.c.namespace_copies <= 1, ranking.c.shared_copies <= 1,
    ).subquery()


async def list_scorers(
    store: KnowledgeStore, limit: int, names: list[str] | None = None, *, automatic: bool = False,
    with_prompt: bool = True,
) -> list[KnowledgeSessionScorer]:
    scorer = KnowledgeSessionScorer
    rows = _selected_scorers(store, names)
    statement = select(scorer).join(rows, and_(
        rows.c.id == scorer.id, rows.c.revision == scorer.revision,
    ))
    if automatic:
        statement = statement.where(
            scorer.sessions.is_(True), scorer.sampling_rate > 0, scorer.is_adoption_template.is_(False),
            scorer.name != ROUTING_STANDARD,
        )
    if not with_prompt:
        statement = statement.options(defer(scorer.files), defer(scorer.prompt))
    return list(await store.db.scalars(statement.order_by(scorer.name).limit(limit)))


async def scorer_catalog(store: KnowledgeStore, limit: int) -> dict[str, object]:
    return {"items": [_rubric(row) for row in await list_scorers(store, limit, with_prompt=False)]}


async def generated_template(store: KnowledgeStore, name: str) -> KnowledgeSessionScorer | None:
    scorer = KnowledgeSessionScorer
    rows = _selected_scorers(store, [name], generated_template=True)
    standard: KnowledgeSessionScorer | None = await store.db.scalar(select(scorer).join(rows, and_(
        rows.c.id == scorer.id, rows.c.revision == scorer.revision,
    )))
    return standard


async def adoption_template(store: KnowledgeStore) -> KnowledgeSessionScorer | None:
    return await generated_template(store, ADOPTION_STANDARD)


async def load_scorer_contents(store: KnowledgeStore, rows: list[KnowledgeSessionScorer]) -> None:
    """按已经选定的版本批量补齐正文和附件，避免为采样候选读取整包。"""
    if rows:
        scorer = KnowledgeSessionScorer
        matches = await store.db.scalars(select(scorer).where(
            _visible(store), tuple_(scorer.id, scorer.revision).in_([(row.id, row.revision) for row in rows]),
        ))
        # ORM 会对会话中已经加载的这些对象补齐延迟列。
        matches.all()


async def score_plan(store: KnowledgeStore, archive_id: UUID, revision: int, names: list[str]) -> dict[str, object]:
    subject = await transcript_metadata(store, archive_id, revision)
    return {"archive": subject, **await rubric_plan(store, names)}


async def rubric_plan(store: KnowledgeStore, names: list[str]) -> dict[str, object]:
    available = {row.name: row for row in await list_scorers(store, len(names), names)}
    if any(row.is_adoption_template for row in available.values()):
        raise KnowledgeError("KNOWLEDGE_ADOPTION_TEMPLATE_USE", "请通过采纳评分入口使用团队采纳标准。", 422)
    if ROUTING_STANDARD in available:
        raise KnowledgeError("KNOWLEDGE_ROUTING_TEMPLATE_USE", "请点名 teamai:routing: 下的 Skill 路由问题。", 422)
    missing = [name for name in names if name not in available]
    if missing:
        raise KnowledgeError("KNOWLEDGE_SCORER_NOT_FOUND", f"找不到评分标准：{', '.join(missing)}。", 404)
    return {"items": [_rubric(available[name], with_prompt=True) for name in names]}


async def write_label(
    store: KnowledgeStore, archive_id: UUID, revision: int, verdict: SessionJudgment, request_id: str | None,
) -> dict[str, object]:
    subject = await transcript_metadata(store, archive_id, revision)
    await lock_judge(store)
    db, scope = store.db, store.scope
    scorer = KnowledgeSessionScorer
    rubric = await db.scalar(select(scorer).where(
        _visible(store), scorer.id == verdict.scorer_id, scorer.revision == verdict.scorer_revision,
        scorer.is_adoption_template.is_(False), scorer.name != ROUTING_STANDARD,
    ).options(defer(scorer.files), defer(scorer.prompt)))
    if rubric is None:
        raise KnowledgeError("KNOWLEDGE_SCORER_NOT_FOUND", "当前范围内找不到该评分标准版本。", 404)
    if verdict.driver != "human":
        require_verified(rubric.isolation == "verified", verdict.isolation)
    if not any(label["value"] == verdict.label for label in rubric.labels):
        raise KnowledgeError("KNOWLEDGE_SCORER_LABEL", "标签必须来自所选评分版本的标签表。", 422)
    label = KnowledgeSessionLabel
    if verdict.judge_call_id is not None:
        if verdict.driver == "human":
            raise KnowledgeError("KNOWLEDGE_SCORER_BATCH_SCOPE", "人工标签每次只对应一项标准。", 422)
        existing_call = (await db.execute(select(label, scorer).join(scorer, and_(
            label.scorer_id == scorer.id, label.scorer_revision == scorer.revision,
        )).where(label.judge_id == scope.user, label.judge_call_id == verdict.judge_call_id)
            .options(defer(scorer.files), defer(scorer.prompt)).limit(1))).tuples().first()
        if existing_call:
            previous, standard = existing_call
            if (previous.archive_id, previous.archive_revision, previous.driver, standard.tool, standard.model) != (
                archive_id, revision, verdict.driver, rubric.tool, rubric.model,
            ):
                raise KnowledgeError(
                    "KNOWLEDGE_SCORER_BATCH_SCOPE", "合并评分须使用同一证据版本及同一客户端和模型。", 409,
                )
        prior_operation = await db.scalar(select(label.operation_id).where(
            label.judge_id == scope.user, label.judge_call_id == verdict.judge_call_id, label.scorer_id == rubric.id,
        ))
        if prior_operation is not None and prior_operation != verdict.operation_id:
            raise KnowledgeError(
                "KNOWLEDGE_SCORER_BATCH_SCOPE", "同一次评分调用只能为每项标准提交一个标签。", 409,
            )
    content = {
        "archive_id": archive_id, "archive_revision": revision, "scorer_id": rubric.id,
        "scorer_revision": rubric.revision,
        "driver": verdict.driver, "label": verdict.label, "judge_call_id": verdict.judge_call_id,
        "isolation": verdict.isolation if verdict.driver != "human" else None,
    }
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    # 同一请求沿用原收据。重新评分可另起请求，保持人工与模型的独立历史。
    await db.execute(insert(label).values(
        **content, judge_id=scope.user, operation_id=verdict.operation_id, request_id=request_id,
        created_at=datetime.now(UTC),
    ).on_conflict_do_nothing(index_elements=["judge_id", "operation_id"]))
    saved = await db.scalar(select(label).where(
        label.judge_id == scope.user, label.operation_id == verdict.operation_id,
    ))
    assert saved is not None
    if any(getattr(saved, field) != value for field, value in content.items()):
        raise KnowledgeError("KNOWLEDGE_SCORER_OPERATION", "请求键已用于另一份评分。", 409)
    result = _judgment(saved, rubric)
    return result | {"evidence": "local"} if subject.get("evidence") == "local" else result


async def list_labels(
    store: KnowledgeStore, archive_id: UUID, revision: int, limit: int, *, history: bool,
) -> dict[str, object]:
    subject = await transcript_metadata(store, archive_id, revision, read_labels=True)
    label, scorer = KnowledgeSessionLabel, KnowledgeSessionScorer
    scoped = select(label.id).where(label.archive_id == archive_id, label.archive_revision == revision)
    if not history:
        rank = func.row_number().over(
            partition_by=(label.scorer_id, label.scorer_revision, label.judge_id, label.driver),
            order_by=label.id.desc(),
        )
        ranked = scoped.add_columns(rank.label("position")).subquery()
        scoped = select(ranked.c.id).where(ranked.c.position == 1)
    rows = await store.db.execute(select(label, scorer).join(scorer, and_(
        label.scorer_id == scorer.id, label.scorer_revision == scorer.revision,
    )).where(label.id.in_(scoped), _visible(store))
        .options(defer(scorer.files), defer(scorer.prompt))
        .order_by(label.id.desc()).limit(limit))
    evidence = {"evidence": "local"} if subject.get("evidence") == "local" else {}
    return {"items": [_judgment(row, rubric) | evidence for row, rubric in rows.tuples()]}


async def scored_local_sessions(store: KnowledgeStore, limit: int) -> dict[str, object]:
    """从团队标签反查会话，仅公开已评分的证据标识。"""
    label, archive = KnowledgeSessionLabel, KnowledgeSharedSession
    rank = func.row_number().over(partition_by=archive.id, order_by=(label.archive_revision.desc(), label.id.desc()))
    latest = select(
        archive.id, archive.author_id, archive.runtime, label.archive_revision, label.created_at,
        rank.label("position"),
    ).join(label, label.archive_id == archive.id).where(
        archive.workspace_id == store.scope.workspace, archive.project_id == store.scope.project,
        archive.score_only.is_(True),
    ).subquery()
    rows = await store.db.execute(select(
        latest.c.id, latest.c.author_id, latest.c.runtime, latest.c.archive_revision, latest.c.created_at,
    ).where(latest.c.position == 1).order_by(latest.c.created_at.desc(), latest.c.id).limit(limit))
    return {"items": [
        {"id": str(identifier), "author_id": str(author), "runtime": runtime, "revision": revision,
         "scored_at": when.isoformat()}
        for identifier, author, runtime, revision, when in rows.tuples()
    ]}
