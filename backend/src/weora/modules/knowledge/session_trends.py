"""按 UTC 首次 Stop 和请求发生日读取相邻的团队活动窗口。"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.models import KnowledgeSessionActivity, KnowledgeSessionRequestDay
from weora.modules.knowledge.store import Scope


class RequestDayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: date
    priced_requests: int = Field(strict=True, ge=1, le=1_000_000)
    cost_micros: int = Field(strict=True, ge=0, le=10**15)


class TrendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first_stop_day: date
    active_ms: int = Field(strict=True, ge=0, le=10**12)
    has_error: bool = Field(strict=True)
    request_days: list[RequestDayInput] = Field(default_factory=list, max_length=31)

    @model_validator(mode="after")
    def distinct_days(self) -> "TrendInput":
        if len({request.day for request in self.request_days}) != len(self.request_days):
            raise ValueError("同一个 UTC 日期只能上报一次。")
        return self


async def record_request_days(db: AsyncSession, identifier: UUID, days: list[RequestDayInput]) -> None:
    if not days:
        return
    row = KnowledgeSessionRequestDay
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    # 日期累计保持幂等。更新成本和请求数须使用同一份较完整的快照。
    statement = insert(row).values([
        {"activity_id": identifier, **day.model_dump()} for day in sorted(days, key=lambda day: day.day)
    ])
    await db.execute(statement.on_conflict_do_update(
        index_elements=[row.activity_id, row.day],
        set_={"priced_requests": statement.excluded.priced_requests, "cost_micros": statement.excluded.cost_micros},
        where=row.priced_requests < statement.excluded.priced_requests,
    ))


@dataclass(slots=True)
class Period:
    sessions: int = 0
    succeeded: int = 0
    prompts: int = 0
    duration: int = 0
    corrected: int = 0
    cached: int = 0
    cache_eligible: int = 0
    priced: int = 0
    cost: int = 0
    interrupts: int = 0
    tool_rejections: int = 0
    corrections: int = 0
    affected: int = 0

    def view(self, first: date, last: date) -> dict[str, object]:
        return {
            "from_day": first.isoformat(), "through_day": last.isoformat(), "sessions_ended": self.sessions,
            "success_rate": self.succeeded / self.sessions if self.sessions else None,
            "avg_prompts": self.prompts / self.sessions if self.sessions else None,
            "avg_duration_ms": self.duration / self.sessions if self.sessions else None,
            "avg_request_cost_micros": self.cost / self.priced if self.priced else None,
            "cache_read_share": self.cached / self.cache_eligible if self.cache_eligible else None,
            "correction_rate": self.corrected / self.sessions if self.sessions else None,
            "priced_requests": self.priced,
        }


    def dashboard(self, first: date, last: date) -> dict[str, object]:
        """未采集到定价时保留未知值，零美元只用于实际采集到的零成本。"""
        length = (last - first).days + 1
        return self.view(first, last) | {
            "sessions_with_correction": self.corrected,
            "prompts": self.prompts,
            "known_cost_micros": self.cost if self.priced else None,
            "daily_known_cost_micros": self.cost / length if self.priced else None,
            "interrupts": self.interrupts,
            "tool_rejections": self.tool_rejections,
            "corrections": self.corrections,
            "sessions_with_intervention": self.affected,
        }


async def session_trends(
    db: AsyncSession, scope: Scope, now: datetime, *, days: int = 7, daily: bool = False,
) -> dict[str, object]:
    today = now.date()
    current_start = today - timedelta(days=days - 1)
    previous_start = current_start - timedelta(days=days)
    current, previous = Period(), Period()
    by_day = {current_start + timedelta(days=offset): Period() for offset in range(days)} if daily else {}
    members: dict[date, int] = {}
    activity, cost = KnowledgeSessionActivity, KnowledgeSessionRequestDay
    where = (activity.workspace_id == scope.workspace, activity.project_id == scope.project)
    groups = (await db.execute(select(
        activity.first_stop_day, func.count(),
        func.sum(case((activity.trend_error.is_(False) & (activity.interrupts == 0)
                       & (activity.corrections == 0), 1), else_=0)),
        func.sum(activity.prompts), func.sum(activity.active_ms),
        func.sum(case((activity.corrections > 0, 1), else_=0)),
        func.sum(activity.cache_read_tokens),
        func.sum(activity.input_tokens + activity.cache_read_tokens + activity.cache_creation_tokens),
        func.sum(activity.interrupts), func.sum(activity.tool_rejections), func.sum(activity.corrections),
        func.sum(case((activity.interrupts + activity.tool_rejections + activity.corrections > 0, 1), else_=0)),
        func.count(func.distinct(activity.user_id)),
    ).where(
        *where, activity.first_stop_day.between(previous_start, today),
    ).group_by(activity.first_stop_day))).tuples()
    for group in groups:
        (day, count, succeeded, prompts, duration, corrected, cached, eligible,
         interrupts, rejects, corrections, affected, active) = group
        if day is None:
            continue
        period = current if day >= current_start else previous
        members[day] = active
        for target in (period, by_day.get(day)):
            if target is None:
                continue
            target.sessions += count
            target.succeeded += succeeded
            target.prompts += prompts or 0
            target.duration += int(duration or 0)
            target.corrected += corrected
            target.cached += int(cached or 0)
            target.cache_eligible += int(eligible or 0)
            target.interrupts += int(interrupts or 0)
            target.tool_rejections += int(rejects or 0)
            target.corrections += int(corrections or 0)
            target.affected += int(affected or 0)

    # 成本按请求发生日汇总，跨午夜或跨周恢复的会话不把费用挪回首次 Stop。
    request_groups = (await db.execute(select(
        cost.day, func.sum(cost.priced_requests), func.sum(cost.cost_micros),
    ).join(activity, activity.id == cost.activity_id).where(
        *where, cost.day.between(previous_start, today),
    ).group_by(cost.day))).tuples()
    for day, requests, micros in request_groups:
        period = current if day >= current_start else previous
        for target in (period, by_day.get(day)):
            if target is not None:
                target.priced += int(requests or 0)
                target.cost += int(micros or 0)
    previous_end = current_start - timedelta(days=1)
    if daily:
        return {
            "current": current.dashboard(current_start, today),
            "previous": previous.dashboard(previous_start, previous_end),
            "daily": [
                {"day": day.isoformat(), "active_members": members.get(day, 0), **values.dashboard(day, day)}
                for day, values in by_day.items()
            ],
        }
    return {
        "current": current.view(current_start, today),
        "previous": previous.view(previous_start, previous_end),
    }
