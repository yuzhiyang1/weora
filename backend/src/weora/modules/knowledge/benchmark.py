"""管理回归任务及提案覆盖，套件行锁串行化任务登记和提案发布。"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import Select, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from weora.modules.knowledge.benchmark_models import (
    BenchmarkTool,
    KnowledgeImprovementRevision,
    KnowledgeRegressionProposal,
    KnowledgeRegressionSuite,
    KnowledgeRegressionTask,
)
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.improvement_plan import regression_candidates
from weora.modules.knowledge.store import KnowledgeStore
from weora.modules.knowledge.transcript_archive import score_source_key
from weora.modules.projects.domain import ProjectActor
from weora.modules.projects.service import ProjectService

DEFAULT_SUITE = "benchmarks/regressions"
SUITE_PATH_PATTERN = r"^benchmarks/(?:[A-Za-z0-9][A-Za-z0-9_-]*/)?[A-Za-z0-9][A-Za-z0-9_-]*$"
BENCHMARK_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"


@dataclass(frozen=True, slots=True)
class SuiteConfig:
    path: str
    repository_id: UUID
    description: str
    tools: list[BenchmarkTool]
    repetitions: int
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class TaskDraft:
    operation_id: UUID
    label_id: int
    scorer: str
    name: str
    title: str
    prompt: str
    starting_commit: str
    success_criteria: str
    proposed: bool
    source: tuple[str, UUID] | None


def _project(store: KnowledgeStore) -> UUID:
    if store.scope.project is None:
        raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_PROJECT_REQUIRED", "回归套件需要选择项目。", 422)
    return store.scope.project


async def _suite(
    store: KnowledgeStore, path: str, *, lock: Literal["read", "write"] | None = None, required: bool = True,
) -> KnowledgeRegressionSuite | None:
    suite = KnowledgeRegressionSuite
    query = select(suite).where(
        suite.workspace_id == store.scope.workspace, suite.project_id == _project(store), suite.path == path,
    )
    row = await store.db.scalar(query.with_for_update(read=lock == "read") if lock else query)
    if row is None and required:
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_SUITE_NOT_FOUND", "当前项目找不到回归套件。", 404)
    return row


async def _lock_connection(
    store: KnowledgeStore, projects: ProjectService, repository: UUID,
) -> int:
    version = dict(store.scope.code_connections).get(repository)
    if version is None:
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_REPOSITORY", "套件仓库必须是项目当前的活动连接。", 409)
    await projects.lock_active_repository(
        ProjectActor(store.scope.user, store.scope.workspace), _project(store), repository, version, store.db,
    )
    return version


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    utc = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return utc.isoformat()


def _suite_view(suite: KnowledgeRegressionSuite) -> dict[str, object]:
    return {
        "id": suite.id, "path": suite.path, "repository_id": suite.repository_id,
        "description": suite.description, "tools": suite.tools, "repetitions": suite.repetitions,
        "timeout_seconds": suite.timeout_seconds, "created_by": suite.created_by,
        "created_at": _timestamp(suite.created_at),
    }


async def register_suite(
    store: KnowledgeStore, projects: ProjectService, config: SuiteConfig,
) -> dict[str, object]:
    store.require_reviewer()
    project = _project(store)
    if config.repository_id not in dict(store.scope.code_connections):
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_REPOSITORY", "套件仓库必须是项目当前的活动连接。", 409)
    insert = pg_insert if store.db.get_bind().dialect.name == "postgresql" else sqlite_insert
    values = asdict(config)
    await store.db.execute(insert(KnowledgeRegressionSuite).values(
        **values, id=uuid4(), workspace_id=store.scope.workspace, project_id=project,
        created_by=store.scope.user, created_at=datetime.now(UTC),
    ).on_conflict_do_nothing(index_elements=["workspace_id", "project_id", "path"]))
    suite = await _suite(store, config.path, lock="write")
    assert suite is not None
    view = _suite_view(suite)
    if any(view[key] != value for key, value in values.items()):
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_SUITE_EXISTS", "套件路径已登记为其他配置。", 409)
    await _lock_connection(store, projects, config.repository_id)
    return view


def _proposal_view(proposal: KnowledgeRegressionProposal) -> dict[str, object]:
    return {
        "id": proposal.id, "scorer": proposal.scorer, "status": proposal.status,
        "created_by": proposal.created_by, "created_at": _timestamp(proposal.created_at),
        "settled_by": proposal.settled_by, "settled_at": _timestamp(proposal.settled_at),
    }


def _task_view(
    suite: KnowledgeRegressionSuite, task: KnowledgeRegressionTask, proposal: KnowledgeRegressionProposal | None,
) -> dict[str, object]:
    return {
        "id": task.id, "path": f"{suite.path}/tasks/{task.name}.yaml", "subject_id": task.subject_id,
        "scoring_label_id": task.label_id, "title": task.title, "prompt": task.prompt,
        "starting_commit": task.starting_commit, "connection_version": task.connection_version,
        "success_criteria": task.success_criteria, "created_by": task.created_by,
        "created_at": _timestamp(task.created_at),
        "proposal": _proposal_view(proposal) if proposal else None,
    }


async def _open_proposal(
    store: KnowledgeStore, suite: KnowledgeRegressionSuite, scorer_id: UUID,
) -> KnowledgeRegressionProposal | None:
    proposal = KnowledgeRegressionProposal
    row: KnowledgeRegressionProposal | None = await store.db.scalar(select(proposal).where(
        proposal.suite_id == suite.id, proposal.scorer_id == scorer_id, proposal.status == "open",
    ))
    return row


def _active_tasks(
    suite: KnowledgeRegressionSuite, proposal: KnowledgeRegressionProposal | None,
) -> Select[tuple[KnowledgeRegressionTask, KnowledgeRegressionProposal]]:
    task, origin = KnowledgeRegressionTask, KnowledgeRegressionProposal
    default = task.proposal_id.is_(None) | (origin.status == "merged")
    active = default if proposal is None else or_(default, task.proposal_id == proposal.id)
    return select(task, origin).outerjoin(origin, origin.id == task.proposal_id).where(
        task.suite_id == suite.id, active,
    )


async def suite_coverage(
    store: KnowledgeStore, path: str | None, scorer_id: UUID,
) -> tuple[dict[UUID, str], UUID | None, UUID | None]:
    suite = await _suite(store, path or DEFAULT_SUITE, lock="read", required=path is not None)
    if suite is None:
        return {}, None, None
    proposal = await _open_proposal(store, suite, scorer_id)
    task = KnowledgeRegressionTask
    query = _active_tasks(suite, proposal).with_only_columns(task.subject_id, task.name, maintain_column_froms=True)
    # 开放提案优先。覆盖查询只读会话标识和任务路径。
    if proposal:
        query = query.order_by((task.proposal_id == proposal.id).desc().nulls_last())
    rows = (await store.db.execute(query.order_by(task.name))).tuples()
    coverage: dict[UUID, str] = {}
    for subject, name in rows:
        coverage.setdefault(subject, f"{suite.path}/tasks/{name}.yaml")
    return coverage, proposal.id if proposal else None, suite.repository_id


async def register_task(
    store: KnowledgeStore, projects: ProjectService, path: str, draft: TaskDraft, *, generated_batch: bool = False,
) -> dict[str, object]:
    store.require_reviewer()
    suite = await _suite(store, path, lock="write")
    assert suite is not None
    spec = asdict(draft)
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()
    task = KnowledgeRegressionTask
    previous = await store.db.scalar(select(task).where(
        task.suite_id == suite.id, task.operation_id == draft.operation_id,
    ))
    if previous:
        if previous.request_digest != digest or previous.created_by != store.scope.user:
            raise KnowledgeError("KNOWLEDGE_BENCHMARK_OPERATION_CONFLICT", "该操作标识已用于另一份任务。", 409)
        origin = await store.db.get(KnowledgeRegressionProposal, previous.proposal_id) if previous.proposal_id else None
        return _task_view(suite, previous, origin)

    version = await _lock_connection(store, projects, suite.repository_id)
    scorer_id, failures = await regression_candidates(store, draft.scorer, label_id=draft.label_id)
    candidate = failures[0] if failures else None
    recorded = candidate.recorded if candidate else None
    git = recorded.get("git_start") if recorded else None
    if (
        candidate is None or not isinstance(git, dict) or git.get("repository_id") != suite.repository_id
        or git.get("commit") != draft.starting_commit or not recorded or recorded.get("connection_version") != version
    ):
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_START", "任务须使用所选标准的有效失败及套件仓库的干净起点。", 422)

    if draft.source is not None and (
        candidate.latest["runtime"] != draft.source[0]
        or candidate.source_key != score_source_key(*draft.source)
    ):
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_SOURCE", "任务来源会话须与评分证据一致。", 422)

    proposal = await _open_proposal(store, suite, scorer_id) if draft.proposed else None
    if proposal is not None and not generated_batch:
        revision = await store.db.scalar(select(KnowledgeImprovementRevision.revision).where(
            KnowledgeImprovementRevision.proposal_id == proposal.id,
        ).limit(1))
        if revision is not None:
            raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_REVISION", "生成提案的任务须在同一修订中更新。", 409)
    duplicate = _active_tasks(suite, proposal).with_only_columns(task.id, maintain_column_froms=True).where(
        or_(task.subject_id == candidate.subject_id, task.name == draft.name),
    )
    if await store.db.scalar(duplicate.limit(1)):
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_TASK_EXISTS", "该会话或任务名已在默认版本或开放提案中。", 409)
    now = datetime.now(UTC)
    if draft.proposed and proposal is None:
        proposal = KnowledgeRegressionProposal(
            id=uuid4(), suite_id=suite.id, scorer_id=scorer_id, scorer=draft.scorer, status="open",
            created_by=store.scope.user, created_at=now,
        )
        store.db.add(proposal)
        await store.db.flush()
    row = KnowledgeRegressionTask(
        id=uuid4(), suite_id=suite.id, proposal_id=proposal.id if proposal else None, operation_id=draft.operation_id,
        request_digest=digest, subject_id=candidate.subject_id, label_id=draft.label_id, name=draft.name,
        title=draft.title, prompt=draft.prompt, starting_commit=draft.starting_commit, connection_version=version,
        success_criteria=draft.success_criteria, created_by=store.scope.user, created_at=now,
    )
    store.db.add(row)
    return _task_view(suite, row, proposal)


async def read_suite(
    store: KnowledgeStore, path: str, proposal_id: UUID | None,
) -> dict[str, object]:
    suite = await _suite(store, path, lock="read")
    assert suite is not None
    proposal = await _proposal(store, suite, proposal_id) if proposal_id else None
    query = _active_tasks(suite, proposal).order_by(KnowledgeRegressionTask.name)
    tasks = (await store.db.execute(query)).tuples()
    return {"suite": _suite_view(suite), "tasks": [_task_view(suite, task, parent) for task, parent in tasks]}


async def _proposal(
    store: KnowledgeStore, suite: KnowledgeRegressionSuite, identifier: UUID,
) -> KnowledgeRegressionProposal:
    model = KnowledgeRegressionProposal
    row = await store.db.scalar(select(model).where(model.id == identifier, model.suite_id == suite.id))
    if row is None:
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_PROPOSAL_NOT_FOUND", "当前套件找不到改进提案。", 404)
    return row


async def settle_proposal(
    store: KnowledgeStore, path: str, identifier: UUID, target: Literal["merged", "closed"],
    *, verified_delivery: bool = False,
) -> dict[str, object]:
    store.require_reviewer()
    suite = await _suite(store, path, lock="write")
    assert suite is not None
    proposal = await _proposal(store, suite, identifier)
    if proposal.status == target:
        return _proposal_view(proposal)
    if proposal.status != "open":
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_PROPOSAL_SETTLED", "改进提案已结束。", 409)
    if target == "merged":
        generated = await store.db.scalar(select(KnowledgeImprovementRevision.revision).where(
            KnowledgeImprovementRevision.proposal_id == identifier,
        ).limit(1))
        if generated is not None and not verified_delivery:
            raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_DELIVERY_REQUIRED", "须先交付并核验提案中的资源变更。", 409)
        task = KnowledgeRegressionTask
        query = _active_tasks(suite, proposal).with_only_columns(
            task.proposal_id, task.name, task.subject_id, maintain_column_froms=True,
        )
        tasks = (await store.db.execute(query)).tuples().all()
        base_names = {name for parent, name, _ in tasks if parent != identifier}
        base_subjects = {subject for parent, _, subject in tasks if parent != identifier}
        if any(parent == identifier and (name in base_names or subject in base_subjects)
               for parent, name, subject in tasks):
            raise KnowledgeError("KNOWLEDGE_BENCHMARK_MERGE_CONFLICT", "默认套件已有相同名称或会话的任务。", 409)
    proposal.status = target
    proposal.settled_at = datetime.now(UTC)
    proposal.settled_by = store.scope.user
    return _proposal_view(proposal)
