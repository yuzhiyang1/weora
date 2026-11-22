"""回归套件保存在项目范围，任务通过提案状态进入默认版本。"""

from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from weora.shared.infrastructure.database import Base

type BenchmarkTool = Literal["claude", "codex"]
type ProposalState = Literal["open", "merged", "closed"]
type BenchmarkArm = Literal["main", "pulled"]
type TrialOutcome = Literal["passed", "failed", "error", "unscored"]


class BenchmarkExecutionProfile(TypedDict):
    """固定容器镜像与随 CLI 挂载的试验和评判入口。"""

    image: str
    runner_sha256: str
    network: str
    gateway: str | None
    judge_tool: BenchmarkTool
    judge_model: str


class BenchmarkExecutionBinding(TypedDict):
    """保存运行内唯一的执行登记和操作者。"""

    id: str
    profile: BenchmarkExecutionProfile
    registered_by: str
    registered_at: str


class KnowledgeRegressionSuite(Base):
    __tablename__ = "knowledge_regression_suites"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "path", name="uq_knowledge_regression_suite_path"),
        CheckConstraint("repetitions BETWEEN 1 AND 10", name="ck_knowledge_regression_repetitions"),
        CheckConstraint("timeout_seconds > 0", name="ck_knowledge_regression_timeout"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    path: Mapped[str] = mapped_column(String(250))
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("project_repositories.id"))
    description: Mapped[str] = mapped_column(String(1000))
    tools: Mapped[list[BenchmarkTool]] = mapped_column(JSON())
    repetitions: Mapped[int]
    timeout_seconds: Mapped[int]
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeRegressionProposal(Base):
    __tablename__ = "knowledge_regression_proposals"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'merged', 'closed')", name="ck_knowledge_regression_proposal_status"),
        Index("uq_knowledge_regression_open_proposal", "suite_id", "scorer_id", unique=True,
              postgresql_where=text("status = 'open'"), sqlite_where=text("status = 'open'")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    suite_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_regression_suites.id"), index=True)
    scorer_id: Mapped[UUID]
    scorer: Mapped[str] = mapped_column(String(100))
    status: Mapped[ProposalState] = mapped_column(String(10))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeRegressionTask(Base):
    __tablename__ = "knowledge_regression_tasks"
    __table_args__ = (
        UniqueConstraint("suite_id", "operation_id", name="uq_knowledge_regression_task_operation"),
        Index("ix_knowledge_regression_task_subject", "suite_id", "subject_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    suite_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_regression_suites.id"))
    proposal_id: Mapped[UUID | None] = mapped_column(ForeignKey("knowledge_regression_proposals.id"), index=True)
    operation_id: Mapped[UUID]
    request_digest: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[UUID]
    label_id: Mapped[int] = mapped_column(ForeignKey("knowledge_session_labels.id"))
    name: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(160))
    prompt: Mapped[str] = mapped_column(Text)
    starting_commit: Mapped[str] = mapped_column(String(64))
    connection_version: Mapped[int]
    success_criteria: Mapped[str] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeRegressionRun(Base):
    """冻结项目仓库、两组资源提交及所选套件任务。"""

    __tablename__ = "knowledge_regression_runs"
    __table_args__ = (
        UniqueConstraint("suite_id", "operation_id", name="uq_knowledge_regression_run_operation"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    suite_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_regression_suites.id"))
    proposal_id: Mapped[UUID | None] = mapped_column(ForeignKey("knowledge_regression_proposals.id"))
    operation_id: Mapped[UUID]
    request_digest: Mapped[str] = mapped_column(String(64))
    repository_version: Mapped[int]
    resource_repository_id: Mapped[UUID] = mapped_column(ForeignKey("project_repositories.id"))
    resource_repository_version: Mapped[int]
    base_resources: Mapped[str] = mapped_column(String(64))
    proposal_resources: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 存在时所有收据必须对应这组执行条件。旧运行和外部执行器允许继续使用原契约。
    execution: Mapped[BenchmarkExecutionBinding | None] = mapped_column(JSON(none_as_null=True))


class KnowledgeRegressionTrial(Base):
    """同一任务、工具和重复序号下的两侧各只接收一个终态。"""

    __tablename__ = "knowledge_regression_trials"
    __table_args__ = (
        UniqueConstraint("run_id", "task_id", "tool", "arm", "repetition", name="uq_knowledge_regression_trial_slot"),
        CheckConstraint("tool IN ('claude', 'codex')", name="ck_knowledge_regression_trial_tool"),
        CheckConstraint("arm IN ('main', 'pulled')", name="ck_knowledge_regression_trial_arm"),
        CheckConstraint("repetition > 0", name="ck_knowledge_regression_trial_repetition"),
        CheckConstraint(
            "outcome IN ('passed', 'failed', 'error', 'unscored')", name="ck_knowledge_regression_trial_outcome",
        ),
        CheckConstraint(
            "(outcome IN ('passed', 'failed') AND judge_tool IS NOT NULL AND judge_model IS NOT NULL)"
            " OR ((outcome IS NULL OR outcome IN ('error', 'unscored'))"
            " AND judge_tool IS NULL AND judge_model IS NULL AND judge_effort IS NULL)",
            name="ck_knowledge_regression_trial_judge",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_regression_runs.id"))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_regression_tasks.id"))
    tool: Mapped[BenchmarkTool] = mapped_column(String(12))
    arm: Mapped[BenchmarkArm] = mapped_column(String(8))
    repetition: Mapped[int]
    outcome: Mapped[TrialOutcome | None] = mapped_column(String(12))
    judge_tool: Mapped[BenchmarkTool | None] = mapped_column(String(12))
    judge_model: Mapped[str | None] = mapped_column(String(160))
    judge_effort: Mapped[str | None] = mapped_column(String(32))
    recorded_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetEdit(TypedDict):
    path: str
    before_sha256: str | None
    content: str | None


class KnowledgeImprovementRevision(Base):
    """以不可变版本固定同一提案中待发布的累计资源差异。"""

    __tablename__ = "knowledge_improvement_revisions"
    __table_args__ = (UniqueConstraint("suite_id", "operation_id", name="uq_knowledge_improvement_operation"),)

    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_regression_proposals.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    suite_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_regression_suites.id"))
    operation_id: Mapped[UUID]
    request_digest: Mapped[str] = mapped_column(String(64))
    resource_repository_id: Mapped[UUID] = mapped_column(ForeignKey("project_repositories.id"))
    connection_version: Mapped[int]
    base_commit: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(2000))
    changes: Mapped[list[AssetEdit]] = mapped_column(JSON())
    task_ids: Mapped[list[str]] = mapped_column(JSON())
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeImprovementDelivery(Base):
    """一个预留只能绑定本修订的提交。跨请求恢复沿用同一分支和 PR。"""

    __tablename__ = "knowledge_improvement_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "revision"], ["knowledge_improvement_revisions.proposal_id",
                                         "knowledge_improvement_revisions.revision"],
        ),
    )

    proposal_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    head_commit: Mapped[str] = mapped_column(String(64))
    manifest_digest: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(8))
    pr_number: Mapped[int | None]
    reserved_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merge_commit: Mapped[str | None] = mapped_column(String(64))
    benchmark_id: Mapped[UUID | None] = mapped_column(ForeignKey("knowledge_regression_runs.id"))


class KnowledgeImprovementCheck(Base):
    """同一交付修订的 CI 尝试按登记顺序固定，旧任务不能覆盖新检查。"""

    __tablename__ = "knowledge_improvement_checks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "revision"], ["knowledge_improvement_deliveries.proposal_id",
                                         "knowledge_improvement_deliveries.revision"],
        ),
        UniqueConstraint("proposal_id", "revision", "operation_id", name="uq_knowledge_improvement_check_operation"),
        UniqueConstraint(
            "proposal_id", "revision", "workflow_run", "attempt", name="uq_knowledge_improvement_check_attempt",
        ),
        CheckConstraint(
            "(workflow_run IS NULL AND attempt IS NULL) OR (workflow_run IS NOT NULL AND attempt IS NOT NULL)",
            name="ck_knowledge_improvement_check_attempt",
        ),
        Index("ix_knowledge_improvement_checks_benchmark", "benchmark_id"),
    )

    proposal_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    generation: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[UUID]
    benchmark_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_regression_runs.id"))
    # Actions 重跑沿用 workflow_run。attempt 负责区分谁可以继续登记结果。
    workflow_run: Mapped[str | None] = mapped_column(String(30))
    attempt: Mapped[str | None] = mapped_column(String(30))
