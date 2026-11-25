"""结算已结束根会话的产出，按运行模式和产出类型读取冻结指标。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.activity import activity_id
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import (
    KnowledgeEntry,
    KnowledgeRevision,
    KnowledgeSessionActivity,
    KnowledgeSessionOutcome,
)
from weora.modules.knowledge.store import KnowledgeStore, Scope

_IDLE_FOR = timedelta(hours=6)
_KINDS = ("pr", "commit", "learning", "none")
_MODES = ("attended", "unattended", "not_recorded")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def finish_session(
    store: KnowledgeStore, *, runtime: str, subject: str, mode: str, stopped: datetime,
    has_commit: bool, has_pr: bool,
) -> dict[str, object]:
    await store.require_usage_reporting()
    scope = store.scope
    identifier = activity_id(scope, runtime, subject)
    activity = await store.db.scalar(select(KnowledgeSessionActivity).where(
        KnowledgeSessionActivity.id == identifier,
    ).with_for_update())
    if activity is None:
        raise KnowledgeError("KNOWLEDGE_OUTCOME_ACTIVITY_REQUIRED", "请先上报这次会话的活动统计。", 409)
    # 先检查已落库的回执，传输重试和之后的会话恢复都不改变第一次结算。
    if await store.db.get(KnowledgeSessionOutcome, identifier) is not None:
        return {"status": "already_recorded"}
    now = datetime.now(UTC)
    if (
        _utc(activity.observed_at) != stopped or activity.run_mode != mode or stopped > now
        or (mode != "unattended" and now - stopped < _IDLE_FOR)
    ):
        raise KnowledgeError("KNOWLEDGE_OUTCOME_NOT_FINISHED", "结束快照已变化或会话仍在等待结束。", 409)
    revision, entry = KnowledgeRevision, KnowledgeEntry
    learning = await store.db.scalar(
        select(revision.entry_id).join(entry, entry.id == revision.entry_id).where(
            revision.workspace_id == scope.workspace, entry.workspace_id == scope.workspace,
            entry.project_id == scope.project, entry.kind == "learning", revision.author_id == scope.user,
            revision.source_session_key == subject, revision.status == "published",
        ).limit(1)
    )
    store.db.add(KnowledgeSessionOutcome(
        activity_id=identifier, workspace_id=scope.workspace, project_id=scope.project, run_mode=mode,
        stopped_at=stopped, reported_at=now, has_commit=has_commit, has_pr=has_pr, has_learning=learning is not None,
        input_tokens=activity.input_tokens, output_tokens=activity.output_tokens,
        interventions=activity.interrupts + activity.tool_rejections + activity.corrections,
    ))
    return {"status": "recorded"}


@dataclass(slots=True)
class OutcomeTotals:
    sessions: int = 0
    measured: int = 0
    tokens: int = 0
    interventions: int = 0

    def view(self, mode: str, kind: str) -> dict[str, object]:
        return {
            "run_mode": mode, "outcome": kind, "sessions": self.sessions, "token_sessions": self.measured,
            "tokens_per_session": self.tokens / self.sessions if self.measured else None,
            "interventions_per_session": self.interventions / self.sessions,
        }


async def outcome_digest(db: AsyncSession, scope: Scope) -> list[dict[str, object]]:
    row = KnowledgeSessionOutcome
    grouped = (await db.execute(select(
        row.run_mode, row.has_pr, row.has_commit, row.has_learning, func.count(), func.count(row.input_tokens),
        func.sum(row.input_tokens), func.sum(row.output_tokens), func.sum(row.interventions),
    ).where(row.workspace_id == scope.workspace, row.project_id == scope.project).group_by(
        row.run_mode, row.has_pr, row.has_commit, row.has_learning,
    ))).tuples()
    totals: dict[tuple[str, str], OutcomeTotals] = {}
    for mode, pr, commit, learning, sessions, measured, input_tokens, output_tokens, interventions in grouped:
        markers = (pr, commit, learning, not (pr or commit or learning))
        for kind, present in zip(_KINDS, markers, strict=True):
            if not present:
                continue
            target = totals.setdefault((mode, kind), OutcomeTotals())
            target.sessions += sessions
            target.measured += measured
            target.tokens += int(input_tokens or 0) + int(output_tokens or 0)
            target.interventions += interventions
    return [counts.view(mode, kind) for mode in _MODES for kind in _KINDS
            if (counts := totals.get((mode, kind))) is not None]
