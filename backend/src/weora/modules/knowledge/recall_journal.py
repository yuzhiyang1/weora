"""记录召回过程，只留会话摘要键、命中版本和引用 ID。"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import case, cast, delete, func, insert, or_, select, true, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from weora.modules.knowledge.models import (
    KnowledgeRecall,
    KnowledgeRecallRun,
    KnowledgeRecallRunHit,
    KnowledgeSkillRecall,
)

RETENTION = timedelta(days=30)
RECENT_WINDOW = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class RecalledItem:
    entry_id: UUID
    revision: int
    score: float
    relevant: bool
    eligible: bool
    related: list[str]
    skill_version_id: UUID | None = None


def session_digest(session: str) -> str:
    return hashlib.sha256(session.encode()).hexdigest()


class RecallJournal:
    def __init__(self, db: AsyncSession, workspace: UUID, project: UUID | None, user: UUID) -> None:
        self.db, self.workspace, self.project, self.user = db, workspace, project, user

    def _scope(self) -> tuple[ColumnElement[bool], ...]:
        run = KnowledgeRecallRun
        return run.workspace_id == self.workspace, run.project_id == self.project, run.user_id == self.user

    async def record(
        self, session: str, items: Sequence[RecalledItem], request_id: str | None, *, recall_actor: UUID | None = None,
    ) -> str:
        """空召回也记一次，正文和原始查询不进入日志。"""
        run = KnowledgeRecallRun(
            id=uuid4(), workspace_id=self.workspace, project_id=self.project, user_id=self.user,
            session_key=session_digest(session), request_id=request_id[:128] if request_id else None,
            recall_actor_digest=session_digest(str(recall_actor)) if recall_actor else None,
            hit_count=len(items), top_score=items[0].score if items else 0.0, created_at=datetime.now(UTC),
        )
        self.db.add(run)
        if items:
            await self.db.flush([run])
            await self.db.execute(insert(KnowledgeRecallRunHit), [
                dict(run_id=run.id, position=position, entry_id=item.entry_id, revision=item.revision,
                     score=item.score, relevant=item.relevant, eligible=item.eligible, related=item.related,
                     skill_version_id=item.skill_version_id)
                for position, item in enumerate(items)
            ])
        return str(run.id)

    def _recent(self, session: str, now: datetime) -> tuple[ColumnElement[bool], ...]:
        run = KnowledgeRecallRun
        return (*self._scope(), run.session_key == session_digest(session),
                run.created_at >= now - RECENT_WINDOW, run.created_at <= now)

    async def can_adopt(
        self, session: str, entry_id: UUID, actor: UUID, *,
        revision: int | None = None, skill_version_id: UUID | None = None,
    ) -> bool:
        """自己的专用召回不能自证采纳，其他有效命中仍可作为同次会话的证据。"""
        run, hit = KnowledgeRecallRun, KnowledgeRecallRunHit
        own = run.recall_actor_digest == session_digest(str(actor))
        version = hit.skill_version_id == skill_version_id if skill_version_id else hit.revision == revision
        counts = select(func.count().filter(own), func.count().filter(own.is_not(True))).select_from(run)
        self_reads, other_reads = (await self.db.execute(counts.join(hit, hit.run_id == run.id).where(
            *self._recent(session, datetime.now(UTC)), hit.entry_id == entry_id, hit.eligible, version,
        ))).one()
        return not self_reads or bool(other_reads)

    async def opened(
        self, session: str, entry_id: UUID, *,
        skill_version_id: UUID | None = None, actor: UUID | None = None,
    ) -> None:
        """只有打开了历史输出实际展示的关系目标，才为该次召回记一次关系使用。"""
        run, hit = KnowledgeRecallRun, KnowledgeRecallRunHit
        target = str(entry_id)
        if self.db.get_bind().dialect.name == "postgresql":
            references = cast(hit.related, JSONB).contains([target])
        else:
            values = func.json_each(hit.related).table_valued("value")
            references = select(values.c.value).where(values.c.value == target).exists()
        appeared = select(hit.run_id).where(hit.run_id == run.id, references).exists()
        now = datetime.now(UTC)
        actors = run.recall_actor_digest.is_distinct_from(session_digest(str(actor))) if actor else true()
        # 固定多行加锁顺序，两个相关条目并发打开时只更新各次召回一次。
        rows = await self.db.scalars(select(run).where(
            *self._recent(session, now), actors, run.via_relation_at.is_(None), appeared,
        ).order_by(run.id).with_for_update())
        for row in rows:
            row.via_relation_at = now
        if skill_version_id is not None:
            # Registry 采用固定版本身份，打开新版本不能替代旧命中的采纳记录。
            owned_runs = select(run.id).where(*self._recent(session, now), actors)
            await self.db.execute(update(hit).where(
                hit.run_id.in_(owned_runs), hit.entry_id == entry_id,
                hit.skill_version_id == skill_version_id, hit.eligible, hit.skill_opened_at.is_(None),
            ).values(skill_opened_at=now))

    async def stats(self, session: str | None, limit: int, *, recent: bool) -> dict[str, object]:
        run, hit, feedback = KnowledgeRecallRun, KnowledgeRecallRunHit, KnowledgeRecall
        skill_feedback = KnowledgeSkillRecall
        cutoff = datetime.now(UTC) - (RECENT_WINDOW if recent else RETENTION)
        statement = select(
            run.id, run.session_key, run.created_at, run.hit_count, run.top_score, run.via_relation_at,
        ).where(*self._scope(), run.created_at >= cutoff)
        if session is not None:
            statement = statement.where(run.session_key == session_digest(session))
        owned = statement.cte("owned_recall_runs")
        latest = func.max(owned.c.created_at).label("last")
        logs = select(
            owned.c.session_key, func.count().label("runs"),
            func.count().filter(owned.c.hit_count > 0).label("hits"),
            func.count().filter(owned.c.hit_count == 0).label("misses"),
            func.max(owned.c.top_score).label("top_score"), latest,
            func.count().filter(owned.c.via_relation_at.is_not(None)).label("via_relation"),
        ).group_by(owned.c.session_key).order_by(latest.desc(), owned.c.session_key).limit(limit).cte("shown_sessions")
        eligible_vote = hit.eligible & or_(
            feedback.positive.is_(True) & feedback.credit_source.in_(("opened", "judge")),
            hit.skill_version_id.is_not(None) & hit.skill_opened_at.is_not(None)
            & (skill_feedback.credit_source.is_distinct_from("manual")
               | skill_feedback.credited_version_id.is_distinct_from(hit.skill_version_id)),
        )
        docs = select(
            owned.c.session_key, func.count(func.distinct(hit.entry_id)).label("recalled"),
            func.count(func.distinct(case((eligible_vote, hit.entry_id)))).label("adopted"),
        ).select_from(owned).join(hit, hit.run_id == owned.c.id).join(
            logs, logs.c.session_key == owned.c.session_key,
        ).outerjoin(
            feedback, (feedback.entry_id == hit.entry_id) & (feedback.user_id == self.user)
            & (feedback.session_key == owned.c.session_key),
        ).outerjoin(
            skill_feedback, (skill_feedback.entry_id == hit.entry_id) & (skill_feedback.user_id == self.user)
            & (skill_feedback.session_key == owned.c.session_key),
        ).group_by(owned.c.session_key).cte("session_doc_counts")
        # 一条 SQL 共享 MVCC 快照，避免新召回在两次聚合之间改变分母。
        rows = (await self.db.execute(select(
            *logs.c, func.coalesce(docs.c.recalled, 0).label("recalled"),
            func.coalesce(docs.c.adopted, 0).label("adopted"),
        ).outerjoin(docs, docs.c.session_key == logs.c.session_key).order_by(
            logs.c.last.desc(), logs.c.session_key,
        ))).mappings()
        return {"items": [
            {
                "session_ref": row["session_key"], "runs": row["runs"], "hits": row["hits"], "misses": row["misses"],
                "top_score": round(row["top_score"], 4), "last": row["last"].isoformat(),
                "via_relation": row["via_relation"], "recalled": row["recalled"], "adopted": row["adopted"],
            }
            for row in rows
        ]}


async def purge_recall_journal(db: AsyncSession, *, batch: int = 5000) -> int:
    """一次清理有界批次，失败后由下一次定时任务继续。"""
    run = KnowledgeRecallRun
    expired = select(run.id).where(run.created_at < datetime.now(UTC) - RETENTION).order_by(
        run.created_at, run.id,
    ).limit(batch)
    if db.get_bind().dialect.name == "postgresql":
        expired = expired.with_for_update(skip_locked=True)
    removed = await db.scalars(delete(run).where(run.id.in_(expired)).returning(run.id))
    return len(list(removed))
