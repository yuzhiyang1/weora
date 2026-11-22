"""同一改进事务写入资源差异和每个失败的回归任务。"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import select

from weora.modules.knowledge.benchmark import (
    TaskDraft,
    _lock_connection,
    _open_proposal,
    _proposal,
    _suite,
    _timestamp,
    register_task,
)
from weora.modules.knowledge.benchmark_models import (
    AssetEdit,
    KnowledgeImprovementDelivery,
    KnowledgeImprovementRevision,
)
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.session_scoring import list_scorers
from weora.modules.knowledge.store import KnowledgeStore
from weora.modules.projects.service import ProjectService

MAX_IMPROVEMENT_TASKS = 50
MAX_IMPROVEMENT_CHANGES = 100
MAX_IMPROVEMENT_BYTES = 1_000_000
_ASSET_ROOTS = {"rules", "skills", "agents"}
_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_RESERVED = re.compile(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?")


@dataclass(frozen=True, slots=True)
class GeneratedTask:
    label_id: int
    name: str
    title: str
    prompt: str
    starting_commit: str
    success_criteria: str


@dataclass(frozen=True, slots=True)
class ImprovementDraft:
    operation_id: UUID
    scorer: str
    expected_proposal_id: UUID | None
    base_revision: int
    resource_repository_id: UUID
    base_commit: str
    reason: str
    tasks: list[GeneratedTask]
    changes: list[AssetEdit]


def writable_asset(path: str) -> bool:
    if path in {"AGENTS.md", "CLAUDE.md"}:
        return True
    parts = path.split("/")
    return len(parts) >= 2 and parts[0] in _ASSET_ROOTS and all(
        _SEGMENT.fullmatch(part) and not part.endswith(".") and not _RESERVED.fullmatch(part) for part in parts
    )


def _sha(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else None


def _combine(previous: list[AssetEdit], additions: list[AssetEdit]) -> list[AssetEdit]:
    edits = {item["path"]: item for item in previous}
    for addition in additions:
        path, before, content = addition["path"], addition["before_sha256"], addition["content"]
        if not writable_asset(path) or _sha(content) == before:
            raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_ASSET", "仅可提交有效的团队资源变更。", 422)
        prior = edits.get(path)
        if prior is not None:
            if _sha(prior["content"]) != before:
                raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_REVISION", "资源内容已变化，请重新生成差异。", 409)
            before = prior["before_sha256"]
        if _sha(content) == before:
            edits.pop(path, None)
        else:
            edits[path] = {"path": path, "before_sha256": before, "content": content}

    if len({path.casefold() for path in edits}) != len(edits):
        raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_ASSET", "团队资源路径在大小写不敏感的文件系统上冲突。", 422)
    paths = {path.casefold() for path, item in edits.items() if item["content"] is not None}
    if any(
        "/".join(path.split("/")[:depth]) in paths
        for path in paths for depth in range(1, len(path.split("/")))
    ):
        raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_ASSET", "团队资源路径不能同时是文件和目录。", 422)
    if not edits or len(edits) > MAX_IMPROVEMENT_CHANGES or sum(
        len((item["content"] or "").encode()) for item in edits.values()
    ) > MAX_IMPROVEMENT_BYTES:
        raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_ASSET", "提案须保留有效且在大小限制内的资源差异。", 422)
    return [edits[path] for path in sorted(edits)]


async def _latest(store: KnowledgeStore, proposal_id: UUID) -> KnowledgeImprovementRevision | None:
    row = KnowledgeImprovementRevision
    current: KnowledgeImprovementRevision | None = await store.db.scalar(select(row).where(
        row.proposal_id == proposal_id,
    ).order_by(row.revision.desc()).limit(1))
    return current


def _receipt(row: KnowledgeImprovementRevision) -> dict[str, object]:
    return {
        "proposal_id": row.proposal_id, "revision": row.revision, "resource_repository_id": row.resource_repository_id,
        "connection_version": row.connection_version, "base_commit": row.base_commit, "reason": row.reason,
        "task_ids": row.task_ids, "change_paths": [item["path"] for item in row.changes],
        "created_by": row.created_by, "created_at": _timestamp(row.created_at),
    }


async def latest_improvement(store: KnowledgeStore, path: str, identifier: UUID) -> dict[str, object]:
    store.require_reviewer()
    suite = await _suite(store, path, lock="read")
    assert suite is not None
    proposal = await _proposal(store, suite, identifier)
    latest = await _latest(store, proposal.id)
    if latest is None:
        return {"proposal_id": proposal.id, "revision": 0, "changes": []}
    return _receipt(latest) | {"changes": latest.changes}


async def recover_improvement(
    store: KnowledgeStore, path: str, scorer: str, operation: UUID,
) -> dict[str, object]:
    """由 PostgreSQL 续做已写入的修订，支持一次性 CI Runner 丢失本机回执。"""
    store.require_reviewer()
    suite = await _suite(store, path, lock="read")
    assert suite is not None
    model = KnowledgeImprovementRevision
    revision = await store.db.scalar(select(model).where(
        model.suite_id == suite.id, model.operation_id == operation,
    ))
    if revision is None:
        return {"status": "missing"}
    proposal = await _proposal(store, suite, revision.proposal_id)
    if proposal.scorer != scorer or revision.created_by != store.scope.user:
        raise KnowledgeError("KNOWLEDGE_BENCHMARK_OPERATION_CONFLICT", "原操作已用于其他改进。", 409)
    newest = await _latest(store, proposal.id)
    if newest is None or newest.revision != revision.revision:
        return _receipt(revision) | {"status": "superseded"}
    if proposal.status != "open":
        return _receipt(revision) | {"status": proposal.status}

    delivery = await store.db.get(KnowledgeImprovementDelivery, (revision.proposal_id, revision.revision))
    if delivery is not None and delivery.pr_number is not None:
        return _receipt(revision) | {
            "status": "delivered", "pr_number": delivery.pr_number, "head_commit": delivery.head_commit,
        }
    return _receipt(revision) | {"status": "pending_delivery"}


async def save_improvement(
    store: KnowledgeStore, projects: ProjectService, path: str, draft: ImprovementDraft,
) -> dict[str, object]:
    store.require_reviewer()
    suite = await _suite(store, path, lock="write")
    assert suite is not None
    model = KnowledgeImprovementRevision
    digest = hashlib.sha256(json.dumps(asdict(draft), sort_keys=True, default=str).encode()).hexdigest()
    prior = await store.db.scalar(select(model).where(
        model.suite_id == suite.id, model.operation_id == draft.operation_id,
    ))
    if prior is not None:
        if prior.created_by != store.scope.user or prior.request_digest != digest:
            raise KnowledgeError("KNOWLEDGE_BENCHMARK_OPERATION_CONFLICT", "该操作标识已用于其他改进。", 409)
        return _receipt(prior)

    scorers = await list_scorers(store, 1, [draft.scorer], with_prompt=False)
    if not scorers or scorers[0].is_adoption_template:
        raise KnowledgeError("KNOWLEDGE_SCORER_NOT_FOUND", "当前项目找不到所选评分标准。", 404)
    open_proposal = await _open_proposal(store, suite, scorers[0].id)
    current_id = open_proposal.id if open_proposal else None
    previous = await _latest(store, current_id) if current_id else None
    current_revision = previous.revision if previous else 0
    if previous is not None:
        delivery = await store.db.get(KnowledgeImprovementDelivery, (previous.proposal_id, previous.revision))
        if delivery is not None and delivery.pr_number is None:
            raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_DELIVERY", "须先恢复正在登记的远端交付。", 409)
    if current_id != draft.expected_proposal_id or current_revision != draft.base_revision:
        raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_REVISION", "开放提案已变化，请重新生成改进方案。", 409)
    if len(draft.tasks) < (1 if open_proposal else 2):
        raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_THRESHOLD", "当前失败数未达到改进启动条件。", 422)
    versions = {
        repository: await _lock_connection(store, projects, repository)
        for repository in sorted({suite.repository_id, draft.resource_repository_id})
    }
    version = versions[draft.resource_repository_id]
    if previous and (
        previous.resource_repository_id != draft.resource_repository_id
        or previous.connection_version != version or previous.base_commit != draft.base_commit
    ):
        raise KnowledgeError("KNOWLEDGE_IMPROVEMENT_BASE", "开放提案须沿用其固定的资源仓库版本和基准提交。", 409)
    changes = _combine(previous.changes if previous else [], draft.changes)
    task_ids = []
    for task in draft.tasks:
        saved = await register_task(store, projects, path, TaskDraft(
            operation_id=uuid5(draft.operation_id, str(task.label_id)), label_id=task.label_id, scorer=draft.scorer,
            name=task.name, title=task.title, prompt=task.prompt, starting_commit=task.starting_commit,
            success_criteria=task.success_criteria, proposed=True, source=None,
        ), generated_batch=True)
        task_ids.append(str(saved["id"]))
        await store.db.flush()
    proposal = await _open_proposal(store, suite, scorers[0].id)
    assert proposal is not None
    row = model(
        proposal_id=proposal.id, revision=current_revision + 1, suite_id=suite.id, operation_id=draft.operation_id,
        request_digest=digest, resource_repository_id=draft.resource_repository_id, connection_version=version,
        base_commit=draft.base_commit, reason=draft.reason, changes=changes, task_ids=task_ids,
        created_by=store.scope.user, created_at=datetime.now(UTC),
    )
    store.db.add(row)
    return _receipt(row)
