"""知识正文版本和反馈分开保存，发布指针与审计在同一事务内变更。"""

from datetime import date, datetime
from typing import Literal, NotRequired, TypedDict
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from weora.modules.knowledge.domain import (
    ArchiveAction,
    ArchiveRunMode,
    PlanReminderLanguage,
    ScoringIsolation,
    SessionPolicyKind,
)
from weora.shared.infrastructure.database import Base
from weora_code_graph.extractor import FileFacts


class KnowledgeWorkflowVersion(Base):
    """每次团队覆盖或恢复默认都追加版本，供审批记录引用。"""

    __tablename__ = "knowledge_workflow_versions"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    definition: Mapped[dict[str, object] | None] = mapped_column(JSON(none_as_null=True))
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeWorkflowRun(Base):
    """冻结发起身份、项目及定义，以原计划恢复草拟和审批。"""

    __tablename__ = "knowledge_workflow_runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "created_by", "operation_id", name="uq_knowledge_workflow_run_operation"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("collab_agent_devices.id"))
    device_name: Mapped[str | None] = mapped_column(String(80))
    operation_id: Mapped[UUID]
    workflow_name: Mapped[str] = mapped_column(String(64))
    workflow_revision: Mapped[int]
    session_workflow_revision: Mapped[int | None]
    definition: Mapped[dict[str, object]] = mapped_column(JSON())
    parameters: Mapped[dict[str, object]] = mapped_column(JSON())
    outcomes: Mapped[dict[str, str]] = mapped_column(JSON())
    drafts: Mapped[list[dict[str, object]] | None] = mapped_column(JSON(none_as_null=True))
    published: Mapped[list[dict[str, object]] | None] = mapped_column(JSON(none_as_null=True))
    session_result: Mapped[dict[str, object] | None] = mapped_column(JSON(none_as_null=True))
    launch: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeWorkflowDecision(Base):
    """记录确认发起和逐步审批，区分浏览器用户、设备与自动批准。"""

    __tablename__ = "knowledge_workflow_decisions"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_workflow_runs.id"), primary_key=True)
    step: Mapped[str] = mapped_column(String(40), primary_key=True)
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(500))
    actor_kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("collab_agent_devices.id"))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeWorkflowScore(Base):
    """保存团队上报的原始判定快照，重评追加记录，导入者独立留痕。"""

    __tablename__ = "knowledge_workflow_scores"
    __table_args__ = (
        Index("ix_knowledge_workflow_scores_window", "workspace_id", "project_id", "scored_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    label_key: Mapped[str] = mapped_column(String(64))
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    imported_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, object]] = mapped_column(JSON())


class WorkflowScoreSyncSource(Base):
    """每个报表范围绑定一个已授权连接，记录租约和最后成功的分支快照。"""

    __tablename__ = "knowledge_workflow_score_sync_sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "scope_key", name="uq_workflow_score_sync_scope"),
        Index("ix_workflow_score_sync_due", "enabled", "next_sync_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    scope_key: Mapped[str] = mapped_column(String(36))
    source_project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("project_repositories.id"))
    connection_version: Mapped[int]
    configured_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    configured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int]
    enabled: Mapped[bool]
    revision: Mapped[str | None] = mapped_column(String(64))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_sync_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))


class WorkflowScoreSyncFile(Base):
    """保存同步快照中的报表文件与其归一化标签，不保存仓库原文。"""

    __tablename__ = "knowledge_workflow_score_sync_files"

    source_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_workflow_score_sync_sources.id"), primary_key=True)
    path: Mapped[str] = mapped_column(String(280), primary_key=True)
    oid: Mapped[str] = mapped_column(String(64))
    month: Mapped[str] = mapped_column(String(7))
    labels: Mapped[list[dict[str, object]]] = mapped_column(JSON())


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'active', 'archived', 'rejected')", name="ck_knowledge_entry_status"),
        CheckConstraint("kind IN ('learning', 'rule', 'doc', 'code', 'skill')", name="ck_knowledge_entry_kind"),
        Index("ix_knowledge_entries_scope", "workspace_id", "project_id", "status"),
        Index("ix_knowledge_entries_resource", "workspace_id", "project_id", "kind", "resource_key", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_revision: Mapped[int | None]
    resource_key: Mapped[str | None] = mapped_column(String(64))
    promoted_from: Mapped[UUID | None] = mapped_column(ForeignKey("knowledge_entries.id"), index=True)
    codebase_id: Mapped[UUID | None] = mapped_column(ForeignKey("knowledge_codebases.id"), index=True)


class KnowledgeRevision(Base):
    __tablename__ = "knowledge_revisions"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'published', 'rejected')", name="ck_knowledge_revision_status"),
        UniqueConstraint("workspace_id", "author_id", "operation_id", name="uq_knowledge_operation"),
        Index("ix_knowledge_revision_session_source", "workspace_id", "author_id", "source_session_key"),
    )

    entry_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_entries.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    author_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    operation_id: Mapped[UUID]
    request_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text())
    tags: Mapped[list[str]] = mapped_column(JSON())
    source: Mapped[dict[str, str]] = mapped_column(JSON())
    source_session_key: Mapped[str | None] = mapped_column(String(64))
    relations: Mapped[list[dict[str, str]]] = mapped_column(JSON())
    relation_findings: Mapped[list[dict[str, str]] | None] = mapped_column(JSON(none_as_null=True))
    rule_paths: Mapped[list[str]] = mapped_column(JSON(), default=list, server_default=text("'[]'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Markdown 原文日期随版本保存，独立于平台提交审计时间。
    authored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_domain: Mapped[str] = mapped_column(String(16))
    search_title_length: Mapped[int]
    search_length: Mapped[int]


class KnowledgeCodeGapSource(Base):
    """缺口的导入名和源码位置绑定到生成时的发布版本。"""

    __tablename__ = "knowledge_code_gap_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["entry_id", "revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
        CheckConstraint("line IS NULL OR line > 0", name="ck_knowledge_gap_source_line"),
        Index("ix_gap_source_name_key", "name_key"),
        Index("ix_gap_source_alias_key", "alias_key"),
        Index("ix_gap_source_path_key", "path_key"),
        Index("ix_gap_source_basename_key", "basename_key"),
    )

    entry_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    finding: Mapped[str] = mapped_column(String(48), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text())
    path: Mapped[str] = mapped_column(Text())
    line: Mapped[int | None]
    name_key: Mapped[str | None] = mapped_column(String(64))
    alias_key: Mapped[str | None] = mapped_column(String(64))
    path_key: Mapped[str | None] = mapped_column(String(64))
    basename_key: Mapped[str | None] = mapped_column(String(64))


class KnowledgeMarkdownPath(Base):
    """位置 0 是条目自身，负数是显式声明，正数是正文链接。"""

    __tablename__ = "knowledge_markdown_paths"
    __table_args__ = (
        ForeignKeyConstraint(
            ["entry_id", "revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_markdown_paths_lookup", "key", "position"),
    )

    entry_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True, server_default="REFERENCES")
    path: Mapped[str] = mapped_column(Text())
    position: Mapped[int]


class KnowledgeConfidenceSnapshot(Base):
    """维护时写回的活动评分，按发布版本与正文分开保存。"""

    __tablename__ = "knowledge_confidence_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["entry_id", "revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
        CheckConstraint("score BETWEEN 0 AND 100", name="ck_knowledge_confidence_score"),
    )

    entry_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    score: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))


class KnowledgeProductMapping(Base):
    """产品映射与正文分开存储，两端都锚定生成时的发布版本。"""

    __tablename__ = "knowledge_product_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["product_id", "product_revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["code_id", "code_revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
        CheckConstraint("product_id <> code_id", name="ck_knowledge_product_mapping_distinct"),
        Index("ix_knowledge_product_mapping_code", "project_id", "code_id"),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    product_id: Mapped[UUID] = mapped_column(primary_key=True)
    code_id: Mapped[UUID] = mapped_column(primary_key=True)
    product_revision: Mapped[int]
    code_revision: Mapped[int]
    terms: Mapped[list[dict[str, str | float]]] = mapped_column(JSON())


class KnowledgeSearchTerm(Base):
    """索引绑定不可变版本。检索时只读取当前有权访问的已发布版本。"""

    __tablename__ = "knowledge_search_terms"
    __table_args__ = (
        ForeignKeyConstraint(
            ["entry_id", "revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_search_terms_revision", "entry_id", "revision"),
    )

    workspace_id: Mapped[UUID] = mapped_column(primary_key=True)
    term: Mapped[str] = mapped_column(String(160), primary_key=True)
    entry_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    fields: Mapped[int]
    frequency: Mapped[int]


class KnowledgeEvent(Base):
    __tablename__ = "knowledge_events"
    __table_args__ = (Index("ix_knowledge_events_entry", "entry_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    entry_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_entries.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int | None]
    reason: Mapped[str] = mapped_column(String(1000))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeRecall(Base):
    __tablename__ = "knowledge_recalls"
    __table_args__ = (
        Index("ix_knowledge_recalls_entry", "entry_id"),
        Index("ix_knowledge_recalls_session", "user_id", "session_key"),
        ForeignKeyConstraint(
            ["entry_id", "recalled_revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            name="fk_knowledge_recall_revision",
        ),
        CheckConstraint(
            "credit_source IN ('opened', 'manual', 'judge')", name="ck_knowledge_recall_credit_source",
        ),
    )

    entry_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_entries.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    session_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    recall_count: Mapped[int]
    last_recalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    positive: Mapped[bool | None]
    voted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recalled_revision: Mapped[int | None]
    credit_source: Mapped[str | None] = mapped_column(String(12))
    manual_vote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manual_vote_id: Mapped[UUID | None]


class KnowledgeSkillRecall(Base):
    """独立 Skill 的会话账本，资产身份稳定，召回和反馈分别记录固定版本。"""

    __tablename__ = "knowledge_skill_recalls"
    __table_args__ = (
        CheckConstraint("credit_source IN ('opened', 'manual')", name="ck_knowledge_skill_recall_credit"),
    )

    entry_id: Mapped[UUID] = mapped_column(ForeignKey("skills.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    session_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    recall_count: Mapped[int]
    last_recalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    positive: Mapped[bool | None]
    voted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credit_source: Mapped[str | None] = mapped_column(String(12))
    manual_vote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manual_vote_id: Mapped[UUID | None]
    recalled_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("skill_versions.id"))
    credited_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("skill_versions.id"))


class KnowledgeSkillFeedbackEvent(Base):
    """记录人工反馈的版本、作用域和顺序，不保存会话原文。"""

    __tablename__ = "knowledge_skill_feedback_events"
    __table_args__ = (
        Index("ix_knowledge_skill_feedback_entry", "entry_id", "received_at"),
        UniqueConstraint("entry_id", "user_id", "session_key", "operation_id", name="uq_knowledge_skill_feedback_op"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    entry_id: Mapped[UUID] = mapped_column(ForeignKey("skills.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    version_id: Mapped[UUID] = mapped_column(ForeignKey("skill_versions.id"))
    session_key: Mapped[str] = mapped_column(String(64))
    operation_id: Mapped[UUID | None]
    positive: Mapped[bool]
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeRecallRun(Base):
    """一次有会话归因的召回，包括没有结果的请求。"""

    __tablename__ = "knowledge_recall_runs"
    __table_args__ = (
        Index("ix_knowledge_recall_runs_scope", "workspace_id", "user_id", "project_id", "session_key", "created_at"),
        Index("ix_knowledge_recall_runs_created", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    session_key: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    # 仅专用召回子 Agent 的请求携带执行者摘要，原始宿主 ID 留在设备。
    recall_actor_digest: Mapped[str | None] = mapped_column(String(64))
    hit_count: Mapped[int]
    top_score: Mapped[float] = mapped_column(Float())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    via_relation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeRecallRunHit(Base):
    """保存召回返回的版本和关系 ID，避免把当前发布状态回填为历史证据。"""

    __tablename__ = "knowledge_recall_run_hits"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_recall_runs.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[UUID]
    revision: Mapped[int]
    score: Mapped[float] = mapped_column(Float())
    relevant: Mapped[bool]
    eligible: Mapped[bool]
    related: Mapped[list[str]] = mapped_column(JSON())
    skill_version_id: Mapped[UUID | None]
    skill_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TranscriptTurn(TypedDict):
    role: Literal["user", "assistant", "tool"]
    text: str


class KnowledgeSharedSession(Base):
    """归档与本地评分共用版本标识，只有归档记录可读正文。"""

    __tablename__ = "knowledge_shared_sessions"
    __table_args__ = (
        CheckConstraint("runtime IN ('codex', 'claude-code')", name="ck_knowledge_shared_session_runtime"),
        Index("ix_knowledge_shared_sessions_scope", "workspace_id", "project_id", "updated_at", "id"),
        Index("ix_knowledge_scoring_source", "workspace_id", "author_id", "score_source_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    author_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    runtime: Mapped[str] = mapped_column(String(16))
    score_only: Mapped[bool] = mapped_column(server_default=text("false"))
    score_source_key: Mapped[str | None] = mapped_column(String(64))
    latest_revision: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeSessionOrigin(Base):
    """按原生会话指纹固定启动证据，供归档和本机评分共用。"""

    __tablename__ = "knowledge_session_origins"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    author_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    subject_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("project_repositories.id"))
    connection_version: Mapped[int]
    commit: Mapped[str] = mapped_column(String(64))
    tracked_changes: Mapped[bool]
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeliveredSkillEvidence(TypedDict):
    name: str
    namespace: str
    version_id: str
    installation_id: str
    description: str | None


class SessionSkillEvidence(TypedDict):
    available: list[DeliveredSkillEvidence]
    used: list[str] | None


class KnowledgeSharedSessionRevision(Base):
    __tablename__ = "knowledge_shared_session_revisions"
    __table_args__ = (UniqueConstraint("archive_id", "content_digest", name="uq_knowledge_shared_session_contents"),)

    archive_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_shared_sessions.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(160))
    turns: Mapped[list[TranscriptTurn]] = mapped_column(JSON())
    turn_count: Mapped[int]
    content_digest: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recall_session_key: Mapped[str | None] = mapped_column(String(64))
    archive_policy_id: Mapped[int | None] = mapped_column(ForeignKey(
        "knowledge_archive_policies.id", name="fk_knowledge_shared_session_archive_policy",
    ))
    local_run_mode: Mapped[ArchiveRunMode | None] = mapped_column(String(12))
    score_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # 缺省表示没有采集记录。空列表表示采集过且当时没有已核验的 Skill。
    skill_evidence: Mapped[SessionSkillEvidence | None] = mapped_column(JSON(none_as_null=True))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeArchivePolicy(Base):
    """分别保留有人值守和无人值守模式的归档策略历史。"""

    __tablename__ = "knowledge_archive_policies"
    __table_args__ = (
        CheckConstraint("run_mode IN ('attended', 'unattended')", name="ck_knowledge_archive_run_mode"),
        CheckConstraint("action IN ('off', 'ask', 'auto')", name="ck_knowledge_archive_action"),
        Index("ix_knowledge_archive_policy_version", "workspace_id", "run_mode", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    run_mode: Mapped[ArchiveRunMode] = mapped_column(String(12))
    action: Mapped[ArchiveAction] = mapped_column(String(4))
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScorerLabel(TypedDict):
    value: str
    score: float
    description: str


class ScorerFile(TypedDict):
    path: str
    content: str


class AdoptionRubric(TypedDict):
    id: str
    name: str
    revision: int
    prompt: str
    labels: list[ScorerLabel]
    passing_score: float
    model: str | None
    tool: Literal["codex", "claude"] | None
    evidence_path: str
    source_hash: str
    isolation: NotRequired[ScoringIsolation]


class KnowledgeSessionScorer(Base):
    """评分标准按发布版本保存，旧标签可以按原标准解释。"""

    __tablename__ = "knowledge_session_scorers"
    __table_args__ = (
        Index("ix_knowledge_session_scorer_scope", "workspace_id", "project_id", "name", "revision"),
        CheckConstraint("isolation IN ('best-effort', 'verified')", name="ck_knowledge_scorer_isolation"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String(100))
    namespace: Mapped[str] = mapped_column(String(64), server_default="")
    prompt: Mapped[str] = mapped_column(Text)
    labels: Mapped[list[ScorerLabel]] = mapped_column(JSON())
    passing_score: Mapped[float] = mapped_column(Float)
    sampling_rate: Mapped[float] = mapped_column(Float)
    sessions: Mapped[bool]
    model: Mapped[str | None] = mapped_column(String(160))
    tool: Mapped[Literal["codex", "claude"] | None] = mapped_column(String(12))
    files: Mapped[list[ScorerFile] | None] = mapped_column(JSON(none_as_null=True))
    source_hash: Mapped[str | None] = mapped_column(String(64))
    isolation: Mapped[ScoringIsolation | None] = mapped_column(String(12))
    is_adoption_template: Mapped[bool] = mapped_column(server_default=text("false"))
    adoption_template_archived: Mapped[bool] = mapped_column(server_default=text("false"))
    published_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeSessionLabel(Base):
    """标签关联固定的会话版本和评分标准，人工及模型评分各自保留。"""

    __tablename__ = "knowledge_session_labels"
    __table_args__ = (
        ForeignKeyConstraint(["archive_id", "archive_revision"], [
            "knowledge_shared_session_revisions.archive_id", "knowledge_shared_session_revisions.revision",
        ]),
        ForeignKeyConstraint(["scorer_id", "scorer_revision"], [
            "knowledge_session_scorers.id", "knowledge_session_scorers.revision",
        ]),
        UniqueConstraint("judge_id", "operation_id", name="uq_knowledge_session_label_operation"),
        UniqueConstraint("judge_id", "judge_call_id", "scorer_id", name="uq_knowledge_session_label_call_scorer"),
        CheckConstraint("driver IN ('human', 'codex', 'claude')", name="ck_knowledge_session_label_driver"),
        CheckConstraint("isolation IN ('best-effort', 'verified')", name="ck_knowledge_session_label_isolation"),
        Index("ix_knowledge_session_label_archive", "archive_id", "archive_revision", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    archive_id: Mapped[UUID]
    archive_revision: Mapped[int]
    scorer_id: Mapped[UUID]
    scorer_revision: Mapped[int]
    judge_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    operation_id: Mapped[UUID]
    driver: Mapped[str] = mapped_column(String(12))
    isolation: Mapped[ScoringIsolation | None] = mapped_column(String(12))
    label: Mapped[str] = mapped_column(String(80))
    judge_call_id: Mapped[UUID | None]
    request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeScoringPolicy(Base):
    """团队独立配置两种运行模式的后台评分与每日限额。"""

    __tablename__ = "knowledge_scoring_policies"
    __table_args__ = (
        CheckConstraint("run_mode IN ('attended', 'unattended')", name="ck_knowledge_scoring_policy_mode"),
        CheckConstraint("action IN ('off', 'ask', 'auto')", name="ck_knowledge_scoring_policy_action"),
        CheckConstraint("daily_limit BETWEEN 0 AND 100", name="ck_knowledge_scoring_policy_quota"),
        CheckConstraint("isolation IN ('best-effort', 'verified')", name="ck_knowledge_scoring_policy_isolation"),
        Index("ix_knowledge_scoring_policy_version", "workspace_id", "run_mode", "id"),
        Index("ix_knowledge_scoring_policy_namespace", "workspace_id", "namespace", "run_mode", "id"),
        CheckConstraint(
            "namespace <> '' OR (action IS NOT NULL AND daily_limit IS NOT NULL AND batch IS NOT NULL)",
            name="ck_knowledge_scoring_policy_workspace_values",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    namespace: Mapped[str] = mapped_column(String(64), server_default="")
    run_mode: Mapped[ArchiveRunMode] = mapped_column(String(12))
    action: Mapped[ArchiveAction | None] = mapped_column(String(4))
    daily_limit: Mapped[int | None]
    # 目录可以让 batch 继承上级，显式空值需要保留。
    batch: Mapped[bool | None] = mapped_column(Boolean().evaluates_none(), server_default=text("false"))
    # 目录缺省时，自动采纳沿用团队开关。
    recall_adoption: Mapped[bool | None]
    skill_routing: Mapped[bool | None]
    isolation: Mapped[ScoringIsolation | None] = mapped_column(String(12))
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeScoringAttempt(Base):
    """每次领取都留存，用于续做、限额核算和过期判定。"""

    __tablename__ = "knowledge_scoring_attempts"
    __table_args__ = (
        ForeignKeyConstraint(["archive_id", "archive_revision"], [
            "knowledge_shared_session_revisions.archive_id", "knowledge_shared_session_revisions.revision",
        ]),
        ForeignKeyConstraint(["scorer_id", "scorer_revision"], [
            "knowledge_session_scorers.id", "knowledge_session_scorers.revision",
        ]),
        UniqueConstraint(
            "archive_id", "archive_revision", "scorer_id", "scorer_revision", "judge_id", "sequence",
            name="uq_knowledge_scoring_attempt_sequence",
        ),
        CheckConstraint("state IN ('open', 'failed', 'completed')", name="ck_knowledge_scoring_attempt_state"),
        CheckConstraint("sequence BETWEEN 1 AND 4", name="ck_knowledge_scoring_attempt_sequence"),
        Index("ix_knowledge_scoring_attempt_quota", "workspace_id", "judge_id", "started_at"),
        Index("ix_knowledge_scoring_attempt_call", "judge_call_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    archive_id: Mapped[UUID]
    archive_revision: Mapped[int]
    scorer_id: Mapped[UUID]
    scorer_revision: Mapped[int]
    judge_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    policy_id: Mapped[int] = mapped_column(ForeignKey("knowledge_scoring_policies.id"))
    run_mode: Mapped[ArchiveRunMode] = mapped_column(String(12))
    sequence: Mapped[int]
    state: Mapped[Literal["open", "failed", "completed"]] = mapped_column(String(12))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(80))
    label_id: Mapped[int | None] = mapped_column(ForeignKey("knowledge_session_labels.id"))
    judge_call_id: Mapped[UUID | None]
    requires_verified: Mapped[bool] = mapped_column(server_default=text("false"))


class KnowledgeAdoptionAttempt(Base):
    """生成式采纳问题的租约，绑定根会话、会话证据和召回正文版本。"""

    __tablename__ = "knowledge_adoption_attempts"
    __table_args__ = (
        ForeignKeyConstraint(["archive_id", "archive_revision"], [
            "knowledge_shared_session_revisions.archive_id", "knowledge_shared_session_revisions.revision",
        ]),
        ForeignKeyConstraint(["entry_id", "entry_revision"], [
            "knowledge_revisions.entry_id", "knowledge_revisions.revision",
        ]),
        UniqueConstraint(
            "judge_id", "session_key", "entry_id", "entry_revision", "sequence",
            name="uq_knowledge_adoption_attempt_sequence",
        ),
        CheckConstraint("state IN ('open', 'failed', 'completed')", name="ck_knowledge_adoption_attempt_state"),
        CheckConstraint("sequence BETWEEN 1 AND 4", name="ck_knowledge_adoption_attempt_sequence"),
        CheckConstraint("label IN ('adopted', 'ignored')", name="ck_knowledge_adoption_attempt_label"),
        CheckConstraint("isolation IN ('best-effort', 'verified')", name="ck_knowledge_adoption_attempt_isolation"),
        Index("ix_knowledge_adoption_attempt_quota", "workspace_id", "judge_id", "started_at"),
        Index("ix_knowledge_adoption_attempt_call", "judge_call_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    archive_id: Mapped[UUID]
    archive_revision: Mapped[int]
    entry_id: Mapped[UUID]
    entry_revision: Mapped[int]
    session_key: Mapped[str] = mapped_column(String(64))
    judge_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    policy_id: Mapped[int] = mapped_column(ForeignKey("knowledge_scoring_policies.id"))
    run_mode: Mapped[ArchiveRunMode] = mapped_column(String(12))
    sequence: Mapped[int]
    state: Mapped[Literal["open", "failed", "completed"]] = mapped_column(String(12))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(80))
    label: Mapped[str | None] = mapped_column(String(12))
    rubric: Mapped[AdoptionRubric | None] = mapped_column(JSON(none_as_null=True))
    scorer_label: Mapped[str | None] = mapped_column(String(80))
    isolation: Mapped[ScoringIsolation | None] = mapped_column(String(12))
    requires_verified: Mapped[bool] = mapped_column(server_default=text("false"))
    applied: Mapped[bool | None]
    request_id: Mapped[str | None] = mapped_column(String(128))
    judge_call_id: Mapped[UUID | None]


class RoutingRubric(TypedDict):
    id: str | None
    revision: int | None
    model: str | None
    tool: Literal["codex", "claude"] | None
    prompt: str
    labels: list[ScorerLabel]
    passing_score: float
    isolation: NotRequired[ScoringIsolation]


class RoutingEvidence(TypedDict):
    skill: DeliveredSkillEvidence
    used: list[str] | None
    evidence_hash: str
    rubric: RoutingRubric


class RoutingQuestion(TypedDict):
    name: str
    source_hash: str
    routing: RoutingEvidence


class KnowledgeRoutingAttempt(Base):
    """同一会话的一组路由问题和判定。后续重试沿用这一批固定题目。"""

    __tablename__ = "knowledge_routing_attempts"
    __table_args__ = (
        ForeignKeyConstraint(["archive_id", "archive_revision"], [
            "knowledge_shared_session_revisions.archive_id", "knowledge_shared_session_revisions.revision",
        ]),
        UniqueConstraint("series_id", "sequence", name="uq_knowledge_routing_attempt_series"),
        CheckConstraint("state IN ('open', 'failed', 'completed')", name="ck_knowledge_routing_attempt_state"),
        CheckConstraint("sequence BETWEEN 1 AND 4", name="ck_knowledge_routing_attempt_sequence"),
        Index("ix_knowledge_routing_attempt_quota", "workspace_id", "judge_id", "started_at"),
        Index("ix_knowledge_routing_attempt_subject", "archive_id", "archive_revision", "judge_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    judge_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    archive_id: Mapped[UUID]
    archive_revision: Mapped[int]
    policy_id: Mapped[int] = mapped_column(ForeignKey("knowledge_scoring_policies.id"))
    series_id: Mapped[UUID]
    run_mode: Mapped[ArchiveRunMode] = mapped_column(String(12))
    sequence: Mapped[int]
    state: Mapped[Literal["open", "failed", "completed"]] = mapped_column(String(12))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(80))
    questions: Mapped[list[RoutingQuestion]] = mapped_column(JSON())
    labels: Mapped[dict[str, str] | None] = mapped_column(JSON(none_as_null=True))
    requires_verified: Mapped[bool] = mapped_column(server_default=text("false"))


class KnowledgeNamedScoreCall(Base):
    """点名调用的幂等收据，保留普通标签与生成问题的完整判定。"""

    __tablename__ = "knowledge_named_score_calls"
    __table_args__ = (
        ForeignKeyConstraint(["archive_id", "archive_revision"], [
            "knowledge_shared_session_revisions.archive_id", "knowledge_shared_session_revisions.revision",
        ]),
        Index("ix_knowledge_named_score_call_usage", "workspace_id", "judge_id", "created_at"),
        Index("ix_knowledge_named_score_call_subject", "archive_id", "archive_revision", "created_at"),
    )

    judge_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    archive_id: Mapped[UUID]
    archive_revision: Mapped[int]
    request_hash: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    items: Mapped[list[dict[str, object]]] = mapped_column(JSON())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeNamespaceManifest(Base):
    """追加目录选择策略，保留每个版本的操作者及生效内容。"""

    __tablename__ = "knowledge_namespace_manifests"
    __table_args__ = (CheckConstraint("revision > 0", name="ck_knowledge_namespace_revision"),)

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    manifest: Mapped[dict[str, object]] = mapped_column(JSON())
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeHintPolicy(Base):
    """成员读取最新一版提醒设置，历史版本记录管理员的变更。"""

    __tablename__ = "knowledge_hint_policies"
    __table_args__ = (
        CheckConstraint("lookback_days BETWEEN 1 AND 90", name="ck_knowledge_hint_days"),
        CheckConstraint("max_mrs BETWEEN 1 AND 100", name="ck_knowledge_hint_max"),
        Index("ix_knowledge_hint_policies_workspace_version", "workspace_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    enabled: Mapped[bool]
    lookback_days: Mapped[int]
    max_mrs: Mapped[int]
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeSessionPolicy(Base):
    """各项会话策略独立追加版本，保留更改人和时间。"""

    __tablename__ = "knowledge_session_policies"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('recall', 'share-reminder', 'recall-adoption', 'usage-report', 'plan-reminder')",
            name="ck_knowledge_session_policy_kind",
        ),
        CheckConstraint(
            "(kind = 'plan-reminder' OR (plan_language IS NULL AND plan_message IS NULL)) "
            "AND (plan_language IS NULL OR plan_language IN ('zh', 'en'))",
            name="ck_knowledge_session_plan_reminder_options",
        ),
        Index("ix_knowledge_session_policy_workspace_version", "workspace_id", "kind", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    kind: Mapped[SessionPolicyKind] = mapped_column(String(20))
    enabled: Mapped[bool]
    plan_language: Mapped[PlanReminderLanguage | None] = mapped_column(String(2))
    plan_message: Mapped[str | None] = mapped_column(String(4000))
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeProjectRecallPolicy(Base):
    """同一项目的设置逐次追加，成员读取最新一次变更。"""

    __tablename__ = "knowledge_project_recall_policies"
    __table_args__ = (Index("ix_knowledge_project_recall_version", "workspace_id", "project_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    inherit_shared: Mapped[bool]
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeSessionActivity(Base):
    """按租户、项目、成员及根会话生成不可直接还原的计数主键。"""

    __tablename__ = "knowledge_session_activity"
    __table_args__ = (
        Index("ix_knowledge_activity_scope", "workspace_id", "project_id"),
        Index("ix_knowledge_activity_stop_day", "workspace_id", "project_id", "first_stop_day"),
        CheckConstraint("run_mode IN ('attended', 'unattended', 'not_recorded')", name="ck_knowledge_activity_mode"),
        CheckConstraint(
            "interrupts >= 0 AND tool_rejections >= 0 AND corrections >= 0", name="ck_knowledge_activity_counts",
        ),
        CheckConstraint(
            "prompts >= 0 AND input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_creation_tokens >= 0", name="ck_knowledge_activity_conversation_counts",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    run_mode: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interrupts: Mapped[int]
    tool_rejections: Mapped[int]
    corrections: Mapped[int]
    prompts: Mapped[int | None]
    tokens_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_tokens: Mapped[int | None] = mapped_column(BigInteger())
    output_tokens: Mapped[int | None] = mapped_column(BigInteger())
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger())
    cache_creation_tokens: Mapped[int | None] = mapped_column(BigInteger())
    first_stop_day: Mapped[date | None] = mapped_column(Date())
    active_ms: Mapped[int | None] = mapped_column(BigInteger())
    trend_error: Mapped[bool | None]


class KnowledgeSessionRequestDay(Base):
    """只保存某个根会话一日的计价数量与总金额，不存请求明细。"""

    __tablename__ = "knowledge_session_request_days"
    __table_args__ = (
        CheckConstraint("priced_requests > 0 AND cost_micros >= 0", name="ck_knowledge_request_day_values"),
        Index("ix_knowledge_request_day", "day"),
    )

    activity_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_session_activity.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date(), primary_key=True)
    priced_requests: Mapped[int]
    cost_micros: Mapped[int] = mapped_column(BigInteger())


class KnowledgeSessionOutcome(Base):
    """活动计数继续更新，已结算的产出及当时用量保持原样。"""

    __tablename__ = "knowledge_session_outcomes"
    __table_args__ = (
        Index("ix_knowledge_outcome_scope", "workspace_id", "project_id"),
        CheckConstraint("NOT has_pr OR has_commit", name="ck_knowledge_outcome_pr_has_commit"),
    )

    activity_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_session_activity.id"), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"))
    run_mode: Mapped[str] = mapped_column(String(16))
    stopped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    has_commit: Mapped[bool]
    has_pr: Mapped[bool]
    has_learning: Mapped[bool]
    input_tokens: Mapped[int | None] = mapped_column(BigInteger())
    output_tokens: Mapped[int | None] = mapped_column(BigInteger())
    interventions: Mapped[int]


class KnowledgeWorkflowAttempt(Base):
    """GitHub 的同一逻辑运行可有多个不可变终态，周报取最大尝试序号。"""

    __tablename__ = "knowledge_workflow_attempts"
    __table_args__ = (
        Index(
            "ix_knowledge_workflow_attempt_scope", "workspace_id", "project_id", "repository_id", "run_id", "attempt",
        ),
        CheckConstraint(
            "attempt > 0 AND connection_version > 0 AND duration_ms >= 0", name="ck_knowledge_workflow_attempt_counts",
        ),
        CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'cancelled', 'skipped')", name="ck_knowledge_workflow_attempt_outcome",
        ),
        CheckConstraint("outcome <> 'skipped' OR duration_ms = 0", name="ck_knowledge_workflow_skipped_duration"),
    )

    repository_id: Mapped[UUID] = mapped_column(ForeignKey("project_repositories.id"), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    attempt: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    connection_version: Mapped[int]
    reported_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    workflow: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(16))
    duration_ms: Mapped[int]


class KnowledgeCodebase(Base):
    """代码证据基线对应授权仓库连接或项目内的本地快照。"""

    __tablename__ = "knowledge_codebases"
    __table_args__ = (
        UniqueConstraint("repository_id", "connection_version", name="uq_knowledge_codebase_connection"),
        UniqueConstraint("project_id", "local_owner_id", "local_key", name="uq_knowledge_codebase_local"),
        CheckConstraint(
            "(repository_id IS NOT NULL AND connection_version IS NOT NULL "
            "AND local_owner_id IS NULL AND local_key IS NULL AND local_name IS NULL) OR "
            "(repository_id IS NULL AND connection_version IS NULL "
            "AND local_owner_id IS NOT NULL AND local_key IS NOT NULL AND local_name IS NOT NULL)",
            name="ck_knowledge_codebase_source",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    repository_id: Mapped[UUID | None] = mapped_column(ForeignKey("project_repositories.id"))
    connection_version: Mapped[int | None]
    local_owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    local_key: Mapped[str | None] = mapped_column(String(64))
    local_name: Mapped[str | None] = mapped_column(String(160))
    local_checkout: Mapped[dict[str, str] | None] = mapped_column(JSON(none_as_null=True))
    revision: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int]
    extraction_version: Mapped[int]
    max_files: Mapped[int]
    candidates: Mapped[int]
    manifest: Mapped[dict[str, FileFacts]] = mapped_column(JSON())
    # 命名空间随已登记来源固定，路径别名指向同一版本。
    wiki_namespaces: Mapped[list[str]] = mapped_column(JSON(), default=list, server_default=text("'[]'"))
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    indexed_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))


class KnowledgeCiExtraction(Base):
    """以登记仓库、评审编号和源提交保存不可变提案与合并处理结果。"""

    __tablename__ = "knowledge_ci_extractions"
    __table_args__ = (
        UniqueConstraint(
            "repository_id", "review_key", "head_commit", "connection_version", name="uq_knowledge_ci_source",
        ),
        Index(
            "uq_knowledge_ci_merge", "repository_id", "review_key",
            unique=True,
            postgresql_where=text("merge_commit IS NOT NULL"), sqlite_where=text("merge_commit IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("project_repositories.id"))
    connection_version: Mapped[int]
    # 仓库登记切换到其他地址后，新的同编号评审有自己的命名空间。
    review_key: Mapped[str] = mapped_column(String(64))
    review_url: Mapped[str] = mapped_column(String(1000))
    head_commit: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text())
    tags: Mapped[list[str]] = mapped_column(JSON())
    review_author: Mapped[str | None] = mapped_column(String(100))
    staged_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    staged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stage_request_id: Mapped[str | None] = mapped_column(String(128))
    # 租约跨同一评审的多个源提交互斥，过期后允许恢复远程评论。
    comment_lease_id: Mapped[UUID | None]
    comment_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 合并的字段在一次事务内结算。合并前为 NULL，重放读取原结果。
    merge_commit: Mapped[str | None] = mapped_column(String(64))
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_learning: Mapped[bool | None] = mapped_column(Boolean())
    rejected_codebase: Mapped[bool | None] = mapped_column(Boolean())
    completed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    complete_request_id: Mapped[str | None] = mapped_column(String(128))
    entry_id: Mapped[UUID | None] = mapped_column(ForeignKey("knowledge_entries.id"))


class KnowledgeCodebaseReview(Base):
    """记录触发仓库索引的评审链接及当时实际采集的提交。"""

    __tablename__ = "knowledge_codebase_reviews"

    codebase_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_codebases.id"), primary_key=True)
    url: Mapped[str] = mapped_column(String(1000), primary_key=True)
    revision: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    indexed_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)


class KnowledgeCodebaseImport(Base):
    """按仓库连接和操作者保存已完成的批量索引请求，网络重试可重放结果。"""

    __tablename__ = "knowledge_codebase_imports"

    codebase_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_codebases.id"), primary_key=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, object]] = mapped_column(JSON())
