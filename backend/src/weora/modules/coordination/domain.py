"""按 Super-agent core/model、ports 翻译的协调契约；出处及 MIT 声明见源码对照文档。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

Json = dict[str, object]
Strength = Literal["explicit", "inferred"]
Mode = Literal["off", "observe", "auto"]
EffectKind = Literal["main", "worker", "poll", "code_poll", "reply", "cancel"]
EffectState = Literal["pending", "running", "done", "cancelled", "blocked", "unknown"]


class CoordinationError(Exception):
    """对外仅暴露稳定错误码，不回显 SQL、凭据或供应商错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DeferredEffect(CoordinationError):
    """并发槽已满等可恢复等待，保留待办而非伪造 Worker 失败。"""


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    instruction: str
    enabled: bool = True


@dataclass(frozen=True)
class RepositorySource:
    """管理员明确授权的仓库入口；外部账号逐一绑定平台成员，凭据始终留在连接服务。"""

    repository_id: str
    remote_id: str
    agent_login: str
    actors: dict[str, str]
    connection_fingerprint: str = ""
    project_snapshot: Json | None = None
    observe_issues: bool = False
    observe_pulls: bool = False
    observe_comments: bool = False
    observe_ci: bool = False
    started_at: str = ""


@dataclass(frozen=True)
class Config:
    id: str
    workspace_id: str
    created_by: str
    source_id: str
    main_agent_id: str
    main_agent_version: str
    worker_agent_id: str
    worker_agent_version: str
    allowed_actors: tuple[str, ...]
    rules: tuple[Rule, ...]
    enabled: bool = True
    mode: Mode = "observe"
    paused: bool = False
    observe_unmentioned: bool = True
    listed_unaddressed: Literal["inferred", "ignore"] = "inferred"
    unlisted_addressed: Literal["explicit", "inferred", "ignore"] = "ignore"
    unlisted_unaddressed: Literal["inferred", "ignore"] = "ignore"
    allowed_bots: tuple[str, ...] = ()
    max_workers: int = 3
    eval_timeout_seconds: int = 120
    debounce_ms: int = 250
    revision: int = 1
    project_id: str | None = None
    repository_ids: tuple[str, ...] = ()
    route_key: str | None = None
    project_grant_id: str | None = None
    # 只影响新会话和新接单；已存在会话、任务沿用冻结的选择。
    main_model_id: str | None = None
    worker_model_id: str | None = None
    source_kind: Literal["feishu", "code_hosting"] = "feishu"
    repository_source: RepositorySource | None = None


@dataclass
class Trigger:
    id: str
    workspace_id: str
    scope_id: str
    actor_id: str
    actor_open_id: str
    human: bool
    addressed: bool
    threads: list[str]
    at: str
    text: str
    origin: Json
    authorized: bool = True
    filtered: str | None = None
    fanout: list[str] = field(default_factory=list)
    context_only: list[str] = field(default_factory=list)
    handbacks: dict[str, str] = field(default_factory=dict)
    acknowledged: list[str] = field(default_factory=list)
    requeued: bool = False
    eval_id: str | None = None
    decision_id: str | None = None
    expires_at: str | None = None
    # 同一飞书话题的其他成员提供的证据，不能再进入新任务评估。
    peer_evidence: bool = False


@dataclass
class Evaluation:
    id: str
    thread_key: str
    trigger_ids: list[str]
    primary: str
    strength: Strength
    actor_id: str
    basis: Json
    policy_version: str
    started_at: str
    delivered_at: str | None = None
    deadline: str | None = None
    paused_at: str | None = None
    context_read: bool = False
    state: str = "pending"


@dataclass
class Thread:
    id: str
    created_at: str
    last_at: str
    pending: list[str] = field(default_factory=list)
    subscriptions: list[str] = field(default_factory=list)
    context_only: list[str] = field(default_factory=list)
    correlated: list[str] = field(default_factory=list)
    evaluation_id: str | None = None
    expired: int = 0
    due: str | None = None


@dataclass(frozen=True)
class Mandate:
    strength: Strength
    policy_version: str
    actor_id: str
    actor_open_id: str
    scope: str
    basis: Json
    rules: tuple[str, ...] = ()
    worker_agent_id: str = ""
    worker_agent_version: str = ""
    project_id: str | None = None
    repository_ids: tuple[str, ...] = ()
    project_grant_id: str | None = None
    route_key: str | None = None
    worker_model_id: str | None = None


@dataclass
class Decision:
    id: str
    eval_id: str
    thread_key: str
    trigger_ids: list[str]
    policy_version: str
    at: str
    kind: str
    reason: str
    strength: Strength
    audit: Json = field(default_factory=dict)
    ticket_id: str | None = None
    dismissed: bool = False


@dataclass
class Report:
    state: Literal["progress", "ready", "blocked", "failed", "dropped", "waiting"]
    summary: str
    at: str
    human_action: str | None = None
    question_trigger_id: str | None = None
    answers: str | None = None
    verified: bool = False
    human_action_supplied: bool = False


@dataclass
class Assignment:
    id: str
    created_at: str
    state: str = "pending"
    run_id: str | None = None
    ended_at: str | None = None
    end_reason: str | None = None
    idle_at: str | None = None
    report: Report | None = None
    delivered_at: str | None = None
    status_only: bool = False
    # 报告时已接收的输入数；后来的有效人类输入解除自动消息挂起，不依赖外部时间戳。
    report_input_count: int | None = None


@dataclass
class Input:
    id: str
    kind: str
    at: str
    trigger_id: str | None = None
    text: str | None = None
    human: bool = False
    context_only: bool = False
    delivered_to: str | None = None
    delivered_at: str | None = None
    read_at: str | None = None
    status: str = "pending"
    # 可交给原 Worker 核实，但不能解除人工等待或代表原发起人授权。
    peer_evidence: bool = False


@dataclass
class Follow:
    thread: str
    own: bool = False
    closed_at: str | None = None
    outcome: str | None = None
    # 只有代码评审等带关闭语义的线程参与目标完成；普通飞书话题没有“已关闭”事实。
    lifecycle: bool = False
    review_run_id: str | None = None
    review_id: str | None = None
    observed_at: str | None = None


@dataclass
class Ticket:
    id: str
    title: str
    decision_id: str
    mandate: Mandate
    origin_id: str
    origin_ids: list[str]
    created_at: str
    updated_at: str
    session_id: str
    task_id: str | None = None
    assignments: list[Assignment] = field(default_factory=list)
    inputs: list[Input] = field(default_factory=list)
    undelivered: list[str] = field(default_factory=list)
    follows: list[Follow] = field(default_factory=list)
    attention: str | None = None
    attention_id: str | None = None
    attention_trigger_id: str | None = None
    answers: list[str] = field(default_factory=list)
    ended_at: str | None = None
    ended_by: str | None = None
    outcome: str | None = None
    delivery_error: str | None = None
    status_requests: int = 0


@dataclass
class Reply:
    id: str
    trigger_id: str
    purpose: str
    text: str
    at: str
    ticket_id: str | None = None
    state: str = "pending"
    remote_message_id: str | None = None
    # 区分同一次在途发送与租约接管后的未知结果，不能仅凭 unknown 决定是否重发。
    sending_owner: str | None = None
    # 仅记录本任务真实截图的引用；远端图片标识由发送端上传后取得。
    artifact_ids: list[str] = field(default_factory=list)


@dataclass
class Notice:
    id: str
    kind: str
    at: str
    data: Json
    quiet: bool = False
    delivered_at: str | None = None
    read_at: str | None = None


@dataclass
class Session:
    id: str
    role: str
    generation: int = 0
    messages: list[Json] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    policy_version: str = ""
    owner: str | None = None
    lease_until: str | None = None
    model_retry: Json | None = None
    model_id: str | None = None
    # 只保存非敏感执行快照，密钥版本由后台装配按团队解析。
    execution_profile: Json | None = None
    runtime_id: str | None = None
    # 父会话发给子会话的受限指令，读取回执后移除；不视为新的人类授权。
    pending_messages: list[Json] = field(default_factory=list)


@dataclass
class Effect:
    id: str
    kind: EffectKind
    business_key: str
    data: Json
    priority: int = 0
    generation: int = 1
    state: EffectState = "pending"
    owner: str | None = None
    lease_until: str | None = None
    attempts: int = 0
    error: str | None = None
    available_at: str | None = None
    queued_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def is_owned_by(self, owner: str | None, now: str) -> bool:
        """判断调用方是否仍持有有效租约；新消息增加 generation 不撤销当前执行权。"""
        return bool(
            owner is not None
            and self.owner == owner
            and self.state == "running"
            and self.lease_until
            and self.lease_until > now
        )

    def is_dispatchable(self, now: str) -> bool:
        """判断是否到达派发时间；已领取的工作仅在租约失效后重新派发。"""
        if self.available_at and self.available_at > now:
            return False
        return self.state == "pending" or bool(self.state == "running" and self.lease_until and self.lease_until <= now)


@dataclass
class Activity:
    """当前 Run 内的后台工作；结果丢失只标未知，不根据旧指令重新执行外部动作。"""

    id: str
    run_id: str
    kind: Literal["command", "subagent"]
    owner: str
    created_at: str
    state: Literal["running", "completed", "failed", "cancelled", "unknown"] = "running"
    result: Json = field(default_factory=dict)
    read: bool = False
    parent_id: str | None = None


@dataclass
class State:
    """一次事务的工作副本；出事务前整体加密，数据库不保存消息或报告明文。"""

    config: Config
    triggers: list[Trigger] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    evaluations: list[Evaluation] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    tickets: list[Ticket] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    checkpoints: Json = field(default_factory=dict)
    activities: list[Activity] = field(default_factory=list)

    def require_effect_lease(self, attempt: Effect, now: str) -> None:
        """在事务内校验当前尝试的执行权，供模型工具和外部副作用共用同一防线。"""
        current = next((item for item in self.effects if item.id == attempt.id), None)
        if not current or not current.is_owned_by(attempt.owner, now):
            raise CoordinationError("SESSION_LEASE_LOST")


class Repository[T](Protocol):
    def get(self, key: str) -> T | None: ...
    def exists(self, key: str) -> bool: ...
    def save(self, value: T) -> None: ...
    def list(self) -> list[T]: ...


class EffectQueue(Protocol):
    def enqueue(self, kind: EffectKind, key: str, data: Json, *, priority: int = 0) -> None: ...
    def cancel(self, kind: EffectKind, key: str) -> None: ...


class Clock(Protocol):
    def __call__(self) -> str: ...


@dataclass
class Records:
    triggers: Repository[Trigger]
    threads: Repository[Thread]
    evaluations: Repository[Evaluation]
    decisions: Repository[Decision]
    tickets: Repository[Ticket]
    replies: Repository[Reply]
    notices: Repository[Notice]
    sessions: Repository[Session]
    effects: EffectQueue
