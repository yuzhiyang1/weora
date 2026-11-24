"""项目知识 HTTP 入口；控制台和 CLI 复用同一授权与写入契约。"""

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal, Self, cast
from uuid import UUID, uuid4

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.types import AwareDatetime

from weora.modules.collaboration.application.agent_mail import AgentMailError
from weora.modules.collaboration.infrastructure import agent_sessions
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.infrastructure.unit_of_work import SqlAlchemyIdentityStore
from weora.modules.knowledge.activity import ConversationTokens, record_session
from weora.modules.knowledge.adoption_rubric import ADOPTION_STANDARD
from weora.modules.knowledge.archive_policy import archive_policies, automatic_archive_policy, set_archive_policy
from weora.modules.knowledge.benchmark import (
    BENCHMARK_NAME_PATTERN,
    DEFAULT_SUITE,
    SUITE_PATH_PATTERN,
    SuiteConfig,
    TaskDraft,
    read_suite,
    register_suite,
    register_task,
    settle_proposal,
    suite_coverage,
)
from weora.modules.knowledge.benchmark_models import AssetEdit, BenchmarkExecutionProfile, BenchmarkTool, TrialOutcome
from weora.modules.knowledge.benchmark_reports import benchmark_dashboard
from weora.modules.knowledge.benchmark_runs import (
    MAX_TRIALS,
    BenchmarkPlanInput,
    TrialResult,
    bind_execution,
    execution_inputs,
    plan_run,
    record_results,
    report_run,
)
from weora.modules.knowledge.ci import CiMerge, CiProposal, CiReview
from weora.modules.knowledge.code_preview import MAX_REVIEW_PATHS
from weora.modules.knowledge.codebase import CodebaseIndex
from weora.modules.knowledge.domain import (
    DEFAULT_PRUNE_THRESHOLD,
    ArchiveAction,
    ArchiveRunMode,
    KnowledgeError,
    KnowledgeKind,
    PlanReminderChange,
    PlanReminderLanguage,
    PlanReminderSettings,
    PromotionKind,
    RecallDepth,
    RelationKind,
    ScoringIsolation,
    SessionContextPolicy,
)
from weora.modules.knowledge.feedback_projection import PendingFeedback
from weora.modules.knowledge.improvement_checks import CheckAttempt, ReviewEvent, check_result, merge_check, plan_check
from weora.modules.knowledge.improvement_delivery import (
    Prefix,
    Reservation,
    confirm_delivery,
    delivery_spec,
    reserve_delivery,
)
from weora.modules.knowledge.improvement_plan import regression_candidates
from weora.modules.knowledge.improvement_proposals import (
    MAX_IMPROVEMENT_CHANGES,
    MAX_IMPROVEMENT_TASKS,
    GeneratedTask,
    ImprovementDraft,
    latest_improvement,
    recover_improvement,
    save_improvement,
    writable_asset,
)
from weora.modules.knowledge.improvement_reports import self_improvement_dashboard
from weora.modules.knowledge.local_source import MAX_LOCAL_BODY_BYTES, LocalCodeSnapshot
from weora.modules.knowledge.models import ScorerFile, ScorerLabel, TranscriptTurn
from weora.modules.knowledge.named_scoring import (
    ADOPTION_NAME_PREFIX,
    GENERATED_NAME_PREFIXES,
    NamedArchive,
    NamedLabel,
    NamedLocal,
    NamedSubject,
    named_plan,
    read_named_call,
    settle_named,
)
from weora.modules.knowledge.namespace_policy import (
    Directory,
    NamespaceUpdate,
    current_manifest,
    hidden_wiki_ids,
    resolve_namespaces,
    select_namespaces,
    write_manifest,
)
from weora.modules.knowledge.queued_recall import QueuedLearning
from weora.modules.knowledge.recall_check import empty_check
from weora.modules.knowledge.report import digest as knowledge_digest
from weora.modules.knowledge.report import health as knowledge_health
from weora.modules.knowledge.report import improvement_dashboard
from weora.modules.knowledge.resource_paths import normalize_resource_path
from weora.modules.knowledge.routing_evidence import (
    MAX_ROUTING_SKILLS,
    DeliveryCapture,
    DeliveryObservation,
    freeze_deliveries,
)
from weora.modules.knowledge.routing_rubric import ROUTING_NAME_PREFIX, ROUTING_STANDARD
from weora.modules.knowledge.rule_paths import normalize_rule_paths
from weora.modules.knowledge.scorer_files import MAX_SCORER_FOLDER_BYTES, normalize_scorer_files
from weora.modules.knowledge.scoring_attempts import (
    ArchivedOfferSubject,
    NativeOfferSubject,
    batch_reply,
    claim_scoring,
    pin_verified_claim,
    require_local_scoring_policy,
    require_offer_policy,
    scoring_offer,
    settle_adoption,
    settle_adoption_group,
    settle_group,
    settle_routing,
    settle_scoring,
)
from weora.modules.knowledge.scoring_policy import (
    MODES,
    DirectoryScoring,
    directory_scoring,
    execution_plan,
    require_compatible_scoring,
    require_judge_tools,
    require_verified,
    scoring_policies,
    scoring_policy,
    set_directory_scoring,
    set_scoring_policy,
)
from weora.modules.knowledge.scoring_report import scoring_failures
from weora.modules.knowledge.session_origin import GitStartInput, remember_start
from weora.modules.knowledge.session_outcomes import finish_session
from weora.modules.knowledge.session_scoring import (
    JudgeError,
    ScoreDriver,
    ScorerDefinition,
    ScorerTool,
    SessionJudgment,
    adoption_template,
    list_labels,
    publish_scorer,
    reset_generated_template,
    rubric_plan,
    score_plan,
    scored_local_sessions,
    scorer_catalog,
    write_label,
)
from weora.modules.knowledge.session_trends import TrendInput
from weora.modules.knowledge.store import Contribution, KnowledgeStore, ProjectRecallSettings, Scope, missing
from weora.modules.knowledge.transcript_archive import (
    MAX_TRANSCRIPT_TURNS,
    MAX_TURN_TEXT,
    adoption_archive,
    list_transcripts,
    publish_transcript,
    read_skill_evidence,
    read_transcript,
    register_local_evidence,
    safe_skill_description,
    score_source_key,
)
from weora.modules.knowledge.workflow_activity import WorkflowKind, WorkflowOutcome, record_workflow
from weora.modules.knowledge.workflow_definition import KnowledgeWorkflowDefinition, WorkflowName
from weora.modules.knowledge.workflow_runs import Decision, WorkflowDraft, WorkflowStart, run_view
from weora.modules.knowledge.workflow_runs import decide as decide_workflow
from weora.modules.knowledge.workflow_runs import draft as draft_workflow
from weora.modules.knowledge.workflow_runs import find as find_workflow_run
from weora.modules.knowledge.workflow_runs import finish_session as finish_workflow_session
from weora.modules.knowledge.workflow_runs import start as start_workflow
from weora.modules.knowledge.workflow_score_report import GroupBy as WorkflowScoreGroup
from weora.modules.knowledge.workflow_score_report import workflow_score_dashboard
from weora.modules.knowledge.workflow_score_sync import configure_sync, sync_status
from weora.modules.knowledge.workflow_scores import WorkflowScoreImport, import_workflow_scores
from weora.modules.knowledge.workflow_versions import configure as configure_workflow
from weora.modules.knowledge.workflow_versions import latest as latest_workflow
from weora.modules.knowledge.workflow_versions import view as workflow_view
from weora.modules.projects.domain import ProjectActor, ProjectError, validate_repository_file_path
from weora.modules.projects.service import ProjectService
from weora.shared.api.request_id import get_request_id
from weora.shared.infrastructure.database import SessionFactory
from weora.shared.object_storage import ObjectStoragePort
from weora_code_graph.design import GraphTopic
from weora_code_graph.extractor import DEFAULT_INDEX_FILES, MAX_INDEX_FILES

router = APIRouter(prefix="/workspaces/{workspace_id}/knowledge", tags=["Knowledge"])
SessionKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60, pattern=r"^[^\x00]+$")]
ScorerName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")]
ScorerValue = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[^\x00]+$"),
]
EvidenceDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SuitePath = Annotated[str, StringConstraints(max_length=250, pattern=SUITE_PATH_PATTERN)]
BenchmarkName = Annotated[str, StringConstraints(max_length=100, pattern=BENCHMARK_NAME_PATTERN)]
CommitHash = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


class RegressionSuiteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    path: SuitePath = DEFAULT_SUITE
    repository_id: UUID
    description: str = Field(default="", max_length=1000, pattern=r"^[^\x00]*$")
    tools: list[BenchmarkTool] = Field(min_length=1, max_length=2)
    repetitions: int = Field(default=3, strict=True, ge=1, le=10)
    timeout_seconds: int = Field(strict=True, ge=1, le=604800)

    @field_validator("tools")
    @classmethod
    def unique_tools(cls, tools: list[BenchmarkTool]) -> list[BenchmarkTool]:
        if len(tools) != len(set(tools)):
            raise ValueError("同一工具只能在套件中出现一次。")
        return tools

    def config(self) -> SuiteConfig:
        return SuiteConfig(self.path, self.repository_id, self.description, self.tools,
                           self.repetitions, self.timeout_seconds)


class RegressionSessionSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["codex", "claude-code"]
    session_id: UUID = Field(alias="sessionId")


class RegressionTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation_id: UUID
    label_id: int = Field(strict=True, ge=1)
    scorer: ScorerName
    name: BenchmarkName
    title: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00]+$")
    prompt: str = Field(min_length=1, max_length=16000, pattern=r"^[^\x00]+$")
    starting_commit: CommitHash
    success_criteria: str = Field(min_length=1, max_length=8000, pattern=r"^[^\x00]+$")
    proposed: bool = True
    source: RegressionSessionSourceInput | None = None

    def draft(self) -> TaskDraft:
        source = (self.source.platform, self.source.session_id) if self.source else None
        return TaskDraft(self.operation_id, self.label_id, self.scorer, self.name, self.title, self.prompt,
                         self.starting_commit, self.success_criteria, self.proposed, source)


class RegressionSettlementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["merged", "closed"]


class ImprovementTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label_id: int = Field(strict=True, ge=1)
    name: BenchmarkName
    title: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00]+$")
    prompt: str = Field(min_length=1, max_length=16000, pattern=r"^[^\x00]+$")
    starting_commit: CommitHash
    success_criteria: str = Field(min_length=1, max_length=8000, pattern=r"^[^\x00]+$")

    def task(self) -> GeneratedTask:
        return GeneratedTask(
            self.label_id, self.name, self.title, self.prompt, self.starting_commit, self.success_criteria,
        )


class ImprovementAssetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=512)
    before_sha256: EvidenceDigest | None
    content: str | None = Field(max_length=64000, pattern=r"^[^\x00]*$")

    @field_validator("path")
    @classmethod
    def eligible_resource(cls, path: str) -> str:
        if not writable_asset(path):
            raise ValueError("路径须指向受支持的团队资源。")
        return path

    def edit(self) -> AssetEdit:
        return {"path": self.path, "before_sha256": self.before_sha256, "content": self.content}


class ImprovementDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    scorer: ScorerName
    expected_proposal_id: UUID | None = None
    base_revision: int = Field(strict=True, ge=0)
    resource_repository_id: UUID
    base_commit: CommitHash
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000,
                                             pattern=r"^[^\x00]+$")]
    tasks: list[ImprovementTaskInput] = Field(min_length=1, max_length=MAX_IMPROVEMENT_TASKS)
    changes: list[ImprovementAssetInput] = Field(min_length=1, max_length=MAX_IMPROVEMENT_CHANGES)

    @model_validator(mode="after")
    def distinct_items(self) -> "ImprovementDraftInput":
        if len(self.tasks) != len({task.label_id for task in self.tasks}) or len(self.tasks) != len({
            task.name for task in self.tasks
        }) or len(self.changes) != len({asset.path.casefold() for asset in self.changes}):
            raise ValueError("任务、评分标签或资源路径重复。")
        return self

    def draft(self) -> ImprovementDraft:
        return ImprovementDraft(
            self.operation_id, self.scorer, self.expected_proposal_id, self.base_revision,
            self.resource_repository_id, self.base_commit, self.reason,
            [task.task() for task in self.tasks], [change.edit() for change in self.changes],
        )


class ImprovementDeliveryReservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(strict=True, ge=1)
    head_commit: CommitHash
    prefix: Prefix


class ImprovementDeliveryConfirmationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(strict=True, ge=1)
    pr_number: int = Field(strict=True, ge=1, le=2147483647)
    benchmark_id: UUID | None = None


class ImprovementReviewEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: UUID
    repository: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.-]+/[a-z0-9_.-]+$", max_length=250)]
    base_branch: str = Field(min_length=1, max_length=120)
    head_commit: CommitHash
    pr_number: int = Field(strict=True, ge=1, le=2147483647)

    def event(self) -> ReviewEvent:
        return ReviewEvent(self.proposal_id, self.repository, self.base_branch, self.head_commit, self.pr_number)


class ImprovementCheckInput(ImprovementReviewEventInput):
    operation_id: UUID
    workflow_run: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,29}$")] | None = None
    attempt: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,29}$")] | None = None

    @model_validator(mode="after")
    def verify_attempt(self) -> Self:
        if (self.workflow_run is None) != (self.attempt is None):
            raise ValueError("工作流运行号和尝试序号必须同时提供。")
        return self


class BenchmarkRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    proposal_id: UUID | None = None
    resource_repository_id: UUID
    base_resources: CommitHash
    proposal_resources: CommitHash
    tasks: list[BenchmarkName] = Field(default_factory=list, max_length=MAX_TRIALS // 2)

    @field_validator("tasks")
    @classmethod
    def unique_tasks(cls, tasks: list[str]) -> list[str]:
        if len(set(tasks)) != len(tasks):
            raise ValueError("不能重复选择同一任务。")
        return sorted(tasks)

    def plan(self) -> BenchmarkPlanInput:
        return BenchmarkPlanInput(self.operation_id, self.proposal_id, self.resource_repository_id,
                                  self.base_resources, self.proposal_resources, tuple(self.tasks))


BenchmarkJudgeModel = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:@+\-]{0,159}$")]
BenchmarkNetwork = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")]
BenchmarkGateway = Annotated[str, StringConstraints(pattern=r"^http://[a-z0-9][a-z0-9_.-]{0,95}:[1-9][0-9]{0,4}$")]


class BenchmarkExecutionInput(BaseModel):
    """只接受可写入审计的固定镜像、内网地址和评判身份。"""

    model_config = ConfigDict(extra="forbid")
    image: Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
    runner_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    network: BenchmarkNetwork
    gateway: BenchmarkGateway | None = None
    judge_tool: BenchmarkTool
    judge_model: BenchmarkJudgeModel
    check_operation_id: UUID | None = None

    @model_validator(mode="after")
    def verify_gateway(self) -> Self:
        if (self.gateway is None) != (self.network == "none"):
            raise ValueError("内部网络和网关必须同时配置。")
        if self.gateway is not None and int(self.gateway.rsplit(":", 1)[-1]) > 65535:
            raise ValueError("网关端口无效。")
        return self

    def profile(self) -> BenchmarkExecutionProfile:
        return {
            "image": self.image, "runner_sha256": self.runner_sha256, "network": self.network, "gateway": self.gateway,
            "judge_tool": self.judge_tool, "judge_model": self.judge_model,
        }


class BenchmarkTrialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trial_id: UUID
    outcome: TrialOutcome
    judge_tool: BenchmarkTool | None = None
    judge_model: BenchmarkJudgeModel | None = None
    judge_effort: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")] | None = None
    execution_id: UUID | None = None

    @model_validator(mode="after")
    def verify_judge(self) -> "BenchmarkTrialInput":
        if self.outcome in ("passed", "failed"):
            if self.judge_tool is None or self.judge_model is None:
                raise ValueError("判定须记录评判工具和模型。")
        elif any(value is not None for value in (self.judge_tool, self.judge_model, self.judge_effort)):
            raise ValueError("未判定的试验不能附上评判身份。")
        return self

    def result(self) -> TrialResult:
        return TrialResult(
            self.trial_id, self.outcome, self.judge_tool, self.judge_model, self.judge_effort, self.execution_id,
        )


class BenchmarkResultsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[BenchmarkTrialInput] = Field(min_length=1, max_length=MAX_TRIALS)
    check_operation_id: UUID | None = None

    @field_validator("items")
    @classmethod
    def unique_trials(cls, items: list[BenchmarkTrialInput]) -> list[BenchmarkTrialInput]:
        if len({item.trial_id for item in items}) != len(items):
            raise ValueError("一次提交中不能重复判定同一试验。")
        return items


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    type: Literal["manual", "session", "run", "pull_request", "file", "wiki", "repository"]
    ref: str = Field(default="", max_length=1000, pattern=r"^[^\x00]*$")
    review_author: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[^\x00]+$")
    review_date: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[^\x00]+$")
    resource_path: str | None = Field(default=None, min_length=1, max_length=1000)
    namespace: Directory | None = None

    @field_validator("resource_path")
    @classmethod
    def relative_resource_path(cls, value: str | None) -> str | None:
        if value is not None and normalize_resource_path(value) is None:
            raise ValueError("知识资源路径必须是相对路径。")
        return value


class RelationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: RelationKind
    target: UUID


class ContributionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    kind: KnowledgeKind = "learning"
    title: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00]+$")
    body: str = Field(min_length=1, max_length=64000, pattern=r"^[^\x00]+$")
    authored_at: datetime | None = Field(default=None, strict=True)
    domain: Literal["technical", "ops", "support", "neutral"] | None = None
    tags: list[Tag] = Field(default_factory=list, max_length=24)
    source: SourceInput = Field(default_factory=lambda: SourceInput(type="manual"))
    relations: list[RelationInput] = Field(default_factory=list, max_length=30)
    related: JsonValue = None
    promoted_from: UUID | None = None
    base_revision: int | None = Field(default=None, ge=1)
    rule_paths: list[str] | None = None

    @field_validator("rule_paths", mode="before", json_schema_input_type=list[str] | str | None)
    @classmethod
    def rule_path_patterns(cls, value: object) -> list[str] | None:
        return normalize_rule_paths(value) if value is not None else None

    @field_validator("authored_at", mode="before", json_schema_input_type=str | None)
    @classmethod
    def authored_date(cls, value: object) -> datetime | None:
        """接收原文的 ISO 日期或时间。未标注时区时按 UTC 解释。"""
        if value is None:
            return None
        if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[Tt ])", value):
            try:
                parsed = datetime.fromisoformat(value)
                return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
            except (OverflowError, ValueError):
                pass
        raise ValueError("知识原文日期应为 ISO 日期或时间。")

    @model_validator(mode="after")
    def paths_apply_to_rules(self) -> "ContributionInput":
        if self.rule_paths is not None and self.kind != "rule":
            raise ValueError("适用路径只用于规则。")
        if self.source.namespace is not None and (self.kind != "learning" or self.source.resource_path is not None):
            raise ValueError("经验目录与显式团队路径只能选择一个。")
        return self

    @field_validator("title", "body")
    @classmethod
    def non_empty_markdown(cls, value: str) -> str:
        # 保留 Markdown 缩进和结尾空格，以免破坏代码块或换行。
        if not value.strip():
            raise ValueError("知识标题和正文不能为空。")
        return value

    def contribution(self) -> Contribution:
        links = dict.fromkeys((item.kind, item.target) for item in self.relations)
        source = self.source.model_dump(exclude_none=True)
        if self.domain is not None:
            source["domain"] = self.domain
        if namespace := self.source.namespace:
            # 路径由同一个离线操作标识生成，重放不会新建其他目录条目。
            source["resource_path"] = f"learnings/{namespace}/{self.operation_id}"
        return Contribution(
            self.operation_id, self.kind, self.title.strip(), self.body, list(dict.fromkeys(self.tags)),
            source,
            [{"kind": kind, "target": str(target)} for kind, target in links],
            self.promoted_from, self.base_revision, self.rule_paths, related=self.related, authored_at=self.authored_at,
        )


class PromotionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    source_revision: int = Field(ge=1)
    kind: PromotionKind
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500, pattern=r"^[^\x00]+$"),
    ]
    body: str = Field(min_length=1, max_length=64000, pattern=r"^[^\x00]+$")

    @field_validator("body")
    @classmethod
    def non_empty_body(cls, body: str) -> str:
        return ContributionInput.non_empty_markdown(body)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(ge=1)
    decision: Literal["accept", "reject"]
    reason: str = Field(default="", max_length=1000, pattern=r"^[^\x00]*$")


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: SessionKey
    positive: bool
    operation_id: UUID | None = None
    occurred_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def paired_delivery_fields(self) -> Self:
        if (self.operation_id is None) != (self.occurred_at is None):
            raise ValueError("反馈操作标识和发生时间需要同时提交。")
        return self


class IWikiSectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    section: Literal["business-api", "external-knowledge", "glossary"]
    body: str = Field(min_length=1, max_length=16000, pattern=r"^[^\x00]+$")


class IWikiSectionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    source: str = Field(pattern=r"^iwiki://[a-zA-Z0-9_-]{1,128}$")
    sections: list[IWikiSectionInput] = Field(min_length=1, max_length=3)
    require_review: bool = False

    @field_validator("sections")
    @classmethod
    def unique_sections(cls, values: list[IWikiSectionInput]) -> list[IWikiSectionInput]:
        if len({item.section for item in values}) != len(values):
            raise ValueError("同一导入不能包含重复章节。")
        return values


IWikiTerm = Annotated[str, StringConstraints(
    min_length=3, max_length=120, pattern=r"(?i)^(?:/api/[A-Za-z0-9/_-]+|[A-Za-z_][A-Za-z0-9_]+)$",
)]
IWikiTerms = Annotated[list[IWikiTerm], Field(min_length=1, max_length=80)]


class ImportInput(BaseModel):
    """接收已经过本地分类及确认的候选项，按批次落库。"""

    model_config = ConfigDict(extra="forbid")
    items: list[ContributionInput] = Field(min_length=1, max_length=200)
    publish_reviewed: bool = False
    iwiki_terms: dict[UUID, IWikiTerms] = Field(default_factory=dict, max_length=200)

    @model_validator(mode="after")
    def check_iwiki_sources(self) -> "ImportInput":
        sources = {item.operation_id: item.source for item in self.items}
        for operation in self.iwiki_terms:
            source = sources.get(operation)
            if (
                source is None or source.type != "wiki"
                or not re.fullmatch(r"https://iwiki\.woa\.com/p/[A-Za-z0-9_-]{1,128}", source.ref)
            ):
                raise ValueError("iWiki 术语必须关联本批次的原始页面。")
        return self


CommitSha = Annotated[str, StringConstraints(pattern=r"^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$")]
ReviewUrl = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class CiGraphPreviewInput(BaseModel):
    """仅允许在登记仓库上读取两个固定提交的指定文件。"""

    model_config = ConfigDict(extra="forbid")
    url: ReviewUrl
    base_commit: CommitSha
    head_commit: CommitSha
    paths: list[Annotated[str, StringConstraints(min_length=1, max_length=480)]] = Field(
        min_length=1, max_length=MAX_REVIEW_PATHS,
    )

    @field_validator("paths")
    @classmethod
    def source_paths(cls, paths: list[str]) -> list[str]:
        try:
            for path in paths:
                validate_repository_file_path(path)
        except ProjectError:
            raise ValueError("预览路径必须是仓库允许读取的相对文件路径。") from None
        return paths


class CiProposalInput(BaseModel):
    """CI 仅提交提炼稿。来源、仓库范围及发布身份由服务端确定。"""

    model_config = ConfigDict(extra="forbid")
    url: ReviewUrl
    head_commit: CommitSha
    title: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00]+$")
    body: str = Field(min_length=1, max_length=64000, pattern=r"^[^\x00]+$")
    tags: list[Tag] = Field(default_factory=list, max_length=24)
    review_author: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[^\x00]+$")

    @field_validator("title", "body")
    @classmethod
    def non_empty_markdown(cls, value: str) -> str:
        return ContributionInput.non_empty_markdown(value)


class CiMergeInput(BaseModel):
    """保存 CI 任务从来源读取的固定合并版本和独立否决信号。"""

    model_config = ConfigDict(extra="forbid")
    merge_commit: CommitSha
    merged_at: AwareDatetime
    rejected_learning: bool
    rejected_codebase: bool
    comment_lease_id: UUID | None = None


class CiCommentLeaseInput(BaseModel):
    """远程评论写入和合并读取使用同一个可续期的标识。"""

    model_config = ConfigDict(extra="forbid")
    lease_id: UUID
    release: bool = False


async def context(
    workspace_id: UUID, request: Request, project_id: UUID | None = None,
) -> AsyncIterator[KnowledgeStore]:
    """设备身份直接绑定工作空间，浏览器写入继续经会话 CSRF 校验。"""
    sessions = cast(SessionFactory, request.app.state.db_session_factory)
    async with sessions() as db, db.begin():
        try:
            token = request.headers.get("authorization")
            device_id = None
            if token:
                device, principal = await agent_sessions.authenticate(db, workspace_id, token)
                device_id = device.id
            elif request.method in {"GET", "HEAD"}:
                principal = await current_principal(request)
            else:
                principal = await write_principal(request, request.headers.get("x-csrf-token"))
            if principal.workspace_id != workspace_id:
                raise missing()
            connections: dict[UUID, int] = {}
            if project_id is not None:
                projects = cast(ProjectService, request.app.state.project_service)
                actor = ProjectActor(principal.user_id, workspace_id)
                await projects.require_active_project(actor, project_id, db)
                connections = await projects.active_repository_connections(actor, project_id, db)
            folders = await resolve_namespaces(db, workspace_id, principal.user_id, project_id)
            hidden_wikis = await hidden_wiki_ids(db, workspace_id, project_id, folders)
            yield KnowledgeStore(db, Scope(
                workspace_id, project_id, principal.user_id, principal.role, tuple(connections.items()),
                folders, hidden_wikis, device_id=device_id,
            ), cast(ObjectStoragePort | None, request.app.state.object_storage))
        except ProjectError:
            raise HTTPException(404, detail={"code": "PROJECT_NOT_FOUND", "message": "项目不存在或无权访问"}) from None
        except (AgentMailError, KnowledgeError) as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc


Store = Annotated[KnowledgeStore, Depends(context, scope="function")]


def _index(request: Request, store: Store) -> CodebaseIndex:
    return CodebaseIndex(store, cast(ProjectService, request.app.state.project_service))


CodeIndex = Annotated[CodebaseIndex, Depends(_index, scope="function")]


@router.get("")
async def search(
    store: Store, code: CodeIndex, query: Annotated[str, Query(max_length=512)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 20, check: bool = False, recall_mode: bool = False,
    depth: RecallDepth | None = None, check_details: bool = False, kind: KnowledgeKind | None = None,
    source_type: Literal["github_pr"] | None = None,
) -> dict[str, object]:
    if check and not query.strip():
        return empty_check() if check_details else {"relevant": False}
    return await code.annotate(
        await store.search(query.strip(), limit, check_only=check, recall=recall_mode, depth=depth,
                           check_details=check_details, kind=kind, source_type=source_type),
    )


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: SessionKey


class OpenInput(SessionInput):
    recall_mode: bool = False
    actor: UUID | None = None


class OpenPathInput(OpenInput):
    path: str = Field(min_length=1, max_length=1000)


class RecallStatsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: SessionKey | None = None
    limit: int = Field(default=10, ge=1, le=100)
    recent: bool = False


class ArchiveEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archive_id: UUID
    revision: int = Field(ge=1)


class AdoptionCandidatesInput(SessionInput):
    accepts_isolation: bool = Field(default=False, strict=True)
    limit: int = Field(default=20, ge=1, le=100)
    after: UUID | None = None
    archive: ArchiveEvidenceInput | None = None
    include_rubric: bool = Field(default=False, strict=True)
    include_resolved: bool = Field(default=False, strict=True)


class AdoptionVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: UUID
    revision: int = Field(ge=1)
    label: Literal["adopted", "ignored"]


class AdoptionInput(SessionInput):
    verdicts: list[AdoptionVerdict] = Field(min_length=1, max_length=100)
    archive: ArchiveEvidenceInput | None = None

    @model_validator(mode="after")
    def unique_entries(self) -> "AdoptionInput":
        if len({row.entry_id for row in self.verdicts}) != len(self.verdicts):
            raise ValueError("每个条目只能提交一个采纳判定。")
        return self


class TranscriptTurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant", "tool"]
    text: str = Field(min_length=1, max_length=MAX_TURN_TEXT, pattern=r"^[^\x00]+$")


class SkillInstallationObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: UUID
    observed_at: AwareDatetime
    description: str | None = Field(default=None, max_length=1024, pattern=r"^[^\x00]+$")


class SkillCaptureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installations: list[SkillInstallationObservationInput] = Field(max_length=MAX_ROUTING_SKILLS)
    usage_profile: EvidenceDigest | None = None
    usage_session: EvidenceDigest | None = None

    @model_validator(mode="after")
    def verify_sources(self) -> "SkillCaptureInput":
        if len({entry.generation for entry in self.installations}) != len(self.installations):
            raise ValueError("Skill 安装代次不能重复。")
        if (self.usage_profile is None) != (self.usage_session is None):
            raise ValueError("Skill 加载归因需要同时提供配置和根会话摘要。")
        return self

    def capture(self) -> DeliveryCapture:
        return DeliveryCapture(
            [DeliveryObservation(item.generation, item.observed_at, safe_skill_description(item.description))
             for item in self.installations],
            self.usage_profile, self.usage_session,
        )


class TranscriptPublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime: Literal["codex", "claude-code"]
    session: UUID
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160, pattern=r"^[^\x00]+$")]
    turns: list[TranscriptTurnInput] = Field(min_length=1, max_length=MAX_TRANSCRIPT_TURNS)
    captured_at: AwareDatetime | None = None
    automatic_mode: ArchiveRunMode | None = None
    git_start: GitStartInput | None = None
    skill_capture: SkillCaptureInput | None = None

    @model_validator(mode="after")
    def require_automatic_capture_time(self) -> "TranscriptPublishInput":
        if self.automatic_mode is not None and self.captured_at is None:
            raise ValueError("自动归档需提供本机快照的采集时间。")
        return self


class ScorerLabelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: ScorerValue
    score: FiniteFloat
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000, pattern=r"^[^\x00]+$"),
    ]


class ScorerFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=MAX_SCORER_FOLDER_BYTES)


class ScorerInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=24000, pattern=r"^[^\x00]+$"),
    ]
    labels: list[ScorerLabelInput] = Field(min_length=2, max_length=20)
    passing_score: FiniteFloat
    sampling_rate: FiniteFloat = Field(default=25, ge=0, le=100)
    sessions: bool = False
    model: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:@+\-]{0,159}$")] | None = None
    tool: ScorerTool | None = None
    isolation: ScoringIsolation | None = None
    files: list[ScorerFileInput] | None = Field(default=None, max_length=1000)
    base_revision: int | None = Field(default=None, ge=0)
    namespace: Directory | None = None

    @model_validator(mode="after")
    def label_scale(self) -> "ScorerInput":
        if len({row.value for row in self.labels}) != len(self.labels):
            raise ValueError("评分标签不能重复。")
        if not min(row.score for row in self.labels) < self.passing_score <= max(row.score for row in self.labels):
            raise ValueError("至少需要一个通过标签和一个未通过标签。")
        return self

    def definition(self) -> ScorerDefinition:
        labels = [ScorerLabel(value=row.value, score=row.score, description=row.description) for row in self.labels]
        files = None
        if self.files is not None:
            files = [ScorerFile(path=row.path, content=row.content) for row in self.files]
        try:
            normalized = normalize_scorer_files(self.prompt, files)
        except ValueError as error:
            raise KnowledgeError("KNOWLEDGE_SCORER_FILES", str(error), 422) from error
        return ScorerDefinition(
            self.prompt, labels, self.passing_score, self.sampling_rate,
            self.sessions, self.model, self.tool, normalized, self.isolation,
        )


class ScorePlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepts_isolation: bool = Field(default=False, strict=True)
    manual: bool = Field(default=False, strict=True)
    scorers: list[ScorerName] = Field(min_length=1, max_length=20)

    @field_validator("scorers")
    @classmethod
    def unique_names(cls, names: list[str]) -> list[str]:
        if len(names) != len(set(names)):
            raise ValueError("每个评分标准只能选一次。")
        return names


class LocalScoreEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime: Literal["codex", "claude-code"]
    subject_key: EvidenceDigest
    digest: EvidenceDigest
    run_mode: ArchiveRunMode | None = None
    recall_session_key: EvidenceDigest | None = None
    captured_at: AwareDatetime | None = None
    git_start: GitStartInput | None = None
    skill_capture: SkillCaptureInput | None = None

    @model_validator(mode="after")
    def recall_attribution(self) -> "LocalScoreEvidenceInput":
        if (
            (self.recall_session_key is not None or self.captured_at is not None)
            and (self.run_mode is None or self.recall_session_key is None or self.captured_at is None)
        ):
            raise ValueError("后台采纳评分需要运行模式、召回归因键及采集时点。")
        return self


class SessionJudgmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scorer_id: UUID
    scorer_revision: int = Field(strict=True, ge=1)
    operation_id: UUID
    label: ScorerValue
    driver: ScoreDriver
    judge_call_id: UUID | None = None
    isolation: ScoringIsolation | None = None


class LocalScoreReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    revision: int = Field(strict=True, ge=1)
    digest: EvidenceDigest


class NativeScoreOfferInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["local"]
    runtime: Literal["codex", "claude-code"]
    subject_key: EvidenceDigest
    digest: EvidenceDigest


class ArchivedScoreOfferInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["archive"]
    archive_id: UUID
    revision: int = Field(strict=True, ge=1)


class NamedLocalInput(NativeScoreOfferInput):
    recall_session_key: EvidenceDigest
    captured_at: AwareDatetime
    skill_capture: SkillCaptureInput | None = None


NamedSubjectInput = Annotated[NamedLocalInput | ArchivedScoreOfferInput, Field(discriminator="kind")]
NamedScorerName = Annotated[
    str, StringConstraints(
        max_length=610,
        pattern=r"^(?:teamai:adoption:[0-9a-f]{12}|teamai:routing:(?:[\w./-]+:)?[a-z0-9][a-z0-9-]{0,63}|"
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,99})$",
    ),
]


def _named_subject(subject: NamedSubjectInput) -> NamedSubject:
    if isinstance(subject, NamedLocalInput):
        return NamedLocal(subject.runtime, subject.subject_key, subject.digest,
                          subject.recall_session_key, subject.captured_at,
                          subject.skill_capture.capture() if subject.skill_capture else None)
    return NamedArchive(subject.archive_id, subject.revision)


class NamedPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepts_isolation: bool = Field(default=False, strict=True)
    manual: bool = Field(default=False, strict=True)
    subject: NamedSubjectInput
    scorers: list[NamedScorerName] = Field(min_length=1, max_length=20)

    @field_validator("scorers")
    @classmethod
    def generated_names(cls, names: list[str]) -> list[str]:
        if not any(name.startswith(GENERATED_NAME_PREFIXES) for name in names):
            raise ValueError("点名计划至少包含一项采纳或 Skill 路由问题。")
        return ScorePlanInput.unique_names(names)


class NamedLabelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: NamedScorerName
    label: ScorerValue
    driver: ScoreDriver
    scorer_id: UUID | None = None
    scorer_revision: int | None = Field(default=None, strict=True, ge=1)
    entry_id: UUID | None = None
    entry_revision: int | None = Field(default=None, strict=True, ge=1)
    source_hash: EvidenceDigest | None = None
    evidence_hash: EvidenceDigest | None = None

    @model_validator(mode="after")
    def check_pin(self) -> "NamedLabelInput":
        if (self.scorer_id is None) != (self.scorer_revision is None):
            raise ValueError("评分标准 ID 和版本须同时给出。")
        if self.name.startswith(ADOPTION_NAME_PREFIX):
            if self.entry_id is None or self.entry_revision is None or self.source_hash is None or self.evidence_hash:
                raise ValueError("采纳问题须提供条目、版本和标准摘要。")
        elif self.name.startswith(ROUTING_NAME_PREFIX):
            if self.source_hash is None or self.evidence_hash is None or self.entry_id or self.entry_revision:
                raise ValueError("Skill 路由问题须提供标准和证据摘要。")
        elif self.scorer_id is None or any(value is not None for value in (
            self.entry_id, self.entry_revision, self.source_hash, self.evidence_hash,
        )):
            raise ValueError("普通标准须提供评分标准 ID 和版本。")
        return self

    def judgment(self) -> NamedLabel:
        return NamedLabel(self.name, self.label, self.driver, self.scorer_id, self.scorer_revision,
                          self.entry_id, self.entry_revision, self.source_hash, self.evidence_hash)


class NamedSettlementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: NamedSubjectInput
    operation_id: UUID
    items: list[NamedLabelInput] = Field(min_length=1, max_length=20)
    offer_mode: ArchiveRunMode | None = None
    recommended_routing: bool = Field(default=False, strict=True)
    isolation: ScoringIsolation | None = None

    @model_validator(mode="after")
    def validate_group(self) -> "NamedSettlementInput":
        names = [item.name for item in self.items]
        if self.recommended_routing and (self.offer_mode is None or not any(
            name.startswith(ROUTING_NAME_PREFIX) for name in names
        )):
            raise ValueError("自动路由建议只可在确认 Skill 路由问题时提交。")
        if self.offer_mode is None:
            NamedPlanInput.generated_names(names)
        else:
            ScorePlanInput.unique_names(names)
            if any(item.driver == "human" for item in self.items):
                raise ValueError("确认评分使用模型判定。")
        return self


class OfferLocalInput(NativeScoreOfferInput):
    recall_session_key: EvidenceDigest | None = None
    captured_at: AwareDatetime | None = None
    skill_capture: SkillCaptureInput | None = None

    @model_validator(mode="after")
    def recall_metadata(self) -> "OfferLocalInput":
        if (self.recall_session_key is None) != (self.captured_at is None):
            raise ValueError("请同时提供召回归因和采集时间。")
        return self


class ScoringOfferInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepts_isolation: bool = Field(default=False, strict=True)
    accept_routing: bool = Field(default=False, strict=True)
    recommended_routing: bool = Field(default=False, strict=True)
    run_mode: ArchiveRunMode
    subject: Annotated[OfferLocalInput | ArchivedScoreOfferInput, Field(discriminator="kind")]
    scorers: list[NamedScorerName] = Field(default_factory=list, max_length=20)

    @field_validator("scorers")
    @classmethod
    def unique_names(cls, names: list[str]) -> list[str]:
        return ScorePlanInput.unique_names(names)


class ScoringClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepts_isolation: bool = Field(default=False, strict=True)
    local_verified: bool = Field(default=False, strict=True)
    verified_tools: set[ScorerTool] = Field(default_factory=set, max_length=2)
    operation_id: UUID
    run_mode: ArchiveRunMode
    local_evidence: list[LocalScoreReferenceInput] = Field(default_factory=list, max_length=100)
    accept_batch: bool = Field(default=False, strict=True)
    accept_adoption: bool = Field(default=False, strict=True)
    accept_adoption_template: bool = Field(default=False, strict=True)
    accept_adoption_batch: bool = Field(default=False, strict=True)
    accept_mixed_batch: bool = Field(default=False, strict=True)
    accept_routing: bool = Field(default=False, strict=True)
    accept_routing_mix: bool = Field(default=False, strict=True)


class RoutingSettlementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    labels: dict[NamedScorerName, ScorerValue] | None = Field(default=None, min_length=1, max_length=20)
    error: JudgeError | None = None
    isolation: ScoringIsolation | None = None

    @model_validator(mode="after")
    def single_outcome(self) -> "RoutingSettlementInput":
        if (self.labels is None) == (self.error is None):
            raise ValueError("请提交整组标签或一个失败原因。")
        return self


class ScoringSettlementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: ScorerValue | None = None
    error: JudgeError | None = None
    isolation: ScoringIsolation | None = None

    @model_validator(mode="after")
    def single_outcome(self) -> "ScoringSettlementInput":
        if (self.label is None) == (self.error is None):
            raise ValueError("请提交一个标签或一个失败原因。")
        return self


class BatchScoringSettlementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    labels: dict[UUID, ScorerValue] | None = Field(default=None, min_length=2, max_length=20)
    error: JudgeError | None = None
    isolation: ScoringIsolation | None = None

    @model_validator(mode="after")
    def single_outcome(self) -> "BatchScoringSettlementInput":
        if (self.labels is None) == (self.error is None):
            raise ValueError("请输入全部标签或一个失败原因。")
        return self


class PendingFeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: UUID
    project_id: UUID | None
    session: SessionKey
    positive: bool
    operation_id: UUID
    occurred_at: AwareDatetime

    def pending(self) -> PendingFeedback:
        return PendingFeedback(
            self.entry_id, self.project_id, self.session, self.positive, self.operation_id, self.occurred_at,
        )


class QueuedLearningInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    project_id: UUID | None = None
    path: str = Field(min_length=1, max_length=1000)
    matches: bool = False

    def hint(self) -> QueuedLearning:
        return QueuedLearning(self.operation_id, self.project_id, self.path, self.matches)


class RecallQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=20, ge=1, le=100)
    recall_mode: bool = True
    depth: RecallDepth | None = None
    pending_feedback: list[PendingFeedbackInput] = Field(default_factory=list, max_length=100)
    queued_learnings: list[QueuedLearningInput] = Field(default_factory=list, max_length=100)

    def pending(self) -> list[PendingFeedback]:
        return [item.pending() for item in self.pending_feedback]


class RecallInput(RecallQuery):
    session: SessionKey
    recall_actor: UUID | None = None


class RecallPreviewInput(RecallQuery):
    check: bool = False
    check_details: bool = False


class OverlapInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=64000, pattern=r"^[^\x00]+$")


class QualityRevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    base_revision: int | None = Field(default=None, strict=True, ge=1)
    base_skill_version_id: UUID | None = None
    body: str = Field(min_length=1, max_length=64000, pattern=r"\S")

    @model_validator(mode="after")
    def one_base(self) -> "QualityRevisionInput":
        if (self.base_revision is None) == (self.base_skill_version_id is None):
            raise ValueError("质量草稿必须绑定一个发布基准。")
        return self

    @field_validator("body")
    @classmethod
    def no_nulls(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("草稿正文包含无效字符。")
        return value


class HintPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(strict=True)
    lookback_days: int = Field(strict=True, ge=1, le=90)
    max_mrs: int = Field(strict=True, ge=1, le=100)


class HintSourcesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urls: list[Annotated[str, StringConstraints(min_length=1, max_length=1000)]] = Field(min_length=1, max_length=100)


class SessionPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class PlanReminderInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool | None = None
    language: PlanReminderLanguage | None = None
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)] | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        if not self.model_fields_set or ("enabled" in self.model_fields_set and self.enabled is None):
            raise ValueError("请提供要更新的提醒设置，启用状态必须为 true 或 false。")
        if self.message is not None and ("\x00" in self.message or re.search(r"\{\{[^{}]*\}\}", self.message)):
            raise ValueError("计划提醒正文须为静态文本，不支持变量或空字符。")
        return self


class ArchivePolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ArchiveAction


class ScoringPolicyInput(ArchivePolicyInput):
    daily_limit: int | None = Field(default=None, strict=True, ge=0, le=100)
    batch: bool | None = Field(default=None, strict=True)
    isolation: ScoringIsolation | None = None
    skill_routing: bool | None = Field(default=None, strict=True)


class DirectoryScoringInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_revision: int = Field(ge=0)
    sessions: bool | dict[ArchiveRunMode, ArchiveAction] = Field(default_factory=dict)
    daily_limit: int | None = Field(default=None, ge=0, le=100)
    batch: bool | None = None
    recall_adoption: bool | None = None
    isolation: ScoringIsolation | None = None
    skill_routing: bool | None = None

    def settings(self) -> DirectoryScoring:
        sessions = self.sessions
        if isinstance(sessions, bool):
            action: ArchiveAction = "auto" if sessions else "off"
            sessions = dict.fromkeys(MODES, action)
        return DirectoryScoring(
            sessions, self.daily_limit, self.batch, self.recall_adoption, self.isolation, self.skill_routing,
        )


class CodebaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incremental: bool = False
    max_files: int = Field(default=DEFAULT_INDEX_FILES, ge=1, le=MAX_INDEX_FILES)
    operation_id: UUID | None = None
    expected_connection_version: int | None = Field(default=None, strict=True, ge=1)


class CodeDesignInput(BaseModel):
    """正文或领域 JSON 固定到计划证据，省略名称和正文时生成确定性图谱。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    basis: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str | None = Field(
        default=None,
        pattern=r"^(?:[a-z0-9][a-z0-9._-]{0,63}\.(?:md|profile\.json)|_domains\.json|_repository-overview\.md)$",
    )
    body: str | None = Field(default=None, min_length=1, max_length=52_000)
    domain_override: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def matching_document(self) -> Self:
        if (self.name is None) != (self.body is None):
            raise ValueError("设计文档名称与正文必须同时提供。")
        if self.domain_override is not None and self.name is not None:
            raise ValueError("显式归类和设计文档发布需要单独提交。")
        return self


class ReviewPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urls: list[Annotated[str, StringConstraints(min_length=1, max_length=1000, pattern=r"^[^\x00]+$")]] = Field(
        min_length=1, max_length=25,
    )


class ReviewRefreshInput(ReviewPlanInput):
    run_id: UUID


class RepositoryPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[Annotated[str, StringConstraints(min_length=1, max_length=2048, pattern=r"^[^\x00]+$")]] = Field(
        min_length=1, max_length=200,
    )


@router.post("/recall")
async def recall(store: Store, code: CodeIndex, payload: RecallInput) -> dict[str, object]:
    result = await store.search(
        payload.query, payload.limit, payload.session, recall=payload.recall_mode,
        request_id=get_request_id(), depth=payload.depth, pending_feedback=payload.pending(),
        queued_learnings=[item.hint() for item in payload.queued_learnings], recall_actor=payload.recall_actor,
    )
    return await code.annotate(result)


@router.post("/recall/preview")
async def recall_preview(store: Store, code: CodeIndex, payload: RecallPreviewInput) -> dict[str, object]:
    result = await store.search(
        payload.query, payload.limit, check_only=payload.check, recall=payload.recall_mode,
        depth=payload.depth, pending_feedback=payload.pending(), check_details=payload.check_details,
        queued_learnings=[item.hint() for item in payload.queued_learnings],
    )
    return await code.annotate(result)


@router.post("/recall/stats")
async def recall_stats(store: Store, payload: RecallStatsInput) -> dict[str, object]:
    return await store.journal.stats(payload.session, payload.limit, recent=payload.recent)


@router.post("/adoption/candidates")
async def adoption_candidates(store: Store, payload: AdoptionCandidatesInput) -> dict[str, object]:
    template = await adoption_template(store) if payload.include_rubric else None
    evidence, archive = None, None
    if reference := payload.archive:
        evidence, archive = await adoption_archive(store, payload.session, reference.archive_id, reference.revision)
    result = await store.adoption_candidates(payload.session, payload.limit, payload.after, evidence, template=template,
                                             include_resolved=payload.include_resolved)
    if payload.include_rubric:
        policy = await require_compatible_scoring(store, "attended")
        result = execution_plan(result, policy, accepts_isolation=payload.accepts_isolation)
    return result | {"archive": archive} if archive is not None else result


@router.post("/adoption/verdicts")
async def adoption_verdicts(store: Store, payload: AdoptionInput) -> dict[str, object]:
    evidence = None
    if reference := payload.archive:
        evidence, _ = await adoption_archive(store, payload.session, reference.archive_id, reference.revision)
    return await store.judge_adoption(
        payload.session, {item.entry_id: (item.revision, item.label == "adopted") for item in payload.verdicts},
        evidence,
    )


@router.post("/overlaps")
async def learning_overlaps(store: Store, payload: OverlapInput) -> dict[str, object]:
    """草稿只用来计算重叠建议。"""
    return {"items": await store.learning_overlaps(payload.body)}


@router.post("/shared-sessions", status_code=201)
async def publish_shared_session(request: Request, store: Store, payload: TranscriptPublishInput) -> dict[str, object]:
    turns = [TranscriptTurn(role=item.role, text=item.text) for item in payload.turns]
    policy_id = await automatic_archive_policy(store, payload.automatic_mode) if payload.automatic_mode else None
    if payload.git_start is not None and payload.captured_at is not None:
        projects = cast(ProjectService, request.app.state.project_service)
        await remember_start(store, projects, score_source_key(payload.runtime, payload.session),
                             payload.git_start, payload.captured_at)
    capture = payload.skill_capture.capture() if payload.skill_capture else None
    skill_evidence = await freeze_deliveries(store, payload.runtime, payload.captured_at, capture)
    return await publish_transcript(
        store, payload.runtime, payload.session, payload.title, turns, get_request_id(), payload.captured_at,
        archive_policy_id=policy_id, skill_evidence=skill_evidence,
        skill_capture_digest=capture.fingerprint() if capture else None,
    )


@router.get("/shared-sessions")
async def shared_sessions(store: Store, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> dict[str, object]:
    return await list_transcripts(store, limit)


@router.get("/shared-sessions/{archive_id}")
async def shared_session(
    store: Store, archive_id: UUID, revision: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, object]:
    return await read_transcript(store, archive_id, revision)


@router.get("/shared-sessions/{archive_id}/skill-evidence")
async def session_skill_evidence(
    store: Store, archive_id: UUID, revision: Annotated[int, Query(ge=1)],
) -> dict[str, object]:
    return {"skills": await read_skill_evidence(store, archive_id, revision)}


@router.get("/session-scorers")
async def session_scorers(store: Store, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> dict[str, object]:
    return await scorer_catalog(store, limit)


@router.put("/session-scorers/{name}")
async def put_scorer(store: Store, name: ScorerName, payload: ScorerInput) -> dict[str, object]:
    return await publish_scorer(
        store, name, payload.definition(), payload.base_revision, namespace=payload.namespace or "",
    )


@router.delete("/session-scorers/teamai-adoption")
async def clear_adoption_template(
    store: Store, base_revision: Annotated[int | None, Query(ge=0)] = None, namespace: Directory | None = None,
) -> dict[str, object]:
    return await reset_generated_template(store, ADOPTION_STANDARD, base_revision, namespace=namespace or "")


@router.delete("/session-scorers/teamai-routing")
async def clear_routing_template(
    store: Store, base_revision: Annotated[int | None, Query(ge=0)] = None, namespace: Directory | None = None,
) -> dict[str, object]:
    return await reset_generated_template(store, ROUTING_STANDARD, base_revision, namespace=namespace or "")


@router.post("/session-scorers/plan")
async def plan_local_session_score(store: Store, payload: ScorePlanInput) -> dict[str, object]:
    return await batch_reply(store, await rubric_plan(store, payload.scorers), "attended",
                             accepts_isolation=payload.accepts_isolation, manual=payload.manual)


@router.post("/shared-sessions/{archive_id}/score-plan")
async def session_score_plan(
    store: Store, archive_id: UUID, revision: Annotated[int, Query(ge=1)], payload: ScorePlanInput,
) -> dict[str, object]:
    return await batch_reply(store, await score_plan(store, archive_id, revision, payload.scorers), "attended",
                             accepts_isolation=payload.accepts_isolation, manual=payload.manual)


@router.post("/shared-sessions/{archive_id}/labels", status_code=201)
async def submit_session_label(
    store: Store, archive_id: UUID, revision: Annotated[int, Query(ge=1)], payload: SessionJudgmentInput,
) -> dict[str, object]:
    verdict = SessionJudgment(
        payload.scorer_id, payload.scorer_revision, payload.operation_id, payload.label, payload.driver,
        payload.judge_call_id, payload.isolation,
    )
    if payload.driver != "human":
        policy = await require_compatible_scoring(store, "attended", settling=True)
        require_verified(policy is not None and policy.isolation == "verified", payload.isolation)
    return await write_label(store, archive_id, revision, verdict, get_request_id())


@router.get("/shared-sessions/{archive_id}/labels")
async def session_labels(
    store: Store, archive_id: UUID, revision: Annotated[int, Query(ge=1)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50, history: bool = False,
) -> dict[str, object]:
    return await list_labels(store, archive_id, revision, limit, history=history)


@router.post("/session-scoring/claim")
async def take_session_scoring(store: Store, payload: ScoringClaimInput) -> dict[str, object]:
    result = await claim_scoring(
        store, payload.run_mode, payload.operation_id,
        [(record.id, record.revision, record.digest) for record in payload.local_evidence],
        accept_batch=payload.accept_batch, accept_adoption=payload.accept_adoption,
        accept_adoption_template=payload.accept_adoption_template,
        accept_adoption_batch=payload.accept_adoption_batch,
        accept_mixed_batch=payload.accept_mixed_batch, accept_routing=payload.accept_routing,
        accept_routing_mix=payload.accept_routing_mix,
    )
    if result.get("status") != "open":
        return result
    policy = await scoring_policy(store, payload.run_mode)
    plan = execution_plan(result, policy, accepts_isolation=payload.accepts_isolation)
    # 若设备无法达到要求，异常会回滚本次领取及额度占用。
    require_judge_tools(plan, set(payload.verified_tools), local_verified=payload.local_verified)
    if payload.local_verified or plan.get("isolation") == "verified":
        await pin_verified_claim(store, payload.operation_id)
        plan["isolation"] = "verified"
    return plan


@router.post("/session-scoring/named-plan")
async def plan_named_scoring(store: Store, payload: NamedPlanInput) -> dict[str, object]:
    plan = await named_plan(store, _named_subject(payload.subject), payload.scorers, manual=payload.manual)
    return await batch_reply(
        store, plan, "attended", accepts_isolation=payload.accepts_isolation, manual=payload.manual,
    )


@router.post("/session-scoring/named-calls")
async def finish_named_scoring(store: Store, payload: NamedSettlementInput) -> dict[str, object]:
    if any(item.driver != "human" for item in payload.items):
        policy = (await require_offer_policy(store, payload.offer_mode) if payload.offer_mode is not None
                  else await require_compatible_scoring(store, "attended", settling=True))
        if payload.recommended_routing and (not policy or not policy.skill_routing):
            raise KnowledgeError("KNOWLEDGE_SCORE_OFFER_CHANGED", "Skill 路由自动建议已关闭，请重新预览。", 403)
        require_verified(policy is not None and policy.isolation == "verified", payload.isolation)
    return await settle_named(store, _named_subject(payload.subject), payload.operation_id,
                              [item.judgment() for item in payload.items], get_request_id(),
                              offer_mode=payload.offer_mode, isolation=payload.isolation)


@router.get("/session-scoring/named-calls/{operation_id}")
async def get_named_scoring(store: Store, operation_id: UUID) -> dict[str, object]:
    return await read_named_call(store, operation_id)


@router.post("/session-scoring/offer")
async def preview_scoring_offer(store: Store, payload: ScoringOfferInput) -> dict[str, object]:
    subject = payload.subject
    named: NamedSubject | None = None
    explicit = any(name.startswith(GENERATED_NAME_PREFIXES) for name in payload.scorers)
    captured = isinstance(subject, OfferLocalInput) and subject.skill_capture is not None
    if explicit or not payload.scorers and payload.accept_routing and captured:
        if isinstance(subject, OfferLocalInput):
            if subject.recall_session_key is None or subject.captured_at is None:
                raise KnowledgeError(
                    "KNOWLEDGE_ADOPTION_EVIDENCE_SCOPE", "本机点名生成式问题需要根会话归因及采集时间。", 422,
                )
            named = NamedLocal(subject.runtime, subject.subject_key, subject.digest,
                               subject.recall_session_key, subject.captured_at,
                               subject.skill_capture.capture() if subject.skill_capture else None)
        else:
            named = NamedArchive(subject.archive_id, subject.revision)
    evidence = (
        NativeOfferSubject(subject.runtime, subject.subject_key, subject.digest)
        if isinstance(subject, NativeScoreOfferInput) else ArchivedOfferSubject(subject.archive_id, subject.revision)
    )
    result = await scoring_offer(
        store, payload.run_mode, evidence, payload.scorers, named_subject=named, accept_routing=payload.accept_routing,
        recommended_routing=payload.recommended_routing,
    )
    if result.get("status") == "off":
        return result
    policy = await scoring_policy(store, payload.run_mode)
    return execution_plan(result, policy, accepts_isolation=payload.accepts_isolation)


@router.post("/session-scoring/local-subjects", status_code=201)
async def register_local_scoring_subject(
    request: Request, store: Store, payload: LocalScoreEvidenceInput,
) -> dict[str, object]:
    if payload.run_mode is not None:
        await require_local_scoring_policy(store, payload.run_mode)
    if payload.git_start is not None:
        projects = cast(ProjectService, request.app.state.project_service)
        await remember_start(store, projects, payload.subject_key, payload.git_start, payload.captured_at)
    skill_evidence = await freeze_deliveries(
        store, payload.runtime, payload.captured_at, payload.skill_capture.capture() if payload.skill_capture else None,
    )
    return await register_local_evidence(
        store, payload.runtime, payload.subject_key, payload.digest, get_request_id(),
        mode=payload.run_mode, recall_session_key=payload.recall_session_key, captured_at=payload.captured_at,
        skill_evidence=skill_evidence,
    )


@router.get("/session-scoring/local-subjects")
async def list_local_scoring_subjects(
    store: Store, limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    return await scored_local_sessions(store, limit)


@router.post("/session-scoring/routing/{attempt_id}/settle")
async def finish_routing_scoring(
    store: Store, attempt_id: UUID, payload: RoutingSettlementInput,
) -> dict[str, object]:
    return await settle_routing(store, attempt_id, payload.labels, payload.error, payload.isolation, get_request_id())


@router.post("/session-scoring/attempts/{attempt_id}/settle")
async def finish_session_scoring(
    store: Store, attempt_id: UUID, payload: ScoringSettlementInput,
) -> dict[str, object]:
    return await settle_scoring(store, attempt_id, payload.label, payload.error, get_request_id(),
                                isolation=payload.isolation)


@router.post("/adoption/attempts/{attempt_id}/settle")
async def finish_adoption_scoring(
    store: Store, attempt_id: UUID, payload: ScoringSettlementInput,
) -> dict[str, object]:
    return await settle_adoption(store, attempt_id, payload.label, payload.error, get_request_id(),
                                 isolation=payload.isolation)


@router.post("/adoption/batches/{operation_id}/settle")
async def finish_adoption_batch(
    store: Store, operation_id: UUID, payload: BatchScoringSettlementInput,
) -> dict[str, object]:
    return await settle_adoption_group(store, operation_id, payload.labels or {}, payload.error, get_request_id(),
                                       isolation=payload.isolation)


@router.post("/session-scoring/batches/{operation_id}/settle")
async def finish_scoring_batch(
    store: Store, operation_id: UUID, payload: BatchScoringSettlementInput,
) -> dict[str, object]:
    return await settle_group(store, operation_id, payload.labels or {}, payload.error, get_request_id(),
                              isolation=payload.isolation)


@router.get("/settings/resource-namespaces")
async def resource_namespace_settings(store: Store) -> dict[str, object]:
    """成员读取自己的生效目录，管理员可以查看并编辑全空间声明。"""
    scope = store.scope
    if not scope.can_review:
        effective = scope.namespaces
        return {"effective": effective.view() if effective else None}
    revision, manifest = await current_manifest(store.db, scope.workspace)
    effective = select_namespaces(revision, manifest, scope.user, scope.project)
    return {
        "revision": revision, **manifest.model_dump(mode="json"),
        "effective": effective.view() if effective else None,
    }


@router.put("/settings/resource-namespaces")
async def replace_resource_namespace_settings(
    store: Store, request: Request, payload: NamespaceUpdate,
) -> dict[str, object]:
    store.require_reviewer()
    scope = store.scope
    if scope.project is not None:
        raise KnowledgeError("KNOWLEDGE_NAMESPACE_SCOPE", "请在工作空间范围保存资源目录清单。", 422)
    projects = cast(ProjectService, request.app.state.project_service)
    for project_id in sorted(payload.projects):
        await projects.require_workspace_project(scope.workspace, project_id, store.db)
    return await write_manifest(store.db, scope.workspace, scope.user, payload)


@router.get("/settings/mr-hint")
async def hint_policy(store: Store) -> dict[str, object]:
    return await store.hint_policy()


class WorkflowDefinitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0, strict=True)
    definition: KnowledgeWorkflowDefinition | None

    @field_validator("definition", mode="before", json_schema_input_type=KnowledgeWorkflowDefinition | str | None)
    @classmethod
    def workflow_yaml(cls, value: object) -> object:
        """控制台上传 YAML，沿用 CLI 使用的同一份定义校验。"""
        if not isinstance(value, str):
            return value
        if len(value.encode("utf-8")) > 131_072:
            raise ValueError("工作流文件不能超过 128 KiB。")
        try:
            document: object = yaml.safe_load(value)
        except (yaml.YAMLError, RecursionError) as exc:
            raise ValueError("无法读取工作流 YAML。") from exc
        if not isinstance(document, dict):
            raise ValueError("工作流 YAML 需要包含一份定义。")
        return document


@router.get("/workflows/{name}")
async def knowledge_workflow(store: Store, name: WorkflowName) -> dict[str, object]:
    return workflow_view(name, await latest_workflow(store, name))


@router.put("/workflows/{name}")
async def configure_knowledge_workflow(
    store: Store, name: WorkflowName, payload: WorkflowDefinitionInput,
) -> dict[str, object]:
    return await configure_workflow(
        store, name, base_revision=payload.base_revision, definition=payload.definition,
    )


class WorkflowDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[WorkflowDraft] = Field(max_length=25)


class WorkflowDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Decision
    reason: str = Field(default="", max_length=500, pattern=r"^[^\x00]*$")


class WorkflowSessionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archive_id: UUID
    revision: int = Field(ge=1, strict=True)


@router.post("/workflows/{name}/runs", status_code=201)
async def launch_knowledge_workflow(store: Store, name: WorkflowName, payload: WorkflowStart) -> dict[str, object]:
    return await start_workflow(store, name, payload)


@router.get("/workflow-runs/{run_id}")
async def knowledge_workflow_run(store: Store, run_id: UUID) -> dict[str, object]:
    return run_view(await find_workflow_run(store, run_id))


@router.put("/workflow-runs/{run_id}/drafts")
async def submit_knowledge_workflow_drafts(
    store: Store, run_id: UUID, payload: WorkflowDraftInput,
) -> dict[str, object]:
    return await draft_workflow(store, run_id, payload.items)


@router.put("/workflow-runs/{run_id}/approvals/{step}")
async def decide_knowledge_workflow_step(
    store: Store, run_id: UUID, step: str, payload: WorkflowDecisionInput,
) -> dict[str, object]:
    return await decide_workflow(store, run_id, step, payload.decision, payload.reason)


@router.put("/workflow-runs/{run_id}/session")
async def complete_knowledge_workflow_session(
    store: Store, run_id: UUID, payload: WorkflowSessionReceipt,
) -> dict[str, object]:
    return await finish_workflow_session(store, run_id, payload.archive_id, payload.revision)


@router.put("/settings/mr-hint")
async def set_hint_policy(store: Store, payload: HintPolicyInput) -> dict[str, object]:
    return await store.set_hint_policy(payload.enabled, payload.lookback_days, payload.max_mrs)


@router.get("/settings/session-context")
async def session_policies(store: Store) -> dict[SessionContextPolicy, dict[str, object]]:
    return {kind: value for kind, value in (await store.session_policies()).items() if kind != "usage-report"}


class ProjectRecallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inherit_shared: bool = Field(strict=True)


@router.get("/settings/recall-scope")
async def project_recall_settings(store: Store) -> ProjectRecallSettings:
    return await store.project_recall_settings()


@router.put("/settings/recall-scope")
async def set_project_recall_settings(store: Store, payload: ProjectRecallInput) -> ProjectRecallSettings:
    return await store.set_project_recall_settings(payload.inherit_shared)


@router.put("/settings/session-context/{kind}")
async def set_session_policy(
    store: Store, kind: SessionContextPolicy, payload: SessionPolicyInput,
) -> dict[str, object]:
    return await store.set_session_policy(kind, payload.enabled)


@router.get("/settings/plan-reminder")
async def get_plan_reminder_policy(store: Store) -> PlanReminderSettings:
    return await store.plan_reminder_policy()


@router.patch("/settings/plan-reminder")
async def update_plan_reminder_policy(store: Store, payload: PlanReminderInput) -> PlanReminderSettings:
    return await store.set_plan_reminder_policy(cast(PlanReminderChange, payload.model_dump(exclude_unset=True)))


@router.get("/settings/usage-report")
async def get_usage_report_policy(store: Store) -> dict[str, object]:
    return (await store.session_policies())["usage-report"]


@router.put("/settings/usage-report")
async def set_usage_report_policy(store: Store, payload: SessionPolicyInput) -> dict[str, object]:
    return await store.set_session_policy("usage-report", payload.enabled)


@router.get("/settings/session-archive")
async def get_archive_policies(store: Store) -> dict[ArchiveRunMode, dict[str, str]]:
    return await archive_policies(store)


@router.put("/settings/session-archive/{mode}")
async def update_archive_policy(store: Store, mode: ArchiveRunMode, payload: ArchivePolicyInput) -> dict[str, str]:
    return await set_archive_policy(store, mode, payload.action)


@router.get("/settings/session-scoring")
async def get_scoring_policies(
    store: Store, workspace_defaults: bool = False,
) -> dict[ArchiveRunMode, dict[str, object]]:
    return await scoring_policies(store, workspace_defaults=workspace_defaults)


@router.put("/settings/session-scoring/{mode}")
async def update_scoring_policy(
    store: Store, mode: ArchiveRunMode, payload: ScoringPolicyInput,
) -> dict[str, object]:
    return await set_scoring_policy(
        store, mode, payload.action, payload.daily_limit, payload.batch, payload.isolation, payload.skill_routing,
    )


@router.get("/settings/session-scoring/namespaces/{namespace}")
async def get_directory_scoring(store: Store, namespace: Directory) -> dict[str, object]:
    return await directory_scoring(store, namespace)


@router.put("/settings/session-scoring/namespaces/{namespace}")
async def update_directory_scoring(
    store: Store, namespace: Directory, payload: DirectoryScoringInput,
) -> dict[str, object]:
    return await set_directory_scoring(store, namespace, payload.base_revision, payload.settings())


@router.post("/mr-hints/check")
async def known_review_sources(store: Store, payload: HintSourcesInput) -> dict[str, object]:
    return {"policy": await store.hint_policy(), "imported": await store.imported_reviews(payload.urls)}


@router.get("/codebases/reconcile")
async def preview_reconciliation(store: Store) -> dict[str, object]:
    return await store.reconcile_code(preview=True)


@router.post("/codebases/reconcile")
async def publish_reconciliation(store: Store) -> dict[str, object]:
    return await store.reconcile_code(preview=False)


@router.get("/codebases")
async def codebase_list(code: CodeIndex, lint: bool = False) -> dict[str, object]:
    return await code.list_status(lint=lint)


@router.post("/codebases/review-plan")
async def review_codebase_plan(code: CodeIndex, payload: ReviewPlanInput) -> dict[str, object]:
    return await code.review_plan(payload.urls)


@router.post("/codebases/repo-plan")
async def repository_plan(code: CodeIndex, payload: RepositoryPlanInput) -> dict[str, object]:
    return await code.repository_plan(payload.sources)


@router.post("/codebases/local")
async def index_local_codebase(code: CodeIndex, request: Request) -> dict[str, object]:
    if code.scope.project is None:
        raise KnowledgeError("KNOWLEDGE_CODEBASE_SCOPE", "本地目录导入需要选择项目。", 422)
    # 在解码 JSON 前限制请求大小，错误响应只返回固定消息，避免回显源码。
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_LOCAL_BODY_BYTES:
            raise KnowledgeError("KNOWLEDGE_LOCAL_TOO_LARGE", "本地目录快照请求超过大小上限。", 413)
        body.extend(chunk)
    try:
        payload = LocalCodeSnapshot.model_validate_json(body)
    except ValidationError:
        raise KnowledgeError("KNOWLEDGE_LOCAL_INVALID", "本地目录快照的路径、编码或大小无效。", 422) from None
    return await code.index_local(payload)


@router.get("/codebases/gaps")
async def codebase_gaps(store: Store, query: Annotated[str, Query(max_length=512)] = "") -> dict[str, object]:
    return {"items": await store.knowledge_gaps(query.strip())}


@router.get("/codebases/graph-documents/{topic}")
async def graph_documents(code: CodeIndex, topic: GraphTopic, codebase_id: UUID | None = None) -> dict[str, object]:
    return await code.graph_document_catalog(topic, codebase_id)


@router.get("/codebases/{codebase_id}/modules")
async def codebase_modules(
    code: CodeIndex, codebase_id: UUID, query: Annotated[str, Query(max_length=160)] = "",
) -> dict[str, object]:
    return await code.module_catalog(codebase_id, query.strip())


@router.get("/codebases/local/{codebase_id}")
async def local_codebase_status(code: CodeIndex, codebase_id: UUID, lint: bool = False) -> dict[str, object]:
    return await code.local_status(codebase_id, lint=lint)


@router.get("/codebases/{repository_id}")
async def codebase_status(code: CodeIndex, repository_id: UUID, lint: bool = False) -> dict[str, object]:
    return await code.status(repository_id, lint=lint)


@router.post("/codebases/{repository_id}")
async def index_codebase(code: CodeIndex, repository_id: UUID, payload: CodebaseInput) -> dict[str, object]:
    return await code.index(
        repository_id, incremental=payload.incremental, limit=payload.max_files,
        operation_id=payload.operation_id, expected_connection_version=payload.expected_connection_version,
    )


@router.get("/codebases/local/{codebase_id}/design")
async def local_design_plan(
    code: CodeIndex, codebase_id: UUID, max_modules: Annotated[int, Query(ge=0, le=(1 << 53) - 1)] = 0,
) -> dict[str, object]:
    return await code.design_plan(codebase_id, local=True, maximum=max_modules)


@router.get("/codebases/{repository_id}/design")
async def repository_design_plan(
    code: CodeIndex, repository_id: UUID, max_modules: Annotated[int, Query(ge=0, le=(1 << 53) - 1)] = 0,
) -> dict[str, object]:
    return await code.design_plan(repository_id, local=False, maximum=max_modules)


@router.post("/codebases/local/{codebase_id}/design")
async def publish_local_design(code: CodeIndex, codebase_id: UUID, payload: CodeDesignInput) -> dict[str, object]:
    return await code.publish_design(
        codebase_id, local=True, basis=payload.basis, name=payload.name, body=payload.body,
        domain_override=payload.domain_override,
    )


@router.post("/codebases/{repository_id}/design")
async def publish_repository_design(
    code: CodeIndex, repository_id: UUID, payload: CodeDesignInput,
) -> dict[str, object]:
    return await code.publish_design(
        repository_id, local=False, basis=payload.basis, name=payload.name, body=payload.body,
        domain_override=payload.domain_override,
    )


@router.post("/codebases/{repository_id}/refresh-reviews")
async def refresh_codebase_reviews(
    code: CodeIndex, repository_id: UUID, payload: ReviewRefreshInput,
) -> dict[str, object]:
    return await code.refresh_reviews(repository_id, payload.run_id, payload.urls)


def _ci(request: Request, store: Store) -> CiReview:
    return CiReview(store, cast(ProjectService, request.app.state.project_service))


@router.post("/ci/graph-preview")
async def preview_ci_graph(
    store: Store, request: Request, code: CodeIndex, payload: CiGraphPreviewInput,
) -> dict[str, object]:
    repo, version = await _ci(request, store).preview_source(payload.url)
    return await code.preview_review(
        repo, version, payload.base_commit.lower(), payload.head_commit.lower(), tuple(payload.paths),
    )


@router.get("/ci/extractions")
async def find_ci_extraction(
    store: Store, request: Request, url: ReviewUrl, head_commit: CommitSha,
) -> dict[str, object]:
    return await _ci(request, store).lookup(url, head_commit.lower())


@router.post("/ci/extractions", status_code=201)
async def propose_ci_extraction(store: Store, request: Request, payload: CiProposalInput) -> dict[str, object]:
    proposal = CiProposal(
        payload.url, payload.head_commit.lower(), payload.title.strip(), payload.body,
        list(dict.fromkeys(payload.tags)), payload.review_author,
    )
    return await _ci(request, store).propose(proposal, get_request_id())


@router.post("/ci/extractions/{extraction_id}/comment-lease")
async def lease_ci_comments(
    store: Store, request: Request, extraction_id: UUID, payload: CiCommentLeaseInput,
) -> dict[str, object]:
    return await _ci(request, store).comment_lease(extraction_id, payload.lease_id, release=payload.release)


@router.post("/ci/extractions/{extraction_id}/merge")
async def finish_ci_extraction(
    store: Store, request: Request, extraction_id: UUID, payload: CiMergeInput,
) -> dict[str, object]:
    return await _ci(request, store).finish(extraction_id, CiMerge(
        payload.merge_commit.lower(), payload.merged_at, payload.rejected_learning, payload.rejected_codebase,
        payload.comment_lease_id,
    ), get_request_id())


@router.get("/review")
async def pending(store: Store, limit: Annotated[int, Query(ge=1, le=100)] = 100) -> dict[str, object]:
    return await store.pending(limit)


@router.get("/maintenance")
async def maintenance(
    store: Store, limit: Annotated[int, Query(ge=1, le=500)] = 100, after: UUID | None = None,
    threshold: FiniteFloat = DEFAULT_PRUNE_THRESHOLD, candidates_only: bool = False, include_shared: bool = False,
) -> dict[str, object]:
    return await store.maintenance(
        limit=limit, after=after, threshold=threshold, candidates_only=candidates_only, include_shared=include_shared,
    )


@router.get("/maintenance/confidence")
async def confidence_preview(
    store: Store, limit: Annotated[int, Query(ge=1, le=500)] = 100, after: UUID | None = None,
    include_shared: bool = False,
) -> dict[str, object]:
    return await store.confidence_writeback(apply=False, limit=limit, after=after, include_shared=include_shared)


@router.post("/maintenance/confidence")
async def confidence_apply(
    store: Store, limit: Annotated[int, Query(ge=1, le=500)] = 100, after: UUID | None = None,
    include_shared: bool = False,
) -> dict[str, object]:
    return await store.confidence_writeback(apply=True, limit=limit, after=after, include_shared=include_shared)


@router.get("/maintenance/promote")
async def promotion_candidates(
    store: Store, limit: Annotated[int, Query(ge=1, le=100)] = 100, after: UUID | None = None,
    include_shared: bool = False,
) -> dict[str, object]:
    return await store.promotion_candidates(limit=limit, after=after, include_shared=include_shared)


@router.get("/maintenance/promote/{entry_id}")
async def promotion_context(store: Store, entry_id: UUID, include_shared: bool = False) -> dict[str, object]:
    return await store.promotion_context(entry_id, include_shared=include_shared)


@router.post("/maintenance/promote/{entry_id}", status_code=201)
async def submit_promotion(store: Store, entry_id: UUID, payload: PromotionInput) -> dict[str, object]:
    return await store.submit_promotion(
        entry_id, operation=payload.operation_id, base=payload.source_revision, kind=payload.kind,
        reason=payload.reason, body=payload.body,
    )


@router.get("/health")
async def health_report(
    store: Store, code: CodeIndex, limit: Annotated[int, Query(ge=1, le=100)] = 20, include_shared: bool = False,
) -> dict[str, object]:
    store.require_reviewer()
    names = dict(await SqlAlchemyIdentityStore(store.db).workspace_member_names(store.scope.workspace))
    result = await knowledge_health(store.db, store.scope, names, limit, include_shared=include_shared)
    return result | {"code_sources": await code.report_sources()}


class SessionActivityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime: Literal["codex", "claude-code"]
    subject_key: EvidenceDigest
    run_mode: ArchiveRunMode | Literal["not_recorded"]
    observed_at: AwareDatetime
    interrupts: int = Field(strict=True, ge=0, le=100_000)
    tool_rejections: int = Field(strict=True, ge=0, le=100_000)
    corrections: int = Field(strict=True, ge=0, le=100_000)
    prompts: int | None = Field(default=None, strict=True, ge=0, le=1_000_000)
    tokens: ConversationTokens | None = None
    trend: TrendInput | None = None


@router.put("/activity/sessions")
async def report_session_activity(store: Store, payload: SessionActivityInput) -> dict[str, object]:
    return await record_session(
        store, runtime=payload.runtime, subject=payload.subject_key, run_mode=payload.run_mode,
        interrupts=payload.interrupts, tool_rejections=payload.tool_rejections, corrections=payload.corrections,
        observed_at=payload.observed_at, prompts=payload.prompts, tokens=payload.tokens, trend=payload.trend,
    )


class SessionOutcomeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime: Literal["codex", "claude-code"]
    subject_key: EvidenceDigest
    run_mode: ArchiveRunMode | Literal["not_recorded"]
    stopped_at: AwareDatetime
    has_commit: bool = Field(strict=True)
    has_pr: bool = Field(strict=True)

    @model_validator(mode="after")
    def require_commit_for_pr(self) -> "SessionOutcomeInput":
        if self.has_pr and not self.has_commit:
            raise ValueError("PR 必须包含此会话归因的提交。")
        return self


@router.put("/activity/session-outcomes")
async def finish_session_outcome(store: Store, payload: SessionOutcomeInput) -> dict[str, object]:
    return await finish_session(
        store, runtime=payload.runtime, subject=payload.subject_key, mode=payload.run_mode,
        stopped=payload.stopped_at, has_commit=payload.has_commit, has_pr=payload.has_pr,
    )


class WorkflowActivityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: UUID
    connection_version: int = Field(strict=True, ge=1, le=2147483647)
    repository: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.-]+/[a-z0-9_.-]+$", max_length=250)]
    run_id: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,19}$")]
    workflow: WorkflowKind
    attempt: int = Field(strict=True, ge=1, le=2147483647)
    outcome: WorkflowOutcome
    duration_ms: int = Field(strict=True, ge=0, le=2147483647)

    @model_validator(mode="after")
    def skipped_duration(self) -> "WorkflowActivityInput":
        if self.outcome == "skipped" and self.duration_ms != 0:
            raise ValueError("跳过的工作流耗时须为零。")
        return self


@router.put("/activity/workflows")
async def report_workflow_activity(request: Request, store: Store, payload: WorkflowActivityInput) -> dict[str, object]:
    projects = cast(ProjectService, request.app.state.project_service)
    return await record_workflow(
        store, projects, repository_id=payload.repository_id, connection_version=payload.connection_version,
        repository=payload.repository, run_id=payload.run_id, workflow=payload.workflow, attempt=payload.attempt,
        outcome=payload.outcome, duration_ms=payload.duration_ms,
    )


@router.get("/digest")
async def digest_report(store: Store, include_shared: bool = False) -> dict[str, object]:
    return await knowledge_digest(store.db, store.scope, include_shared=include_shared)


@router.get("/digest/failures")
async def digest_failures(store: Store, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> dict[str, object]:
    return await scoring_failures(store, limit)


@router.get("/dashboard/improvement")
async def team_improvement_report(
    store: Store, days: Literal["7", "30", "90"] = "30", limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, object]:
    store.require_reviewer()
    return await improvement_dashboard(store.db, store.scope, int(days), limit)


@router.get("/dashboard/scorers")
async def scorer_dashboard_report(
    store: Store, days: Literal["7", "30", "90", "180"] = "30", limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, object]:
    return await scoring_failures(store, limit, days=int(days), dashboard=True)


@router.get("/dashboard/scorers/workflow")
async def workflow_scorer_dashboard_report(
    request: Request, store: Store, days: Literal["7", "30", "90", "180"] = "30",
    scorer: Annotated[str, Query(min_length=1, max_length=256)] | None = None,
    cohort: Annotated[str, Query(pattern=r"^[a-f0-9]{64}$")] | None = None,
    group_by: WorkflowScoreGroup | None = None,
    group: Annotated[str, Query(pattern=r"^[a-f0-9]{64}$")] | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 25,
) -> dict[str, object]:
    return await workflow_score_dashboard(
        store, cast(ProjectService, request.app.state.project_service),
        days=int(days), scorer=scorer, cohort=cohort, group_by=group_by, group=group, limit=limit,
    )


@router.post("/dashboard/scorers/workflow/import")
async def import_workflow_scorer_report(store: Store, payload: WorkflowScoreImport) -> dict[str, int]:
    return await import_workflow_scores(store, payload)


class WorkflowScoreSyncInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    enabled: bool
    source_project_id: UUID | None = None
    repository_id: UUID | None = None


@router.get("/dashboard/scorers/workflow/sync")
async def workflow_score_sync_status(request: Request, store: Store) -> dict[str, object]:
    return await sync_status(
        store, cast(ProjectService, request.app.state.project_service),
        available=request.app.state.workflow_score_sync is not None,
    )


@router.put("/dashboard/scorers/workflow/sync")
async def set_workflow_score_sync(request: Request, store: Store, payload: WorkflowScoreSyncInput) -> dict[str, object]:
    return await configure_sync(
        store, cast(ProjectService, request.app.state.project_service),
        expected_version=payload.expected_version, enabled=payload.enabled,
        project=payload.source_project_id, repository=payload.repository_id,
        available=request.app.state.workflow_score_sync is not None,
    )


@router.get("/dashboard/self-improvement")
async def self_improvement_dashboard_report(
    store: Store, scorer: ScorerName | None = None, limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    store.require_reviewer()
    return await self_improvement_dashboard(store, scorer, limit, offset)


@router.get("/dashboard/benchmarks")
async def benchmark_dashboard_report(
    store: Store, suite_id: UUID | None = None, days: Literal["30", "90", "365"] = "90",
    limit: Annotated[int, Query(ge=1, le=100)] = 20, offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    return await benchmark_dashboard(store, suite_id, int(days), limit, offset)


@router.get("/self-improvement/candidates")
async def improvement_candidates(store: Store, scorer: ScorerName, suite: SuitePath | None = None) -> dict[str, object]:
    scorer_id, subjects = await regression_candidates(store, scorer)
    coverage, proposal, repository = await suite_coverage(store, suite, scorer_id)
    return {
        "scorer": scorer, "scorer_id": scorer_id, "proposal_id": proposal, "repository_id": repository,
        "subjects": [subject.summary() | {"task": coverage.get(subject.subject_id)} for subject in subjects],
    }


@router.post("/self-improvement/proposals", status_code=201)
async def save_improvement_proposal(
    request: Request, store: Store, payload: ImprovementDraftInput, suite: SuitePath = DEFAULT_SUITE,
) -> dict[str, object]:
    return await save_improvement(
        store, cast(ProjectService, request.app.state.project_service), suite, payload.draft(),
    )


@router.get("/self-improvement/resume")
async def resume_improvement(
    store: Store, scorer: ScorerName, operation_id: UUID, suite: SuitePath = DEFAULT_SUITE,
) -> dict[str, object]:
    return await recover_improvement(store, suite, scorer, operation_id)


@router.get("/self-improvement/proposals/{proposal_id}/revision")
async def read_improvement_proposal(
    store: Store, proposal_id: UUID, suite: SuitePath = DEFAULT_SUITE,
) -> dict[str, object]:
    return await latest_improvement(store, suite, proposal_id)


@router.get("/self-improvement/proposals/{proposal_id}/delivery")
async def improvement_delivery_spec(
    request: Request, store: Store, proposal_id: UUID, suite: SuitePath = DEFAULT_SUITE,
) -> dict[str, object]:
    return await delivery_spec(store, cast(ProjectService, request.app.state.project_service), suite, proposal_id)


@router.post("/self-improvement/proposals/{proposal_id}/delivery")
async def reserve_improvement_delivery(
    request: Request, store: Store, proposal_id: UUID, payload: ImprovementDeliveryReservationInput,
    suite: SuitePath = DEFAULT_SUITE,
) -> dict[str, object]:
    return await reserve_delivery(
        store, cast(ProjectService, request.app.state.project_service), suite, proposal_id,
        Reservation(payload.revision, payload.head_commit, payload.prefix),
    )


@router.put("/self-improvement/proposals/{proposal_id}/delivery")
async def confirm_improvement_delivery(
    request: Request, store: Store, proposal_id: UUID, payload: ImprovementDeliveryConfirmationInput,
    suite: SuitePath = DEFAULT_SUITE,
) -> dict[str, object]:
    return await confirm_delivery(
        store, cast(ProjectService, request.app.state.project_service), suite, proposal_id,
        payload.revision, payload.pr_number, payload.benchmark_id,
    )


@router.post("/self-improvement/ci/plan")
async def plan_improvement_check(
    request: Request, store: Store, payload: ImprovementCheckInput,
) -> dict[str, object]:
    return await plan_check(
        store, cast(ProjectService, request.app.state.project_service), payload.event(),
        CheckAttempt(payload.operation_id, payload.workflow_run, payload.attempt),
    )


@router.post("/self-improvement/ci/result")
async def read_improvement_check(
    request: Request, store: Store, payload: ImprovementCheckInput,
) -> dict[str, object]:
    return await check_result(
        store, cast(ProjectService, request.app.state.project_service), payload.event(), payload.operation_id,
    )


@router.post("/self-improvement/ci/merge")
async def reconcile_improvement_merge(
    request: Request, store: Store, payload: ImprovementReviewEventInput,
) -> dict[str, object]:
    return await merge_check(store, cast(ProjectService, request.app.state.project_service), payload.event())


@router.post("/benchmark/suites", status_code=201)
async def create_regression_suite(request: Request, store: Store, payload: RegressionSuiteInput) -> dict[str, object]:
    return await register_suite(store, cast(ProjectService, request.app.state.project_service), payload.config())


@router.get("/benchmark/suites")
async def read_regression_suite(
    store: Store, suite: SuitePath = DEFAULT_SUITE, proposal_id: UUID | None = None,
) -> dict[str, object]:
    return await read_suite(store, suite, proposal_id)


@router.post("/benchmark/tasks", status_code=201)
async def add_regression_task(
    request: Request, store: Store, payload: RegressionTaskInput, suite: SuitePath = DEFAULT_SUITE,
) -> dict[str, object]:
    return await register_task(store, cast(ProjectService, request.app.state.project_service), suite, payload.draft())


@router.post("/benchmark/runs", status_code=201)
async def create_benchmark_run(
    request: Request, store: Store, payload: BenchmarkRunInput, suite: SuitePath = DEFAULT_SUITE,
) -> dict[str, object]:
    return await plan_run(store, cast(ProjectService, request.app.state.project_service), suite, payload.plan())


@router.get("/benchmark/runs/{run_id}")
async def read_benchmark_run(store: Store, run_id: UUID) -> dict[str, object]:
    return await report_run(store, run_id)


@router.get("/benchmark/runs/{run_id}/inputs")
async def read_benchmark_inputs(store: Store, run_id: UUID) -> dict[str, object]:
    return await execution_inputs(store, run_id)


@router.put("/benchmark/runs/{run_id}/execution")
async def register_benchmark_execution(
    store: Store, run_id: UUID, payload: BenchmarkExecutionInput,
) -> dict[str, object]:
    return dict(await bind_execution(store, run_id, payload.profile(), payload.check_operation_id))


@router.post("/benchmark/runs/{run_id}/results")
async def record_benchmark_results(store: Store, run_id: UUID, payload: BenchmarkResultsInput) -> dict[str, object]:
    return await record_results(store, run_id, [item.result() for item in payload.items], payload.check_operation_id)


@router.put("/benchmark/proposals/{proposal_id}")
async def settle_regression(
    store: Store, proposal_id: UUID, payload: RegressionSettlementInput, suite: SuitePath = DEFAULT_SUITE,
) -> dict[str, object]:
    return await settle_proposal(store, suite, proposal_id, payload.status)


@router.post("/maintenance/archive")
async def archive(
    store: Store, limit: Annotated[int, Query(ge=1, le=500)] = 100, after: UUID | None = None,
    threshold: FiniteFloat = DEFAULT_PRUNE_THRESHOLD, candidates_only: bool = False, include_shared: bool = False,
    entry_id: UUID | None = None,
) -> dict[str, object]:
    return await store.maintenance(
        archive=True, limit=limit, after=after, threshold=threshold, candidates_only=candidates_only,
        include_shared=include_shared, entry_id=entry_id,
    )


@router.get("/maintenance/quality")
async def quality_candidates(
    store: Store, limit: Annotated[int, Query(ge=1, le=500)] = 100, after: UUID | None = None,
    include_shared: bool = False,
) -> dict[str, object]:
    return await store.quality_candidates(after=after, limit=limit, include_shared=include_shared)


@router.get("/maintenance/quality/{entry_id}")
async def quality_context(store: Store, entry_id: UUID, include_shared: bool = False) -> dict[str, object]:
    return await store.quality_context(entry_id, include_shared=include_shared)


@router.post("/maintenance/quality/{entry_id}", status_code=201)
async def submit_quality_revision(store: Store, entry_id: UUID, payload: QualityRevisionInput) -> dict[str, object]:
    baseline = payload.base_skill_version_id or payload.base_revision
    assert baseline is not None  # 请求校验要求恰好一个发布基准。
    return await store.submit_quality_revision(entry_id, baseline, payload.body, payload.operation_id)


@router.post("", status_code=201)
async def contribute(store: Store, payload: ContributionInput) -> dict[str, object]:
    return await store.contribute(payload.contribution())


@router.post("/imports/iwiki-sections", status_code=201)
async def import_iwiki_sections(store: Store, payload: IWikiSectionsInput) -> dict[str, object]:
    return {"items": await store.import_iwiki_sections(
        payload.run_id, payload.source, [(item.section, item.body) for item in payload.sections],
        require_review=payload.require_review, request_id=get_request_id() or str(uuid4()),
    )}


@router.post("/imports", status_code=201)
async def import_knowledge(store: Store, payload: ImportInput) -> dict[str, object]:
    if sum(len(item.body.encode("utf-8")) for item in payload.items) > 800_000:
        raise HTTPException(422, detail={"code": "KNOWLEDGE_IMPORT_TOO_LARGE", "message": "导入正文超过批次大小限制。"})
    for item in payload.items:
        is_session_learning = item.source.type == "session" and item.kind == "learning"
        if (
            not item.source.ref
            or (item.source.type not in {"file", "wiki", "pull_request", "repository"} and not is_session_learning)
        ):
            raise HTTPException(422, detail={
                "code": "KNOWLEDGE_IMPORT_SOURCE_REQUIRED", "message": "导入需要原始出处。",
            })
        if item.promoted_from or item.base_revision or item.kind not in {"learning", "rule", "doc", "code"}:
            raise HTTPException(422, detail={"code": "KNOWLEDGE_IMPORT_TYPE", "message": "请选择可导入的知识类型。"})
    return {"items": await store.import_batch(
        [item.contribution() for item in payload.items],
        publish_reviewed=payload.publish_reviewed, request_id=get_request_id() or str(uuid4()),
        iwiki_terms=payload.iwiki_terms,
    )}


@router.get("/by-path")
async def detail_by_path(
    store: Store, code: CodeIndex, path: Annotated[str, Query(min_length=1, max_length=1000)],
    recall_mode: bool = False,
) -> dict[str, object]:
    return await code.annotate(await store.detail_path(path, recall=recall_mode))


@router.post("/by-path/open")
async def open_by_path(store: Store, code: CodeIndex, payload: OpenPathInput) -> dict[str, object]:
    return await code.annotate(await store.detail_path(
        payload.path, payload.session, recall=payload.recall_mode, actor=payload.actor,
    ))


@router.get("/{entry_id}")
async def detail(store: Store, code: CodeIndex, entry_id: UUID, recall_mode: bool = False) -> dict[str, object]:
    return await code.annotate(await store.detail(entry_id, recall=recall_mode))


@router.post("/{entry_id}/open")
async def open_entry(store: Store, code: CodeIndex, entry_id: UUID, payload: OpenInput) -> dict[str, object]:
    return await code.annotate(await store.detail(
        entry_id, payload.session, recall=payload.recall_mode, actor=payload.actor,
    ))


@router.post("/{entry_id}/revisions", status_code=201)
async def revise(store: Store, entry_id: UUID, payload: ContributionInput) -> dict[str, object]:
    return await store.contribute(payload.contribution(), entry_id)


@router.get("/{entry_id}/review")
async def preview_review(store: Store, entry_id: UUID, revision: Annotated[int, Query(ge=1)]) -> dict[str, object]:
    return await store.review_preview(entry_id, revision)


@router.post("/{entry_id}/review")
async def review(store: Store, entry_id: UUID, payload: ReviewInput) -> dict[str, object]:
    return await store.review(
        entry_id, payload.revision, payload.decision == "accept", payload.reason, get_request_id() or str(uuid4()),
    )


@router.post("/{entry_id}/feedback", status_code=204)
async def feedback(store: Store, entry_id: UUID, payload: FeedbackInput) -> Response:
    await store.feedback(
        entry_id, payload.session, payload.positive, operation=payload.operation_id, occurred_at=payload.occurred_at,
    )
    return Response(status_code=204)


@router.post("/{entry_id}/restore")
async def restore(store: Store, entry_id: UUID) -> dict[str, object]:
    return await store.restore(entry_id, get_request_id() or str(uuid4()))
