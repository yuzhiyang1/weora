"""维护、召回排序和报告共用同一个采纳统计口径。"""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.engine import RowMapping
from sqlalchemy.sql.elements import ColumnElement

from weora.modules.knowledge.domain import FeedbackTotals
from weora.modules.knowledge.models import KnowledgeRecall, KnowledgeSkillRecall


def recall_aggregates(
    history: type[KnowledgeRecall] | type[KnowledgeSkillRecall] = KnowledgeRecall,
) -> tuple[ColumnElement[int] | ColumnElement[datetime | None], ...]:
    vote = history.positive.is_(True)
    judge = history.credit_source == "judge"
    return (
        func.coalesce(func.sum(history.recall_count), 0).label("recalls"),
        func.count().filter(history.recall_count > 0).label("sessions"),
        func.count().filter(vote).label("upvotes"),
        func.count(func.distinct(history.user_id)).filter(vote).label("voters"),
        func.count(func.distinct(history.user_id)).filter(history.recall_count > 0).label("recallers"),
        func.max(history.last_recalled_at).label("last_recalled"),
        func.max(history.voted_at).filter(vote).label("last_upvote"),
        func.count().label("observations"),
        func.count().filter(judge).label("judged"),
        func.count().filter(judge & vote).label("judge_upvotes"),
    )


def feedback_totals(row: RowMapping) -> FeedbackTotals:
    activity = max(filter(None, (row["last_recalled"], row["last_upvote"])), default=None)
    return FeedbackTotals(
        row["recalls"] or 0, row["sessions"] or 0, row["upvotes"] or 0, row["voters"] or 0, activity,
        row["recallers"] or 0, row["observations"] or 0, row["judged"] or 0, row["judge_upvotes"] or 0,
    )
