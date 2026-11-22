"""仅为请求者计算本次召回中的有效反馈，不写入反馈账本。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, Uuid, and_, case, cast, func, literal, or_, select, union_all
from sqlalchemy.sql.selectable import CTE

from weora.modules.knowledge.feedback_order import FEEDBACK_CLOCK_SKEW
from weora.modules.knowledge.models import KnowledgeSkillFeedbackEvent
from weora.modules.knowledge.recall_journal import session_digest
from weora.modules.skills.infrastructure.knowledge_catalog import skill_release_times


@dataclass(frozen=True, slots=True)
class PendingFeedback:
    entry: UUID
    project: UUID | None
    session: str
    positive: bool
    operation: UUID
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class FeedbackProjection:
    user: UUID
    project: UUID | None
    pending: list[PendingFeedback]



def projected_history(targets: CTE, history: CTE, feedback: FeedbackProjection) -> CTE | None:
    """仅合并已授权目标，在同一条 SQL 中比较顺序与回执。"""
    now = datetime.now(UTC)
    timestamps = DateTime(timezone=True)
    delivery = [
        select(
            literal(vote.entry).label("entry_id"), literal(session_digest(vote.session)).label("session_key"),
            literal(vote.positive).label("positive"), literal(vote.operation).label("operation"),
            literal(vote.occurred_at.astimezone(UTC), timestamps).label("occurred_at"),
            literal(min(vote.occurred_at, now).astimezone(UTC), timestamps).label("voted_at"),
            literal(vote.project is None).label("shared_only"),
        )
        for vote in feedback.pending
        if (vote.project is None or vote.project == feedback.project) and vote.occurred_at <= now + FEEDBACK_CLOCK_SKEW
    ]
    if not delivery:
        return None

    queued = union_all(*delivery).cte("queued_recall_feedback")
    authorized = select(targets.c.id, targets.c.project_id, targets.c.skill_version_id).where(
        ~targets.c.hidden,
    ).cte("authorized_feedback_targets")
    releases = skill_release_times().subquery()
    audit = KnowledgeSkillFeedbackEvent
    applied = select(audit.id).where(
        audit.entry_id == authorized.c.id, audit.user_id == feedback.user,
        audit.session_key == queued.c.session_key, audit.operation_id == queued.c.operation,
    ).correlate(authorized, queued).exists()
    ordered = select(*queued.c, func.row_number().over(
        partition_by=(queued.c.entry_id, queued.c.session_key),
        order_by=(queued.c.occurred_at.desc(), queued.c.operation.desc()),
    ).label("position")).join_from(queued, authorized, authorized.c.id == queued.c.entry_id).where(
        ~queued.c.shared_only | authorized.c.project_id.is_(None),
        authorized.c.skill_version_id.is_(None) | select(releases.c.id).where(
            releases.c.skill_id == authorized.c.id, releases.c.published_at <= queued.c.occurred_at,
        ).correlate(authorized, queued).exists(),
        ~applied,
    ).cte("ordered_recall_feedback")

    row = history.c
    same_key = and_(
        row.entry_id == ordered.c.entry_id, row.user_id == feedback.user, row.session_key == ordered.c.session_key,
    )
    confirmed_at = func.coalesce(row.manual_vote_at, case((row.credit_source == "manual", row.voted_at)))
    fresher = or_(
        confirmed_at.is_(None), ordered.c.occurred_at > confirmed_at,
        (ordered.c.occurred_at == confirmed_at)
        & (row.manual_vote_id.is_(None) | (ordered.c.operation > row.manual_vote_id)),
    )
    # 同方向的补发只推动顺序，采纳活动时间沿用已确认记录。
    voted_at = case(
        ((row.credit_source == "manual") & (row.positive == ordered.c.positive), row.voted_at),
        else_=ordered.c.voted_at,
    )
    # 明确账号列类型，避免 PostgreSQL 将独立的绑定值推断成文本。
    replacement = select(
        ordered.c.entry_id, cast(literal(feedback.user), Uuid()).label("user_id"), ordered.c.session_key,
        func.coalesce(row.recall_count, 0).label("recall_count"), row.last_recalled_at, ordered.c.positive,
        voted_at.label("voted_at"), literal("manual").label("credit_source"),
    ).outerjoin_from(ordered, history, same_key).where(
        ordered.c.position == 1, row.manual_vote_id.is_distinct_from(ordered.c.operation), fresher,
    ).cte("pending_feedback_replacement")

    replaced = select(replacement.c.entry_id).where(
        replacement.c.entry_id == row.entry_id,
        replacement.c.user_id == row.user_id,
        replacement.c.session_key == row.session_key,
    ).exists()
    unchanged = select(
        row.entry_id, row.user_id, row.recall_count, row.last_recalled_at,
        row.positive, row.voted_at, row.credit_source,
    ).where(~replaced)
    return unchanged.union_all(select(
        replacement.c.entry_id, replacement.c.user_id, replacement.c.recall_count, replacement.c.last_recalled_at,
        replacement.c.positive, replacement.c.voted_at, replacement.c.credit_source,
    )).cte("recall_feedback_with_pending")
