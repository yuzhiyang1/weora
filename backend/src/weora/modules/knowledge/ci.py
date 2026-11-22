"""处理一次评审的 CI 草稿和合并结果。来源地址只匹配登记仓库。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Select, or_, select

from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.models import KnowledgeCiExtraction
from weora.modules.knowledge.review_source import registered_repo, review_identity
from weora.modules.knowledge.store import Contribution, KnowledgeStore
from weora.modules.projects.domain import ProjectActor
from weora.modules.projects.service import ProjectService


@dataclass(frozen=True)
class CiProposal:
    """来自 CI 提炼的单条经验，以及它所基于的固定评审内容。"""

    url: str
    head_commit: str
    title: str
    body: str
    tags: list[str]
    review_author: str | None


@dataclass(frozen=True)
class CiMerge:
    """受授权任务从托管平台取得的合并事实和两个独立的评审结论。"""

    commit: str
    merged_at: datetime
    rejected_learning: bool
    rejected_codebase: bool
    comment_lease_id: UUID | None = None


def _conflict(message: str) -> KnowledgeError:
    return KnowledgeError("KNOWLEDGE_CI_CONFLICT", message, 409)


def _view(row: KnowledgeCiExtraction) -> dict[str, object]:
    return {
        "id": row.id,
        "repository_id": row.repository_id,
        "source": row.review_url,
        "head_commit": row.head_commit,
        "status": "merged" if row.merge_commit is not None else "proposed",
        "learning": {"title": row.title, "body": row.body, "tags": row.tags, "review_author": row.review_author},
        "entry_id": row.entry_id,
        "merge_commit": row.merge_commit,
        "rejected_learning": row.rejected_learning,
        "rejected_codebase": row.rejected_codebase,
    }


class CiReview:
    """隔离未合并建议，合并时复用知识发布及审计事务。"""

    def __init__(self, store: KnowledgeStore, projects: ProjectService) -> None:
        self.store = store
        self.projects = projects
        self.scope = store.scope
        self.actor = ProjectActor(self.scope.user, self.scope.workspace)

    def _project(self) -> UUID:
        self.store.require_reviewer()
        if self.scope.project is None:
            raise KnowledgeError("KNOWLEDGE_CI_PROJECT_REQUIRED", "CI 知识提炼需要指定项目。", 422)
        return self.scope.project

    def _scoped(self, project: UUID) -> Select[tuple[KnowledgeCiExtraction]]:
        return select(KnowledgeCiExtraction).where(
            KnowledgeCiExtraction.workspace_id == self.scope.workspace,
            KnowledgeCiExtraction.project_id == project,
        )

    async def _target(self, project: UUID, url: str) -> tuple[UUID, int, str]:
        identity = review_identity(url)
        if identity is None:
            raise KnowledgeError("KNOWLEDGE_CI_INVALID_SOURCE", "请输入受支持的 PR/MR 来源地址。", 422)
        key, number = identity
        addresses = await self.projects.active_repository_addresses(self.actor, project, self.store.db)
        connections = dict(self.scope.code_connections)
        candidates = [
            repo for repo, address in addresses.items() if repo in connections and registered_repo(address) == key
        ]
        if len(candidates) != 1:
            raise _conflict("评审必须唯一匹配此项目的一个已登记仓库。")
        repo = candidates[0]
        stable_key = hashlib.sha256(json.dumps((key, number)).encode()).hexdigest()
        return repo, connections[repo], stable_key

    async def preview_source(self, url: str) -> tuple[UUID, int]:
        """图谱预览和提炼必须匹配同一个已授权的项目仓库。"""
        project = self._project()
        repo, version, _ = await self._target(project, url)
        return repo, version

    def _review(self, project: UUID, repo: UUID, review_key: str) -> Select[tuple[KnowledgeCiExtraction]]:
        return self._scoped(project).where(
            KnowledgeCiExtraction.repository_id == repo,
            KnowledgeCiExtraction.review_key == review_key,
        )

    async def lookup(self, url: str, head: str) -> dict[str, object]:
        project = self._project()
        repo, version, review_key = await self._target(project, url)
        row = await self.store.db.scalar(self._proposal(project, repo, review_key, head, version))
        if row is None:
            return {"status": "missing", "repository_id": repo}
        return _view(row)

    def _proposal(
        self, project: UUID, repo: UUID, review_key: str, head: str, version: int,
    ) -> Select[tuple[KnowledgeCiExtraction]]:
        # 换凭据后可重新提案，已合并的版本始终重放原收据。
        return self._review(project, repo, review_key).where(
            KnowledgeCiExtraction.head_commit == head,
            or_(KnowledgeCiExtraction.merge_commit.is_not(None), KnowledgeCiExtraction.connection_version == version),
        ).order_by(KnowledgeCiExtraction.merge_commit.is_(None)).limit(1)

    async def propose(self, proposal: CiProposal, request_id: str | None) -> dict[str, object]:
        project = self._project()
        repo, version, review_key = await self._target(project, proposal.url)
        db = self.store.db
        await self.projects.lock_active_repository(self.actor, project, repo, version, db)
        review = self._review(project, repo, review_key)
        previous = await db.scalar(self._proposal(project, repo, review_key, proposal.head_commit, version))
        if previous is not None:
            # 同一提交重复提炼时保留先发布的评审建议，避免已展示正文发生变动。
            return _view(previous)
        if await db.scalar(review.where(KnowledgeCiExtraction.merge_commit.is_not(None))):
            raise _conflict("此评审已由另一源提交完成合并处理。")
        row = KnowledgeCiExtraction(
            id=uuid4(), workspace_id=self.scope.workspace, project_id=project, repository_id=repo,
            connection_version=version, review_key=review_key, review_url=proposal.url,
            head_commit=proposal.head_commit,
            title=proposal.title, body=proposal.body, tags=proposal.tags, review_author=proposal.review_author,
            staged_by=self.scope.user, staged_at=datetime.now(UTC), stage_request_id=request_id,
        )
        db.add(row)
        await db.flush()
        return _view(row)

    async def _locked_extraction(
        self, project: UUID, extraction_id: UUID,
    ) -> tuple[KnowledgeCiExtraction, int]:
        db = self.store.db
        row = await db.scalar(self._scoped(project).where(KnowledgeCiExtraction.id == extraction_id))
        if row is None:
            raise KnowledgeError("KNOWLEDGE_CI_NOT_FOUND", "提炼记录不存在或无权访问。", 404)
        repo, version, review_key = await self._target(project, row.review_url)
        if repo != row.repository_id or review_key != row.review_key:
            raise _conflict("仓库登记已变化，请重新从源发起 CI 提炼。")
        await self.projects.lock_active_repository(self.actor, project, repo, version, db)
        await db.refresh(row)
        return row, version

    async def _active_lease(self, row: KnowledgeCiExtraction, version: int) -> KnowledgeCiExtraction | None:
        leases = await self.store.db.scalars(self._review(row.project_id, row.repository_id, row.review_key).where(
            KnowledgeCiExtraction.connection_version == version,
            KnowledgeCiExtraction.comment_lease_until > datetime.now(UTC),
        ))
        return leases.one_or_none()

    async def comment_lease(self, extraction_id: UUID, lease: UUID, *, release: bool) -> dict[str, object]:
        """同一评审的远程评论操作互斥。重试续期须持有原租约。"""
        project = self._project()
        row, version = await self._locked_extraction(project, extraction_id)
        if release:
            if row.comment_lease_id == lease:
                row.comment_lease_id, row.comment_lease_until = None, None
            return {"status": "released"}
        if row.connection_version != version:
            raise _conflict("仓库连接已变化，请重新从源发起 CI 提炼。")
        active = await self._active_lease(row, version)
        if active is not None and (active.id != row.id or active.comment_lease_id != lease):
            raise _conflict("已有任务在处理该评审的评论，请稍后重试。")
        row.comment_lease_id, row.comment_lease_until = lease, datetime.now(UTC) + timedelta(minutes=5)
        return {"status": "acquired"}

    async def finish(self, extraction_id: UUID, merge: CiMerge, request_id: str | None) -> dict[str, object]:
        project = self._project()
        db = self.store.db
        row, version = await self._locked_extraction(project, extraction_id)
        if row.merge_commit is not None:
            if (
                row.merge_commit != merge.commit or row.rejected_learning != merge.rejected_learning
                or row.rejected_codebase != merge.rejected_codebase or _utc(row.merged_at) != merge.merged_at
            ):
                raise _conflict("合并结果已记录，请沿用原合并信息。")
            return _view(row)
        if row.connection_version != version:
            raise _conflict("仓库连接已变化，请重新从源发起 CI 提炼。")
        active = await self._active_lease(row, version)
        if active is not None and (active.id != row.id or active.comment_lease_id != merge.comment_lease_id):
            raise _conflict("已有任务在处理该评审的评论，请稍后重试。")
        if merge.comment_lease_id is not None and active is None:
            raise _conflict("读取评论后租约已过期，请重跑以重新核对表态。")
        other = await db.scalar(self._review(project, row.repository_id, row.review_key).where(
            KnowledgeCiExtraction.merge_commit.is_not(None),
        ))
        if other is not None:
            raise _conflict("此评审已由另一源提交完成合并处理。")
        if not merge.rejected_learning:
            source = {
                "type": "pull_request", "ref": row.review_url, "review_date": merge.merged_at.isoformat(),
                "review_head": row.head_commit, "merge_commit": merge.commit,
            }
            if row.review_author is not None:
                source["review_author"] = row.review_author
            published = await self.store.contribute(
                Contribution(row.id, "learning", row.title, row.body, row.tags, source, []), publish_mr=True,
            )
            row.entry_id = UUID(str(published["id"]))
        row.merge_commit, row.merged_at = merge.commit, merge.merged_at
        row.rejected_learning, row.rejected_codebase = merge.rejected_learning, merge.rejected_codebase
        row.completed_by, row.completed_at, row.complete_request_id = self.scope.user, datetime.now(UTC), request_id
        await db.flush()
        return _view(row)


def _utc(value: datetime | None) -> datetime | None:
    # SQLite 的测试时刻没有 tzinfo， PostgreSQL 会返回带时区的值。
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value
