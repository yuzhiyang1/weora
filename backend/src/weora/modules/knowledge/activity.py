"""发布按项目隔离的会话计数，周报只读取团队聚合。"""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import KnowledgeSessionActivity
from weora.modules.knowledge.session_trends import TrendInput, record_request_days
from weora.modules.knowledge.store import KnowledgeStore, Scope


class ConversationTokens(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: int = Field(strict=True, ge=0, le=10**15)
    output: int = Field(strict=True, ge=0, le=10**15)
    cache_read: int = Field(strict=True, ge=0, le=10**15)
    cache_creation: int = Field(strict=True, ge=0, le=10**15)


def activity_id(scope: Scope, runtime: str, subject: str) -> UUID:
    identity = f"weora:activity:{scope.workspace}:{scope.project}:{scope.user}:{runtime}:{subject}"
    return uuid5(NAMESPACE_URL, identity)


async def record_session(
    store: KnowledgeStore, *, runtime: str, subject: str, run_mode: str,
    interrupts: int, tool_rejections: int, corrections: int, observed_at: datetime,
    prompts: int | None, tokens: ConversationTokens | None, trend: TrendInput | None,
) -> dict[str, object]:
    # 同一根会话恢复后会重新上报累计值。设备重放和并发提交不能重复计数。
    await store.require_usage_reporting()
    now = datetime.now(UTC)
    if trend is not None and (
        trend.first_stop_day > observed_at.astimezone(UTC).date()
        or any(day.day > now.date() for day in trend.request_days)
    ):
        raise KnowledgeError("KNOWLEDGE_USAGE_TREND_DAY", "趋势或请求日期超出可观测时间。", 422)
    if observed_at > now + timedelta(seconds=60):
        raise KnowledgeError("KNOWLEDGE_USAGE_OBSERVED_AT", "会话结束时间超出当前时间。", 422)
    scope, row = store.scope, KnowledgeSessionActivity
    identifier = activity_id(scope, runtime, subject)
    insert = sqlite_insert if store.db.get_bind().dialect.name == "sqlite" else pg_insert
    values = {"interrupts": interrupts, "tool_rejections": tool_rejections, "corrections": corrections}
    if prompts is not None:
        values["prompts"] = prompts
    if trend is not None:
        values["active_ms"] = trend.active_ms
    updates = {
        name: case((or_(getattr(row, name).is_(None), getattr(row, name) < value), value), else_=getattr(row, name))
        for name, value in values.items()
    }
    token_values = {}
    if tokens is not None:
        token_values = {
            "input_tokens": tokens.input, "output_tokens": tokens.output,
            "cache_read_tokens": tokens.cache_read, "cache_creation_tokens": tokens.cache_creation,
            "tokens_observed_at": observed_at,
        }
        # 文件级估计换成线程级快照时，缓存桶也可能变小。必须整组替换，禁止拼接不同观测的最大值。
        newer = or_(row.tokens_observed_at.is_(None), row.tokens_observed_at < observed_at)
        updates.update({name: case((newer, value), else_=getattr(row, name)) for name, value in token_values.items()})
    trend_values = {}
    if trend is not None:
        trend_values = {"first_stop_day": trend.first_stop_day, "trend_error": trend.has_error}
        earlier = or_(row.first_stop_day.is_(None), row.first_stop_day > trend.first_stop_day)
        updates.update(
            first_stop_day=case((earlier, trend.first_stop_day), else_=row.first_stop_day),
            trend_error=case((row.trend_error.is_(True), True), else_=trend.has_error),
        )
    statement = insert(row).values(
        id=identifier, workspace_id=scope.workspace, project_id=scope.project, user_id=scope.user,
        run_mode=run_mode, observed_at=observed_at, **values, **token_values, **trend_values,
    )
    await store.db.execute(statement.on_conflict_do_update(
        index_elements=[row.id],
        set_=updates | {
            "run_mode": case((row.observed_at < observed_at, run_mode), else_=row.run_mode),
            "observed_at": case((row.observed_at < observed_at, observed_at), else_=row.observed_at),
        },
    ))
    if trend is not None:
        await record_request_days(store.db, identifier, trend.request_days)
    return {"status": "recorded"}


async def intervention_digest(db: AsyncSession, scope: Scope) -> dict[str, object] | None:
    row = KnowledgeSessionActivity
    statement = select(
        row.run_mode, func.count(), func.sum(row.interrupts), func.sum(row.tool_rejections), func.sum(row.corrections),
    ).where(row.workspace_id == scope.workspace, row.project_id == scope.project).group_by(row.run_mode)
    groups = (await db.execute(statement)).tuples().all()
    if not groups:
        return None
    modes = {"attended": 0, "unattended": 0, "not_recorded": 0}
    interrupts = tool_rejections = corrections = 0
    for mode, count, interrupted, rejected, corrected in groups:
        modes[mode] = count
        interrupts += interrupted
        tool_rejections += rejected
        corrections += corrected
    sessions = sum(modes.values())
    total = interrupts + tool_rejections + corrections
    return {
        "sessions": sessions, "total": total, "avg_per_session": total / sessions, "interrupts": interrupts,
        "tool_rejections": tool_rejections, "corrections": corrections, "by_run_mode": modes,
    }


async def conversation_digest(db: AsyncSession, scope: Scope) -> dict[str, object] | None:
    row = KnowledgeSessionActivity
    groups = (await db.execute(select(
        row.run_mode, func.sum(row.prompts), func.count(row.input_tokens),
        func.sum(row.input_tokens), func.sum(row.output_tokens),
        func.sum(row.cache_read_tokens), func.sum(row.cache_creation_tokens),
    ).where(
        row.workspace_id == scope.workspace, row.project_id == scope.project,
    ).group_by(row.run_mode))).tuples()
    modes: tuple[Literal["attended", "unattended", "not_recorded"], ...] = (
        "attended", "unattended", "not_recorded",
    )
    prompts_by_mode: dict[str, int] = dict.fromkeys(modes, 0)
    tokens_by_mode: dict[str, int] = dict.fromkeys(modes, 0)
    measured = 0
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    for mode, prompts, measured_count, *token_sums in groups:
        prompts_by_mode[mode] = prompts or 0
        measured += measured_count
        for key, count in zip(totals, token_sums, strict=True):
            units = int(count or 0)
            totals[key] += units
            tokens_by_mode[mode] += units
    if not measured and not any(prompts_by_mode.values()):
        return None
    return {
        "prompts": sum(prompts_by_mode.values()), "prompts_by_run_mode": prompts_by_mode,
        "token_sessions": measured, "total_tokens": sum(totals.values()) if measured else None,
        "tokens": totals if measured else None, "tokens_by_run_mode": tokens_by_mode if measured else None,
    }
