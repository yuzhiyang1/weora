"""将独立 Skill 的召回和手工反馈写入知识账本。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.feedback_order import apply_manual_vote, vote_time
from weora.modules.knowledge.models import KnowledgeRecallRun, KnowledgeRecallRunHit, KnowledgeSkillFeedbackEvent
from weora.modules.knowledge.models import KnowledgeSkillRecall as SkillRecall
from weora.modules.knowledge.recall_journal import RECENT_WINDOW, session_digest
from weora.modules.skills.infrastructure.knowledge_catalog import (
    PublishedSkillReference,
    published_skill_reference,
    skill_release_times,
)


class SkillFeedback:
    def __init__(
        self, db: AsyncSession, workspace: UUID, project: UUID | None, user: UUID,
        directories: tuple[str, ...] | None = None,
    ) -> None:
        self.db, self.workspace, self.project, self.user = db, workspace, project, user
        self.directories = directories

    async def _lock(self, entry: UUID) -> PublishedSkillReference | None:
        # 与发布、归档使用同一资产行锁，避免把反馈写给刚撤下的发布版本。
        return await published_skill_reference(
            self.db, self.workspace, self.project, identity=entry, lock=True, directories=self.directories,
        )

    async def _row(self, entry: UUID, session_key: str) -> SkillRecall:
        row = await self.db.get(SkillRecall, (entry, self.user, session_key))
        if row is None:
            row = SkillRecall(entry_id=entry, user_id=self.user, session_key=session_key, recall_count=0)
            self.db.add(row)
        return row

    async def returned(self, reference: PublishedSkillReference, session: str) -> bool:
        """确认当前发布指针后计数；已换版的查询结果不再进入本次日志。"""
        current = await self._lock(reference.id)
        if current is None or current.version_id != reference.version_id:
            return False
        row = await self._row(reference.id, session_digest(session))
        row.recall_count += 1
        row.last_recalled_at = datetime.now(UTC)
        if reference.project_id == self.project:
            row.recalled_version_id = reference.version_id
        return True

    async def opened(self, reference: PublishedSkillReference, session: str) -> None:
        """调用方已经锁定当前版本。只采纳同一会话实际返回的同一版本。"""
        if reference.project_id != self.project:
            return
        row = await self.db.get(SkillRecall, (reference.id, self.user, session_digest(session)))
        if row is None or row.positive is not None or row.recalled_version_id != reference.version_id:
            return
        last = row.last_recalled_at
        now = datetime.now(UTC)
        if last is None:
            return
        last = last.astimezone(UTC) if last.tzinfo else last.replace(tzinfo=UTC)
        if now - RECENT_WINDOW <= last <= now:
            row.positive, row.credit_source, row.voted_at = True, "opened", now
            row.credited_version_id = reference.version_id

    async def vote(
        self, entry: UUID, session: str, positive: bool, operation: UUID | None, occurred_at: datetime | None,
    ) -> bool:
        reference = await self._lock(entry)
        if reference is None:
            return False
        now = datetime.now(UTC)
        timestamp = vote_time(occurred_at or now, now)
        key = session_digest(session)
        if operation is not None:
            previous = await self.db.scalar(select(KnowledgeSkillFeedbackEvent).where(
                KnowledgeSkillFeedbackEvent.entry_id == entry, KnowledgeSkillFeedbackEvent.user_id == self.user,
                KnowledgeSkillFeedbackEvent.session_key == key, KnowledgeSkillFeedbackEvent.operation_id == operation,
            ))
            if previous is not None:
                before = previous.occurred_at
                before = before.astimezone(UTC) if before.tzinfo else before.replace(tzinfo=UTC)
                if previous.project_id != self.project or previous.positive != positive or before != timestamp:
                    raise KnowledgeError("KNOWLEDGE_CONFLICT", "同一反馈操作标识已用于其他反馈内容。", 409)
                return True
        row = await self._row(entry, key)
        accepted, _ = apply_manual_vote(row, positive, operation, timestamp, now)
        if not accepted:
            return True
        version = await self._version_at(entry, key, timestamp)
        if version is None:
            raise KnowledgeError("KNOWLEDGE_CONFLICT", "反馈时间没有对应的 Skill 发布版本。", 409)
        row.credited_version_id = version
        self.db.add(KnowledgeSkillFeedbackEvent(
            id=uuid4(), workspace_id=self.workspace, project_id=self.project,
            entry_id=entry, user_id=self.user, version_id=version, session_key=key,
            operation_id=operation, positive=positive, occurred_at=timestamp, received_at=now,
        ))
        return True

    async def _version_at(self, entry: UUID, key: str, timestamp: datetime) -> UUID | None:
        run, hit = KnowledgeRecallRun, KnowledgeRecallRunHit
        # 先用该用户实际看见的版本。旧召回已清理时按反馈发生时可用的发布版本归因。
        result = await self.db.scalar(select(hit.skill_version_id).join(run, run.id == hit.run_id).where(
            run.workspace_id == self.workspace, run.project_id == self.project,
            run.user_id == self.user, run.session_key == key, run.created_at <= timestamp,
            hit.entry_id == entry, hit.skill_version_id.is_not(None),
        ).order_by(run.created_at.desc(), run.id.desc()).limit(1))
        if result is not None:
            return result
        releases = skill_release_times().subquery()
        return await self.db.scalar(select(releases.c.id).where(
            releases.c.skill_id == entry, releases.c.published_at <= timestamp,
        ).order_by(releases.c.published_at.desc(), releases.c.id.desc()).limit(1))
