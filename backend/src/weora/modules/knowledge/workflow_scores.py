"""导入 TeamAI 发布的工作流评分，只保留报表所需的证据字段。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import KnowledgeWorkflowScore
from weora.modules.knowledge.store import KnowledgeStore

MAX_IMPORT_BYTES = 1024 * 1024
MAX_IMPORT_LINES = 2000
STAGES = ("check", "ci", "protected")

LabelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256,
                                             pattern=r"^[^\x00-\x1f\x7f]+$")]
Commit = Annotated[str, StringConstraints(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")]
JudgeIdentity = tuple[str, str | None, str | None, str | None]


def score_digest(*parts: object) -> str:
    canonical = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TeamAiRecord(BaseModel):
    # TeamAI 的日志可向前扩展；提示词、推理、补丁以及其他报表外字段都不会进入数据库。
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class WorkflowSubject(TeamAiRecord):
    run_id: LabelName
    step: LabelName
    attempt: Annotated[int, Field(strict=True, gt=0, le=1_000_000)] = Field(alias="pass")


class WorkflowRubric(TeamAiRecord):
    source_hash: LabelName
    team_revision: Commit | None = None


class ObservedModel(TeamAiRecord):
    reported: LabelName | None = None
    requested: LabelName | None = None

    def resolved(self) -> tuple[str | None, str | None]:
        if self.reported is not None:
            return self.reported, "reported"
        if self.requested is not None:
            return self.requested, "requested"
        return None, None


class WorkflowJudge(TeamAiRecord):
    kind: Literal["model", "human", "stage"]
    tool: LabelName | None = None
    model: ObservedModel | None = None

    @model_validator(mode="after")
    def check_model_judge(self) -> "WorkflowJudge":
        if self.kind == "model" and self.tool is None:
            raise ValueError("模型评审缺少工具")
        return self


class DeliverySelection(TeamAiRecord):
    step_role: LabelName | None = None
    primary_role: LabelName | None = None
    projects: list[LabelName] = Field(default_factory=list, max_length=120)
    subscribed_tags: list[LabelName] = Field(default_factory=list, max_length=120)


class ObservedTeam(TeamAiRecord):
    revision: Commit
    local_changes: bool = False
    selection: DeliverySelection | None = None


class NamedTool(TeamAiRecord):
    name: LabelName


class WorkflowAgent(TeamAiRecord):
    tool: NamedTool | None = None
    model: ObservedModel | None = None
    definition: NamedTool | None = None
    team: ObservedTeam | None = None
    fingerprint: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None


class UsedResources(TeamAiRecord):
    skills: list[LabelName] = Field(max_length=120)


class WorkflowScoreLabel(TeamAiRecord):
    source: Literal["workflow"]
    subject: WorkflowSubject
    rubric: WorkflowRubric
    scorer: LabelName
    label: LabelName
    score: Annotated[float, Field(strict=True, allow_inf_nan=False)]
    passed: Annotated[bool, Field(strict=True)]
    judge: WorkflowJudge
    scored_at: AwareDatetime
    run_mode: Literal["attended", "unattended"] | None = None
    decided_by: Literal["check", "ci", "protected", "judge"] | None = None
    agent: WorkflowAgent | None = None
    used: UsedResources | None = None

    @model_validator(mode="after")
    def check_stage(self) -> "WorkflowScoreLabel":
        if self.judge.kind == "stage" and self.decided_by not in STAGES:
            raise ValueError("阶段判定必须带判定阶段")
        # 旧版在阶段失败标签中携带了原计划使用的模型。归一后与新版阶段判定等价。
        if self.decided_by in STAGES:
            self.judge = WorkflowJudge(kind="stage")
        self.scored_at = self.scored_at.astimezone(UTC)
        return self

    def judge_identity(self) -> JudgeIdentity:
        model, source = self.judge.model.resolved() if self.judge.model is not None else (None, None)
        if self.judge.kind == "model":
            return "model", self.judge.tool, model, source
        return self.judge.kind, None, None, None

    def logical_key(self) -> str:
        return score_digest(
            self.scorer, self.rubric.source_hash, self.subject.model_dump(), self.judge_identity(),
        )


class WorkflowScoreImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonl: str = Field(min_length=1, max_length=MAX_IMPORT_BYTES)


def parse_workflow_scores(
    content: str, now: datetime, *, max_bytes: int = MAX_IMPORT_BYTES, max_lines: int = MAX_IMPORT_LINES,
    allow_empty: bool = False,
) -> tuple[list[WorkflowScoreLabel], int]:
    lines = content.lstrip("\ufeff").splitlines()
    if len(content.encode("utf-8")) > max_bytes or len(lines) > max_lines:
        message = f"评分文件最多 {max_bytes // (1024 * 1024)} MiB、{max_lines} 行。"
        raise KnowledgeError("KNOWLEDGE_WORKFLOW_SCORE_SIZE", message, 422)
    labels, skipped = [], 0
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict) and row.get("source") == "session":
                skipped += 1
                continue
            label = WorkflowScoreLabel.model_validate(row)
            if label.scored_at > now + timedelta(minutes=5):
                raise ValueError("评分时间超前")
            labels.append(label)
        except (ValueError, RecursionError):
            message = f"第 {line_no} 行不是可导入的工作流评分记录，请检查格式和评分时间。"
            raise KnowledgeError("KNOWLEDGE_WORKFLOW_SCORE_FORMAT", message, 422) from None
    if not labels and not allow_empty:
        raise KnowledgeError("KNOWLEDGE_WORKFLOW_SCORE_EMPTY", "文件没有可导入的工作流评分标签。", 422)
    return labels, skipped


async def import_workflow_scores(store: KnowledgeStore, payload: WorkflowScoreImport) -> dict[str, int]:
    store.require_reviewer()
    scope, db = store.scope, store.db
    if scope.device_id is not None:
        raise KnowledgeError("KNOWLEDGE_FORBIDDEN", "团队评分需要管理员本人导入。", 403)
    now = datetime.now(UTC)
    labels, skipped = parse_workflow_scores(payload.jsonl, now)
    records: dict[str, dict[str, object]] = {}
    for label in labels:
        key = label.logical_key()
        ident = score_digest(str(scope.workspace), str(scope.project), key, label.scored_at.isoformat())
        record = {
            "id": ident, "workspace_id": scope.workspace, "project_id": scope.project,
            "label_key": key, "scored_at": label.scored_at, "imported_by": scope.user,
            "imported_at": now, "payload": label.model_dump(mode="json"),
        }
        if ident in records and records[ident]["payload"] != record["payload"]:
            raise _conflicting_score()
        records[ident] = record

    model = KnowledgeWorkflowScore
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    ids = sorted(records)
    recorded = 0
    # 并发导入按相同键顺序获取冲突锁。批次只提交一次，后段冲突会整批回滚。
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        statement = insert(model).values([records[key] for key in batch]).on_conflict_do_nothing(
            index_elements=[model.id],
        ).returning(model.id)
        inserted = set((await db.scalars(statement)).all())
        recorded += len(inserted)
        existing = set(batch) - inserted
        if existing:
            rows = (await db.execute(select(model.id, model.payload).where(model.id.in_(existing)))).all()
            if len(rows) != len(existing) or any(records[row.id]["payload"] != row.payload for row in rows):
                raise _conflicting_score()
    return {"recorded": recorded, "unchanged": len(labels) - recorded, "skipped_sessions": skipped}


def _conflicting_score() -> KnowledgeError:
    return KnowledgeError("KNOWLEDGE_WORKFLOW_SCORE_CONFLICT", "同一评分时间的标签已有不同内容。", 409)
