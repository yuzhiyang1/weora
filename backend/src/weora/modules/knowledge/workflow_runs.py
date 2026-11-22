"""执行冻结的知识分享计划，把审批和实际发布放在同一事务。"""

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from weora.modules.collaboration.infrastructure.agent_mail import AgentDevice
from weora.modules.identity.infrastructure.unit_of_work import SqlAlchemyIdentityStore
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import (
    KnowledgeSessionLabel,
    KnowledgeSharedSession,
    KnowledgeSharedSessionRevision,
    KnowledgeWorkflowDecision,
    KnowledgeWorkflowRun,
)
from weora.modules.knowledge.namespace_policy import Directory
from weora.modules.knowledge.session_keys import score_source_key
from weora.modules.knowledge.store import Contribution, KnowledgeStore, conflict, missing
from weora.modules.knowledge.workflow_definition import (
    KnowledgeWorkflowDefinition,
    WorkflowApproval,
    WorkflowName,
    embed_session_workflow,
)
from weora.modules.knowledge.workflow_versions import latest, view

LAUNCH = "_launch"
Decision = Literal["approve", "reject"]
Actor = Literal["person", "device", "automatic"]
Tag = Annotated[str, Field(min_length=1, max_length=60, pattern=r"^[^\x00]+$")]
ScorerName = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[^\x00]+$")]
TEMPLATE = re.compile(
    r"\{\{\s*(inputs\.(?:session|source|publish|scorers)|"
    r"length\(steps\.[a-z0-9-]+\.outputs\.learnings\))\s*\}\}"
)


class WorkflowStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    runtime: Literal["codex", "claude-code"]
    session: UUID
    hitl: Literal["recommend", "person", "auto"] = "recommend"
    unattended: bool = False
    publish: bool = False
    scorers: list[ScorerName] = Field(default_factory=list, max_length=20)

    @field_validator("scorers")
    @classmethod
    def distinct_scorers(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("同一个评分标准只能选择一次。")
        return values


class WorkflowDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00]+$")
    body: str = Field(min_length=1, max_length=64_000, pattern=r"^[^\x00]+$")
    tags: list[Tag] = Field(default_factory=list, max_length=24)
    namespace: Directory | None = None

    @field_validator("title", "body")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("草稿标题和正文不能为空。")
        return value


def run_view(run: KnowledgeWorkflowRun) -> dict[str, object]:
    definition = KnowledgeWorkflowDefinition.model_validate(run.definition)
    dependencies = definition.dependencies()
    items: list[dict[str, object]] = []
    for step in definition.steps:
        state = run.outcomes.get(step.id, "waiting")
        if state == "waiting" and run.launch == "approved" and all(
            run.outcomes.get(dependency) == "success" for dependency in dependencies[step.id]
        ):
            state = "ready"
        rendered = step.model_dump(mode="json", exclude_none=True)
        if step.prompt is not None:
            rendered["prompt"] = render(step.prompt, run, definition)
        if isinstance(step.approve, WorkflowApproval):
            rendered["approve"] = {
                "message": render(step.approve.message, run, definition), "required": step.approve.required,
            }
        items.append(rendered | {"state": state, "needs": dependencies[step.id]})
    return {
        "id": str(run.id),
        "name": run.workflow_name,
        "revision": run.workflow_revision,
        "session_workflow_revision": run.session_workflow_revision,
        "project": str(run.project_id) if run.project_id else None,
        "device_name": run.device_name,
        "launch": run.launch,
        "parameters": run.parameters,
        "steps": items,
        "drafts": run.drafts,
        "published": run.published,
        "session_result": run.session_result,
    }


def render(template: str, run: KnowledgeWorkflowRun, definition: KnowledgeWorkflowDefinition) -> str:
    """替换内置输入及草稿数量，团队文案不会执行任意表达式。"""
    inputs = {
        "inputs.session": run.parameters["session"],
        "inputs.source": run.parameters["runtime"],
        "inputs.publish": run.parameters["publish"],
        "inputs.scorers": run.parameters["scorers"],
    }
    if run.drafts is not None:
        inputs.update({
            f"length(steps.{step.id}.outputs.learnings)": len(run.drafts)
            for step in definition.steps if step.prompt is not None
        })

    def substitute(match: re.Match[str]) -> str:
        value = inputs.get(match[1])
        if value is None:
            return match[0]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    return TEMPLATE.sub(substitute, template)


async def start(
    store: KnowledgeStore, name: WorkflowName, request: WorkflowStart,
) -> dict[str, object]:
    scope = store.scope
    # 发起与设置发布共用工作空间锁，重试不会切换版本或重复建运行。
    if not await SqlAlchemyIdentityStore(store.db).lock_workspace(scope.workspace):
        raise missing()
    parameters = request.model_dump(mode="json", exclude={"operation_id"})
    existing = await store.db.scalar(select(KnowledgeWorkflowRun).where(
        KnowledgeWorkflowRun.workspace_id == scope.workspace,
        KnowledgeWorkflowRun.created_by == scope.user,
        KnowledgeWorkflowRun.operation_id == request.operation_id,
    ))
    if existing is not None:
        if (
            existing.project_id != scope.project or existing.device_id != scope.device_id
            or existing.workflow_name != name or existing.parameters != parameters
        ):
            raise conflict("同一个工作流请求的发起身份、项目或参数已经变化。")
        return run_view(existing)

    version = await latest(store, name)
    raw = view(name, version)["definition"]
    definition = KnowledgeWorkflowDefinition.model_validate(raw)
    device_launch = scope.device_id is not None
    if device_launch and (definition.invoke == "deny" or definition.invoke == "ask" and request.unattended):
        raise KnowledgeError("KNOWLEDGE_WORKFLOW_INVOKE_DENIED", "当前工作流不允许设备以此模式发起。", 403)
    child_revision = None
    if name == "teamai-share-learnings" and (request.publish or request.scorers) and any(
        step.uses == "weora.share-session" for step in definition.steps
    ):
        child = await latest(store, "teamai-share-session")
        child_revision = child.revision if child else 0
        if child is not None and child.definition is not None:
            try:
                definition = embed_session_workflow(
                    definition, KnowledgeWorkflowDefinition.model_validate(child.definition),
                )
            except ValidationError as exc:
                raise KnowledgeError("KNOWLEDGE_WORKFLOW_CHILD", "合并父子工作流后超出步骤范围。", 422) from exc
    device = await store.db.get(AgentDevice, scope.device_id) if scope.device_id else None
    run = KnowledgeWorkflowRun(
        id=uuid4(), workspace_id=scope.workspace, project_id=scope.project, created_by=scope.user,
        device_id=scope.device_id, device_name=device.name if device else None,
        operation_id=request.operation_id, workflow_name=name,
        workflow_revision=version.revision if version else 0,
        session_workflow_revision=child_revision,
        definition=definition.model_dump(mode="json", by_alias=True, exclude_none=True), parameters=parameters,
        outcomes={}, launch="pending" if device_launch and definition.invoke == "ask" else "approved",
        created_at=datetime.now(UTC),
    )
    store.db.add(run)
    await store.db.flush()
    await advance(store, run)
    return run_view(run)


async def find(store: KnowledgeStore, run_id: UUID, *, lock: bool = False) -> KnowledgeWorkflowRun:
    scope = store.scope
    query = select(KnowledgeWorkflowRun).where(
        KnowledgeWorkflowRun.id == run_id, KnowledgeWorkflowRun.workspace_id == scope.workspace,
        KnowledgeWorkflowRun.project_id == scope.project, KnowledgeWorkflowRun.created_by == scope.user,
    )
    run = await store.db.scalar(query.with_for_update() if lock else query)
    if run is None or scope.device_id is not None and scope.device_id != run.device_id:
        raise missing()
    return run


def audit(
    store: KnowledgeStore, run: KnowledgeWorkflowRun, step: str, decision: Decision, actor: Actor, reason: str,
) -> None:
    store.db.add(KnowledgeWorkflowDecision(
        run_id=run.id, step=step, decision=decision, reason=reason, actor_kind=actor, actor_id=store.scope.user,
        device_id=store.scope.device_id if actor == "device" else None, decided_at=datetime.now(UTC),
    ))


def ready(run: KnowledgeWorkflowRun, needs: list[str]) -> bool:
    return run.launch == "approved" and all(run.outcomes.get(dependency) == "success" for dependency in needs)


async def decide(
    store: KnowledgeStore, run_id: UUID, step: str, decision: Decision, reason: str,
) -> dict[str, object]:
    run = await find(store, run_id, lock=True)
    definition = KnowledgeWorkflowDefinition.model_validate(run.definition)
    gate = next((item.approve for item in definition.steps if item.id == step), None)
    if step != LAUNCH and not isinstance(gate, WorkflowApproval):
        raise missing()
    browser = store.scope.device_id is None
    human_required = (
        step == LAUNCH or run.parameters.get("hitl") == "person"
        or isinstance(gate, WorkflowApproval) and gate.required
    )
    if not browser and human_required:
        raise KnowledgeError("KNOWLEDGE_WORKFLOW_HUMAN_REQUIRED", "这项审批需要本人登录后确认。", 403)
    if not browser and not reason:
        raise KnowledgeError("KNOWLEDGE_WORKFLOW_REASON", "设备代答时需要说明审批理由。", 422)
    old = await store.db.get(KnowledgeWorkflowDecision, (run.id, step))
    if old is not None:
        if old.decision != decision or old.reason != reason:
            raise conflict("该工作流审批已有决定。")
        return run_view(run)
    if step == LAUNCH:
        if run.launch != "pending":
            raise conflict("当前运行没有待确认的发起申请。")
        run.launch = "approved" if decision == "approve" else "rejected"
    else:
        if step in run.outcomes or not ready(run, definition.dependencies()[step]):
            raise conflict("当前步骤尚未进入审批。")
        run.outcomes = run.outcomes | {step: "success" if decision == "approve" else "rejected"}
    audit(store, run, step, decision, "person" if browser else "device", reason)
    await advance(store, run)
    return run_view(run)


async def draft(store: KnowledgeStore, run_id: UUID, drafts: list[WorkflowDraft]) -> dict[str, object]:
    run = await find(store, run_id, lock=True)
    definition = KnowledgeWorkflowDefinition.model_validate(run.definition)
    step = next((item for item in definition.steps if item.prompt is not None), None)
    if step is None:
        raise missing()
    if sum(len(item.body.encode()) for item in drafts) > 800_000:
        raise KnowledgeError("KNOWLEDGE_WORKFLOW_DRAFT_SIZE", "草稿正文超过批次大小限制。", 422)
    snapshot = [item.model_dump(mode="json") for item in drafts]
    if run.drafts is not None:
        if run.drafts != snapshot:
            raise conflict("草稿已经定版，请发起新运行以修改草稿。")
        return run_view(run)
    if not ready(run, definition.dependencies()[step.id]):
        raise conflict("当前运行尚未进入草拟。")
    run.drafts = snapshot
    run.outcomes = run.outcomes | {step.id: "success" if drafts else "skipped"}
    await advance(store, run)
    return run_view(run)


async def finish_session(
    store: KnowledgeStore, run_id: UUID, archive_id: UUID, revision: int,
) -> dict[str, object]:
    """只接受本次选定会话的归档和实际评分收据，以便流程可以从另一回合恢复。"""
    run = await find(store, run_id, lock=True)
    detail = {"archive_id": str(archive_id), "revision": revision}
    if run.session_result is not None:
        if run.session_result != detail:
            raise conflict("该运行已经绑定另一份归档收据。")
        return run_view(run)
    definition = KnowledgeWorkflowDefinition.model_validate(run.definition)
    step = next((item for item in definition.steps if item.uses == "weora.share-session"), None)
    if step is None or step.id in run.outcomes or not ready(run, definition.dependencies()[step.id]):
        raise conflict("当前运行没有待处理的会话分享步骤。")
    parameters = WorkflowStart.model_validate(run.parameters | {"operation_id": run.operation_id})
    archive = await store.db.scalar(
        select(KnowledgeSharedSession)
        .join(KnowledgeSharedSessionRevision, KnowledgeSharedSessionRevision.archive_id == KnowledgeSharedSession.id)
        .where(
            KnowledgeSharedSession.id == archive_id,
            KnowledgeSharedSession.workspace_id == run.workspace_id,
            KnowledgeSharedSession.project_id == run.project_id,
            KnowledgeSharedSession.author_id == run.created_by,
            KnowledgeSharedSession.score_source_key == score_source_key(parameters.runtime, parameters.session),
            KnowledgeSharedSessionRevision.revision == revision,
        )
    )
    if archive is None or parameters.publish and archive.score_only:
        raise conflict("会话归档与当前运行选择的发布范围不一致。")
    expected = {uuid5(run.id, name) for name in parameters.scorers}
    if expected:
        observed = set(await store.db.scalars(select(KnowledgeSessionLabel.operation_id).where(
            KnowledgeSessionLabel.archive_id == archive_id,
            KnowledgeSessionLabel.archive_revision == revision,
            KnowledgeSessionLabel.judge_id == run.created_by,
            KnowledgeSessionLabel.operation_id.in_(expected),
        )))
        if observed != expected:
            raise conflict("当前会话还有未完成的评分。")
    run.session_result = detail
    run.outcomes = run.outcomes | {step.id: "success"}
    await advance(store, run)
    return run_view(run)


async def advance(store: KnowledgeStore, run: KnowledgeWorkflowRun) -> None:
    """只自动处理已有充分证据的步骤。更改定义或重跑设备不会绕过等待中的审批。"""
    if run.launch != "approved":
        return
    definition = KnowledgeWorkflowDefinition.model_validate(run.definition)
    dependencies = definition.dependencies()
    while True:
        previous = run.outcomes
        for step in definition.steps:
            if step.id in run.outcomes:
                continue
            needs = dependencies[step.id]
            if any(run.outcomes.get(dependency) in {"rejected", "skipped"} for dependency in needs):
                outcome = "skipped"
            elif not ready(run, needs):
                continue
            elif isinstance(step.approve, WorkflowApproval) and (
                not step.approve.required and run.parameters.get("hitl") == "auto"
            ):
                audit(store, run, step.id, "approve", "automatic", "发起时选择自动批准可选审批")
                outcome = "success"
            elif step.uses == "weora.contribute-learnings":
                await publish(store, run)
                outcome = "success"
            elif step.uses == "weora.share-session" and not (
                run.parameters.get("publish") or run.parameters.get("scorers")
            ):
                outcome = "skipped"
            else:
                continue
            run.outcomes = run.outcomes | {step.id: outcome}
        if run.outcomes == previous:
            return


async def publish(store: KnowledgeStore, run: KnowledgeWorkflowRun) -> None:
    assert run.drafts
    values = []
    for index, raw in enumerate(run.drafts):
        item = WorkflowDraft.model_validate(raw)
        operation = uuid5(run.id, str(index))
        directory = f"{item.namespace}/" if item.namespace else ""
        values.append(Contribution(
            operation, "learning", item.title.strip(), item.body, list(dict.fromkeys(item.tags)),
            {
                "type": "session",
                "ref": f"{run.parameters['runtime']}:{run.parameters['session']}",
                "resource_path": f"learnings/{directory}{operation}",
            },
            [],
        ))
    entries = await store.import_batch(values, publish_reviewed=False, request_id=str(run.id))
    # 只保存发布回执，正文仍从知识版本读取。
    run.published = [{"id": item["id"], "revision": item["revision"]} for item in entries]
