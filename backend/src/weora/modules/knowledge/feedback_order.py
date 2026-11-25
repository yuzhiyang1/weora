"""知识条目和独立 Skill 共用的手工反馈顺序。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import KnowledgeRecall, KnowledgeSkillRecall

FEEDBACK_CLOCK_SKEW = timedelta(minutes=5)


def vote_time(value: datetime, now: datetime) -> datetime:
    timestamp = value.astimezone(UTC)
    if timestamp > now + FEEDBACK_CLOCK_SKEW:
        raise KnowledgeError("KNOWLEDGE_FEEDBACK_CLOCK", "反馈发生时间晚于服务端时间，请校准设备时钟。", 422)
    return timestamp


def apply_manual_vote(
    row: KnowledgeRecall | KnowledgeSkillRecall, positive: bool, operation: UUID | None,
    occurred_at: datetime, now: datetime,
) -> tuple[bool, bool]:
    """返回是否采用本次顺序，以及决定或证据来源是否改变。"""
    occurred_at = vote_time(occurred_at, now)
    latest = row.manual_vote_at
    if latest is None and row.credit_source == "manual":
        # 兼容补发排序发布前保存的手工反馈。
        latest = row.voted_at
    if latest is not None:
        latest = latest.astimezone(UTC) if latest.tzinfo else latest.replace(tzinfo=UTC)
    if operation is not None and row.manual_vote_id == operation:
        if latest != occurred_at or row.positive != positive:
            raise KnowledgeError("KNOWLEDGE_CONFLICT", "同一反馈操作标识已用于其他反馈内容。", 409)
        return False, False
    previous_id = row.manual_vote_id.int if row.manual_vote_id else 0
    sequence = operation.int if operation is not None else 0
    if latest is not None and (occurred_at, sequence) <= (latest, previous_id):
        return False, False
    row.manual_vote_at, row.manual_vote_id = occurred_at, operation
    changed = row.positive != positive or row.credit_source != "manual"
    if changed:
        row.positive, row.credit_source, row.voted_at = positive, "manual", min(occurred_at, now)
    return True, changed
