"""在工作空间内读写知识工作流的不可变版本。"""

from datetime import UTC, datetime
from typing import get_args

from sqlalchemy import select

from weora.modules.identity.infrastructure.unit_of_work import SqlAlchemyIdentityStore
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import KnowledgeWorkflowVersion
from weora.modules.knowledge.store import KnowledgeStore, conflict, missing
from weora.modules.knowledge.workflow_definition import (
    KnowledgeWorkflowDefinition,
    WorkflowName,
    default_workflow,
)


async def latest(store: KnowledgeStore, name: WorkflowName) -> KnowledgeWorkflowVersion | None:
    rows = await store.db.scalars(
        select(KnowledgeWorkflowVersion)
        .where(
            KnowledgeWorkflowVersion.workspace_id == store.scope.workspace,
            KnowledgeWorkflowVersion.name == name,
        )
        .order_by(KnowledgeWorkflowVersion.revision.desc())
        .limit(1)
    )
    return rows.first()


def view(name: WorkflowName, row: KnowledgeWorkflowVersion | None) -> dict[str, object]:
    if row is not None and row.definition is not None:
        definition = KnowledgeWorkflowDefinition.model_validate(row.definition)
    else:
        definition = default_workflow(name)
    return {
        "revision": row.revision if row else 0,
        "overridden": row is not None and row.definition is not None,
        "definition": definition.model_dump(mode="json", by_alias=True, exclude_none=True),
    }


async def configure(
    store: KnowledgeStore,
    name: WorkflowName,
    *,
    base_revision: int,
    definition: KnowledgeWorkflowDefinition | None,
) -> dict[str, object]:
    store.require_reviewer()
    if store.scope.project is not None:
        raise KnowledgeError("KNOWLEDGE_INVALID_SCOPE", "知识工作流覆盖在工作空间级别配置。", 422)
    if definition is not None and definition.name != name:
        raise KnowledgeError("KNOWLEDGE_WORKFLOW_NAME", "定义名称应与覆盖目标一致。", 422)
    # 工作空间锁同时保护首次覆盖与后续恢复。不同的并发修改不能悄悄覆盖彼此。
    if not await SqlAlchemyIdentityStore(store.db).lock_workspace(store.scope.workspace):
        raise missing()
    current = await latest(store, name)
    if definition is not None and definition.alias:
        for other in get_args(WorkflowName):
            if other == name:
                continue
            other_version = await latest(store, other)
            other_definition = other_version.definition if other_version else None
            if definition.alias == other or other_definition and definition.alias == other_definition.get("alias"):
                raise conflict("这个别名已被另一个知识工作流使用。")
    revision = current.revision if current else 0
    desired = definition.model_dump(mode="json", by_alias=True, exclude_none=True) if definition else None
    if desired == (current.definition if current else None):
        return view(name, current)
    if revision != base_revision:
        raise conflict("知识工作流版本已变化，请重新读取后再提交。")
    next_version = KnowledgeWorkflowVersion(
        workspace_id=store.scope.workspace,
        name=name,
        revision=revision + 1,
        definition=desired,
        updated_by=store.scope.user,
        updated_at=datetime.now(UTC),
    )
    store.db.add(next_version)
    await store.db.flush()
    return view(name, next_version)
