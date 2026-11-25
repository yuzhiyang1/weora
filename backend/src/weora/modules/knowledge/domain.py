"""团队知识的领域类型与 TeamAI 置信度规则。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict
from uuid import NAMESPACE_URL, UUID, uuid5

KnowledgeKind = Literal["learning", "rule", "doc", "code", "skill"]
RecallDepth = Literal["route", "context", "lookup"]
PromotionKind = Literal["rule", "skill", "doc"]
DEFAULT_PRUNE_THRESHOLD = 0.15
SessionContextPolicy = Literal["recall", "share-reminder", "recall-adoption"]
BooleanSessionPolicy = SessionContextPolicy | Literal["usage-report"]
SessionPolicyKind = BooleanSessionPolicy | Literal["plan-reminder"]
PlanReminderLanguage = Literal["zh", "en"]
ArchiveRunMode = Literal["attended", "unattended"]
ArchiveAction = Literal["off", "ask", "auto"]
ScoringIsolation = Literal["best-effort", "verified"]
RelationKind = Literal[
    "MAPS_TO", "REFERENCES", "SUPERSEDES", "CONFLICTS_WITH", "DEPENDS_ON", "CONTAINS", "IMPLEMENTS"
]


class PlanReminderChange(TypedDict, total=False):
    """省略字段时保留原策略，语言或正文显式传空时使用内置值。"""

    enabled: bool
    language: PlanReminderLanguage | None
    message: str | None


class PlanReminderSettings(TypedDict):
    enabled: bool
    language: PlanReminderLanguage | None
    message: str | None
    source: Literal["default", "workspace"]


def codebase_id(repository_id: UUID, connection_version: int) -> UUID:
    """登记连接更新时使用新的索引身份，旧证据仍属于此前连接。"""
    return uuid5(NAMESPACE_URL, f"weora:knowledge:repository:{repository_id}:{connection_version}")


def review_authored_day(source: dict[str, str]) -> datetime | None:
    """PR/MR 经验使用原评审的日期，与生成的 Markdown 保持一致。"""
    if source.get("type") != "pull_request" or len(value := source.get("review_date", "")) < 10:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


class KnowledgeError(Exception):
    """向调用方报告可预期的知识操作失败。"""

    def __init__(self, code: str, message: str, status: int) -> None:
        self.code, self.status = code, status
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AdoptionEvidence:
    """采纳判定使用的固定归档或本机快照及其采集时点。"""

    archive_id: UUID
    revision: int
    captured_at: datetime
    attempt_id: UUID | None = None
    local: bool = False
    judge_call_id: UUID | None = None


@dataclass(frozen=True)
class FeedbackTotals:
    recalls: int
    sessions: int
    upvotes: int
    voters: int
    last_activity: datetime | None
    recallers: int = 0
    observations: int = 0
    judged: int = 0
    judge_upvotes: int = 0

    def confidence(self, now: datetime) -> float:
        """沿用 TeamAI 的活动量、近期度和采纳率，保留两位小数。"""
        activity = self.last_activity
        if activity is not None and activity.tzinfo is None:
            activity = activity.replace(tzinfo=UTC)
        age = max(timedelta(), now - activity).total_seconds() / 86400 if activity else 180
        base = min(1.0, self.recalls * 0.1 + self.upvotes * 0.3)
        recency = max(0.0, 1 - age / 180)
        direct = min(1.0, max(0, self.upvotes - self.judge_upvotes) / max(1, self.sessions))
        judged = min(1.0, self.judge_upvotes / self.judged) if self.judged else 0
        adoption = direct + judged * (1 - direct)
        return int((base * 0.4 + recency * 0.3 + adoption * 0.3) * 100 + 0.5) / 100

    def can_promote(self, created_at: datetime, now: datetime) -> bool:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return (
            self.confidence(now) >= 0.9
            and self.upvotes >= 5
            and self.voters >= 2
            and now - created_at >= timedelta(days=14)
        )

    def needs_quality_revision(self) -> bool:
        """规则、文档和 Skill 采用 TeamAI 的改进候选阈值。"""
        return self.recalls >= 5 and self.upvotes <= 1 and self.recallers >= 2

    def prune_reason(
        self, created_at: datetime, now: datetime, threshold: float = DEFAULT_PRUNE_THRESHOLD,
    ) -> str | None:
        """新经验先积累真实反馈；旧经验使用 TeamAI 的第二条低置信度阈值。"""
        if not self.observations:
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        confidence = self.confidence(now)
        if confidence < threshold:
            return "low_confidence"
        if now - created_at > timedelta(days=180) and confidence < 0.3:
            return "stale_low_confidence"
        return None
