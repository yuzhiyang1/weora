"""按工作空间和项目存取知识版本，维护审核和召回记录。"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from typing import cast as type_cast
from uuid import UUID, uuid4, uuid5

from pydantic import JsonValue
from sqlalchemy import Select, String, cast, false, func, literal, or_, select, true, tuple_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CTE

from weora.modules.identity.infrastructure.unit_of_work import SqlAlchemyIdentityStore
from weora.modules.knowledge.adoption_rubric import adoption_question, render_adoption_rubric
from weora.modules.knowledge.cross_repository import PublishedCrossEdge, published_cross_repository_edges
from weora.modules.knowledge.domain import (
    DEFAULT_PRUNE_THRESHOLD,
    AdoptionEvidence,
    BooleanSessionPolicy,
    FeedbackTotals,
    KnowledgeError,
    KnowledgeKind,
    PlanReminderChange,
    PlanReminderSettings,
    PromotionKind,
    RecallDepth,
    SessionPolicyKind,
    codebase_id,
    review_authored_day,
)
from weora.modules.knowledge.feedback_order import apply_manual_vote
from weora.modules.knowledge.feedback_projection import FeedbackProjection, PendingFeedback
from weora.modules.knowledge.markdown_paths_v1 import declared_path, entry_path
from weora.modules.knowledge.markdown_references import (
    declaration_findings,
    index_markdown,
    markdown_relations,
    saved_declarations,
)
from weora.modules.knowledge.models import (
    KnowledgeCodebase,
    KnowledgeConfidenceSnapshot,
    KnowledgeEntry,
    KnowledgeEvent,
    KnowledgeHintPolicy,
    KnowledgeMarkdownPath,
    KnowledgeProjectRecallPolicy,
    KnowledgeRecall,
    KnowledgeRecallRun,
    KnowledgeRecallRunHit,
    KnowledgeRevision,
    KnowledgeSessionPolicy,
    KnowledgeSessionScorer,
    KnowledgeSharedSessionRevision,
)
from weora.modules.knowledge.namespace_paths import namespace_visible, selected_folders
from weora.modules.knowledge.namespace_policy import NamespaceSelection
from weora.modules.knowledge.overlap import LOOKBACK, MIN_OVERLAP, keywords, overlap
from weora.modules.knowledge.path_declarations import Declaration, declarations
from weora.modules.knowledge.promotion import promotion_path, promotion_targets, publish_knowledge_skill, suggested_kind
from weora.modules.knowledge.queued_recall import QueuedLearning, QueueRecall, resolve_queue
from weora.modules.knowledge.recall_change_files import change_evidence, source_groups
from weora.modules.knowledge.recall_check import describe_check, empty_check
from weora.modules.knowledge.recall_code import RelatedFiles, code_route, context_graph, is_context_page, lookup_graph
from weora.modules.knowledge.recall_gaps import published_gaps
from weora.modules.knowledge.recall_journal import RecalledItem, RecallJournal, session_digest
from weora.modules.knowledge.recall_metrics import feedback_totals, recall_aggregates
from weora.modules.knowledge.reconciliation import (
    active_mappings,
    build_report,
    changed_products,
    is_code,
    is_product,
    replace_mappings,
    serialize_publication,
)
from weora.modules.knowledge.registry_publication import KNOWLEDGE_SKILL_PUBLICATION
from weora.modules.knowledge.registry_quality import (
    REGISTRY_QUALITY_SOURCE,
    pending_skill_proposals,
    publish_registry_quality,
    registry_quality_catalog,
    registry_quality_feedback,
    registry_skill_totals,
)
from weora.modules.knowledge.registry_skills import skill_markdown, skill_markdown_parts, standalone_skill_catalog
from weora.modules.knowledge.resource_paths import normalize_resource_path, resource_key
from weora.modules.knowledge.rule_paths import derived_path_edges, markdown_rule_paths
from weora.modules.knowledge.scoring_policy import adoption_override
from weora.modules.knowledge.search import SearchHit, add_indexed_revisions, order_hits, rank_published
from weora.modules.knowledge.session_keys import learning_session_key
from weora.modules.knowledge.skill_feedback import SkillFeedback
from weora.modules.knowledge.skill_releases import REGISTRY_VERSION, REGISTRY_VERSION_ID
from weora.modules.knowledge.target_reservations import lock_knowledge_targets
from weora.modules.skills.application.service import SkillActor
from weora.modules.skills.domain.errors import SkillRegistryError
from weora.modules.skills.infrastructure.binding import scoped_skill_registry
from weora.modules.skills.infrastructure.knowledge_catalog import (
    PublishedSkillReference,
    SkillSearchView,
    published_skill_reference,
    skill_search_views,
)
from weora.shared.object_storage import ObjectStoragePort
from weora_code_graph.coverage import GAP_PAGE
from weora_code_graph.iwiki_links import CodeEvidence, CodeLink
from weora_code_graph.reconcile import Page

_ADOPTION_WINDOW = timedelta(hours=24)


class ProjectRecallSettings(TypedDict):
    inherit_shared: bool
    revision: int


@dataclass(frozen=True)
class Scope:
    workspace: UUID
    project: UUID | None
    user: UUID
    role: str
    code_connections: tuple[tuple[UUID, int], ...] = ()
    namespaces: NamespaceSelection | None = None
    hidden_wikis: tuple[UUID, ...] = ()
    device_id: UUID | None = None
    include_shared: bool = True

    @property
    def can_review(self) -> bool:
        return self.role in {"owner", "admin"}

    @property
    def skill_directories(self) -> tuple[str, ...] | None:
        return self.namespaces.skills if self.namespaces is not None else None


@dataclass(frozen=True)
class Contribution:
    operation_id: UUID
    kind: KnowledgeKind
    title: str
    body: str
    tags: list[str]
    source: dict[str, str]
    relations: list[dict[str, str]]
    promoted_from: UUID | None = None
    base_revision: int | None = None
    rule_paths: list[str] | None = None
    iwiki_terms: tuple[str, ...] = ()
    related: JsonValue = None
    authored_at: datetime | None = None

    def digest(self, scope: Scope, entry_id: UUID | None) -> str:
        payload = (
            scope.project, entry_id, self.kind, self.title, self.body, self.tags,
            self.source, self.relations, self.promoted_from, self.base_revision,
        )
        # 旧贡献没有路径字段，保持已发送请求的摘要可以重放。
        content = (*payload, self.rule_paths) if self.rule_paths is not None else payload
        if self.iwiki_terms:
            content = (*content, {"iwiki_terms": self.iwiki_terms})
        if self.related is not None:
            ordered = list(self.related.items()) if isinstance(self.related, dict) else self.related
            content = (*content, {"related": ordered})
        if self.authored_at is not None:
            content = (*content, {"authored_at": self.authored_at.isoformat()})
        return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


def missing() -> KnowledgeError:
    return KnowledgeError("KNOWLEDGE_NOT_FOUND", "知识条目不存在或无权访问。", 404)


def conflict(message: str) -> KnowledgeError:
    return KnowledgeError("KNOWLEDGE_CONFLICT", message, 409)


def visible(scope: Scope) -> ColumnElement[bool]:
    shared = KnowledgeEntry.project_id.is_(None) & KnowledgeEntry.codebase_id.is_(None)
    if scope.project is None:
        return shared
    ids = [codebase_id(repo, version) for repo, version in scope.code_connections]
    locals_in_project = select(KnowledgeCodebase.id).where(
        KnowledgeCodebase.workspace_id == scope.workspace,
        KnowledgeCodebase.project_id == scope.project,
        KnowledgeCodebase.local_key.is_not(None),
    )
    allowed_code = or_(
        KnowledgeEntry.codebase_id.is_(None), KnowledgeEntry.codebase_id.in_(ids),
        KnowledgeEntry.codebase_id.in_(locals_in_project),
    )
    if scope.hidden_wikis:
        allowed_code &= or_(KnowledgeEntry.codebase_id.is_(None), KnowledgeEntry.codebase_id.not_in(scope.hidden_wikis))
    current = (KnowledgeEntry.project_id == scope.project) & allowed_code
    return or_(shared, current) if scope.include_shared else current


def published(scope: Scope) -> Select[tuple[KnowledgeEntry, KnowledgeRevision]]:
    return (
        select(KnowledgeEntry, KnowledgeRevision)
        .join(
            KnowledgeRevision,
            (KnowledgeRevision.entry_id == KnowledgeEntry.id)
            & (KnowledgeRevision.revision == KnowledgeEntry.published_revision),
        )
        .where(
            KnowledgeEntry.workspace_id == scope.workspace, visible(scope), KnowledgeEntry.status == "active",
            namespace_visible(scope.namespaces),
        )
    )


def lifecycle_projects(scope: Scope, include_shared: bool) -> ColumnElement[bool]:
    primary = KnowledgeEntry.project_id == scope.project
    return or_(primary, KnowledgeEntry.project_id.is_(None)) if include_shared and scope.project else primary


def lifecycle_date() -> ColumnElement[datetime]:
    """原文未标注日期的历史条目沿用首次入库时间。"""
    return func.coalesce(KnowledgeRevision.authored_at, KnowledgeEntry.created_at)


def lifecycle_published(scope: Scope, include_shared: bool) -> Select[tuple[KnowledgeEntry, KnowledgeRevision]]:
    rows = published(scope).where(lifecycle_projects(scope, include_shared))
    return rows.where(~overridden(scope)) if include_shared else rows


def overridden(scope: Scope) -> ColumnElement[bool]:
    """项目同路径资源与命名空间规则可覆盖共享资源。"""
    if not scope.include_shared:
        return false()
    directory_rule = _project_rule_replaces_root(scope) if scope.project or scope.namespaces else false()
    shared = KnowledgeEntry.project_id.is_(None) & KnowledgeEntry.codebase_id.is_(None)
    if scope.project is None:
        return shared & (KnowledgeEntry.kind == "rule") & directory_rule
    inherited_path = aliased(KnowledgeMarkdownPath)
    # 与按路径打开共用逻辑路径，贡献中的扩展名和分隔符写法不影响覆盖关系。
    inherited_key = select(inherited_path.key).where(
        inherited_path.entry_id == KnowledgeEntry.id,
        inherited_path.revision == KnowledgeEntry.published_revision,
        inherited_path.position == 0,
    ).correlate(KnowledgeEntry).scalar_subquery()
    project = aliased(KnowledgeEntry)
    project_path = aliased(KnowledgeMarkdownPath)
    replacement = select(project.id).join(project_path, (
        (project_path.entry_id == project.id) & (project_path.revision == project.published_revision)
    )).where(
        project.workspace_id == scope.workspace,
        project.project_id == scope.project,
        project.kind == KnowledgeEntry.kind,
        project_path.position == 0,
        project_path.key == inherited_key,
        project.status == "active",
        project.codebase_id.is_(None),
    ).correlate(KnowledgeEntry)
    return shared & or_(
        replacement.exists(), (KnowledgeEntry.kind == "rule") & directory_rule,
    )


def _project_rule_replaces_root(scope: Scope) -> ColumnElement[bool]:
    """TeamAI 的规则覆盖只匹配 rules/<namespace>/<文件> 与 rules/<文件>。"""
    root = aliased(KnowledgeMarkdownPath)
    nested = aliased(KnowledgeMarkdownPath)
    candidate = aliased(KnowledgeEntry)
    filename = func.substr(root.path, len("rules/") + 1)
    suffix = func.substr(nested.path, func.length(nested.path) - func.length(filename))
    allowed = candidate.project_id == scope.project
    selected: ColumnElement[bool] = true()
    if scope.namespaces is not None:
        allowed = or_(allowed, candidate.project_id.is_(None))
        selected = selected_folders(nested.path, "rules", scope.namespaces.knowledge)
    return select(root.entry_id).select_from(root).join(nested, suffix == literal("/").concat(filename)).join(
        candidate, (candidate.id == nested.entry_id) & (candidate.published_revision == nested.revision),
    ).where(
        root.entry_id == KnowledgeEntry.id, root.revision == KnowledgeEntry.published_revision,
        root.position == 0, root.path.like("rules/%"), ~root.path.like("rules/%/%"),
        nested.position == 0, nested.path.like("rules/%/%"), ~nested.path.like("rules/%/%/%"),
        candidate.workspace_id == scope.workspace, allowed, selected, candidate.kind == "rule",
        candidate.status == "active", candidate.codebase_id.is_(None),
    ).correlate(KnowledgeEntry).exists()


class KnowledgeStore:
    def __init__(self, db: AsyncSession, scope: Scope, storage: ObjectStoragePort | None = None) -> None:
        self.db = db
        self.scope = scope
        self.storage = storage
        self.journal = RecallJournal(db, scope.workspace, scope.project, scope.user)
        self.skill_feedback = SkillFeedback(db, scope.workspace, scope.project, scope.user, scope.skill_directories)

    def _recall_project(self) -> UUID:
        if self.scope.project is None:
            raise KnowledgeError("KNOWLEDGE_INVALID_SCOPE", "召回继承设置需要指定项目。", 422)
        return self.scope.project

    async def project_recall_settings(self) -> ProjectRecallSettings:
        project = self._recall_project()
        policy = KnowledgeProjectRecallPolicy
        latest = await self.db.scalar(select(policy).where(
            policy.workspace_id == self.scope.workspace, policy.project_id == project,
        ).order_by(policy.id.desc()).limit(1))
        if latest is None:
            return {"inherit_shared": False, "revision": 0}
        return {"inherit_shared": latest.inherit_shared, "revision": latest.id}

    async def set_project_recall_settings(self, inherit_shared: bool) -> ProjectRecallSettings:
        self.require_reviewer()
        project = self._recall_project()
        # 初次设置也在工作空间锁内读写。重复请求复用已登记版本。
        if not await SqlAlchemyIdentityStore(self.db).lock_workspace(self.scope.workspace):
            raise missing()
        current = await self.project_recall_settings()
        if current["revision"] and current["inherit_shared"] == inherit_shared:
            return current
        row = KnowledgeProjectRecallPolicy(
            workspace_id=self.scope.workspace, project_id=project, inherit_shared=inherit_shared,
            updated_by=self.scope.user, updated_at=datetime.now(UTC),
        )
        self.db.add(row)
        await self.db.flush()
        return {"inherit_shared": inherit_shared, "revision": row.id}

    async def _recall_view(self, recall: bool) -> "KnowledgeStore":
        if not recall or self.scope.project is None or not self.scope.include_shared:
            return self
        if (await self.project_recall_settings())["inherit_shared"]:
            return self
        # 从同一个授权视图读候选、正文和关系，预检与实际召回使用相同边界。
        return KnowledgeStore(self.db, replace(self.scope, include_shared=False), self.storage)

    def _event(self, entry: KnowledgeEntry, action: str, revision: int | None, reason: str = "") -> None:
        self.db.add(KnowledgeEvent(
            id=uuid4(), entry_id=entry.id, actor_id=self.scope.user, action=action,
            revision=revision, reason=reason, occurred_at=datetime.now(UTC),
        ))

    async def _entry(self, entry_id: UUID, *, exact_scope: bool = False, refresh: bool = False) -> KnowledgeEntry:
        scope = self.scope
        target = visible(scope)
        if exact_scope:
            target &= KnowledgeEntry.project_id == scope.project
        lookup = select(KnowledgeEntry).where(
            KnowledgeEntry.workspace_id == scope.workspace, target, KnowledgeEntry.id == entry_id,
        ).with_for_update()
        if refresh:
            # 加锁可能等待并发发布，必须用锁到的记录覆盖此前加载的 ORM 对象。
            lookup = lookup.execution_options(populate_existing=True)
        entry = await self.db.scalar(lookup)
        if entry is None:
            raise missing()
        return entry

    async def _first_path(self, entry_id: UUID) -> str | None:
        rows = await self.db.scalars(select(KnowledgeMarkdownPath.path).where(
            KnowledgeMarkdownPath.entry_id == entry_id,
            KnowledgeMarkdownPath.revision == 1,
            KnowledgeMarkdownPath.position == 0,
        ))
        return rows.one_or_none()

    async def _replay(self, operation_id: UUID, fingerprint: str) -> dict[str, object] | None:
        row = (await self.db.execute(
            select(KnowledgeEntry, KnowledgeRevision)
            .join(KnowledgeRevision, KnowledgeRevision.entry_id == KnowledgeEntry.id)
            .where(
                KnowledgeRevision.workspace_id == self.scope.workspace,
                KnowledgeRevision.author_id == self.scope.user,
                KnowledgeRevision.operation_id == operation_id,
            )
        )).one_or_none()
        if row is None:
            return None
        entry, revision = row
        if fingerprint != revision.request_digest:
            raise conflict("同一请求标识已用于不同的知识操作。")
        return await self.view(entry, revision)

    async def _lock_promotion_targets(self) -> None:
        await lock_knowledge_targets(self.db, self.scope.workspace, self.scope.project)

    async def contribute(
        self, value: Contribution, entry_id: UUID | None = None, *,
        publish_mr: bool = False, managed_iwiki: bool = False,
        wiki_links: Sequence[CodeLink] = (),
    ) -> dict[str, object]:
        """普通经验立即发布，规则、文档、代码、修订或晋升进入评审。"""
        if entry_id is None and value.kind in {"doc", "rule", "skill"}:
            await self._lock_promotion_targets()
        fingerprint = value.digest(self.scope, entry_id)
        if result := await self._replay(value.operation_id, fingerprint):
            return result
        try:
            async with self.db.begin_nested():
                entry, revision = await self._contribute(
                    value, entry_id, fingerprint, publish_mr=publish_mr,
                    managed_iwiki=managed_iwiki, wiki_links=wiki_links,
                )
        except IntegrityError:
            # 并发重试的唯一键由数据库裁决；查不到同一请求时保留原始异常。
            if result := await self._replay(value.operation_id, fingerprint):
                return result
            raise
        return await self.view(entry, revision)

    async def import_batch(
        self, values: Sequence[Contribution], *, publish_reviewed: bool, request_id: str,
        iwiki_terms: dict[UUID, list[str]] | None = None,
    ) -> list[dict[str, object]]:
        """将确认的评审经验按普通贡献发布，其余候选沿用审核权限。"""
        imported: dict[UUID, dict[str, object]] = {}
        evidence = await self._iwiki_code_evidence() if iwiki_terms and self.scope.project else None
        async with self.db.begin_nested():
            # 批次并发重放时按固定顺序占用同一组幂等键。
            for value in sorted(values, key=lambda item: item.operation_id):
                terms = tuple((iwiki_terms or {}).get(value.operation_id, []))
                item = await self.contribute(
                    replace(value, iwiki_terms=terms), publish_mr=publish_reviewed,
                    wiki_links=evidence.links(terms) if evidence and terms else (),
                )
                if publish_reviewed and self.scope.can_review and item["revision_status"] == "pending":
                    item = await self.review(
                        UUID(str(item["id"])), int(str(item["revision"])), True, "来源导入审阅", request_id,
                    )
                imported[value.operation_id] = item
        return [imported[value.operation_id] for value in values]

    async def _iwiki_code_evidence(self) -> CodeEvidence:
        scoped = published(self.scope).where(
            KnowledgeEntry.project_id == self.scope.project, KnowledgeEntry.codebase_id.is_not(None),
            KnowledgeRevision.source["wiki_page"].as_string().is_(None),
        ).with_only_columns(KnowledgeEntry.id, KnowledgeRevision)
        pages = [
            Page(str(identity), revision.title, revision.body, revision.source["ref"], str(revision.revision))
            for identity, revision in (await self.db.execute(scoped)).tuples() if is_code(revision.source)
        ]
        return CodeEvidence(pages)

    async def import_iwiki_sections(
        self, run: UUID, source: str, sections: Sequence[tuple[str, str]], *,
        require_review: bool, request_id: str,
    ) -> list[dict[str, object]]:
        """在实际作用域内维护三个独立文档版本，不改动已有的手工知识正文。"""
        titles = {"business-api": "业务接口", "external-knowledge": "外部知识源", "glossary": "术语表"}
        scope = self.scope
        project = KnowledgeEntry.project_id == scope.project
        await self._lock_promotion_targets()

        result = []
        for section, body in sorted(sections):
            operation = uuid5(run, f"iwiki-section/{section}")
            provenance = {
                "type": "wiki", "ref": source, "iwiki_section": section,
                "resource_path": f"docs/team-codebase/external-knowledge/{section}.md",
            }
            query = select(KnowledgeEntry, KnowledgeRevision).join(
                KnowledgeRevision, KnowledgeRevision.entry_id == KnowledgeEntry.id,
            ).where(KnowledgeEntry.workspace_id == scope.workspace)
            replay = (await self.db.execute(query.where(
                KnowledgeRevision.operation_id == operation, KnowledgeRevision.author_id == scope.user,
            ))).one_or_none()
            if replay:
                entry, revision = replay
                if entry.project_id != scope.project or revision.source != provenance or revision.body != body:
                    raise conflict("同一导入运行已用于其他 iWiki 汇总。")
                result.append(await self.view(entry, revision))
                continue

            # 只使用服务端标记过的汇总条目。手工创建的同路径文档不能成为写入目标。
            current = (await self.db.execute(query.where(
                project, KnowledgeEntry.kind == "doc", KnowledgeEntry.status.in_(("active", "pending")),
                KnowledgeEntry.resource_key == resource_key(provenance),
                KnowledgeRevision.source["iwiki_section"].as_string() == section,
            ).order_by(KnowledgeRevision.revision.desc()).limit(1).with_for_update(of=KnowledgeEntry))).one_or_none()
            if current:
                entry, _ = current
                latest = await self.db.scalar(select(KnowledgeRevision).where(
                    KnowledgeRevision.entry_id == entry.id,
                ).order_by(KnowledgeRevision.revision.desc()).limit(1))
                assert latest is not None
                if latest.status == "pending":
                    raise conflict(f"{titles[section]}已有待审核版本。")
            value = Contribution(
                operation_id=operation, kind="doc", title=titles[section], body=body,
                tags=["iWiki", titles[section]], source=provenance, relations=[],
                base_revision=current[0].published_revision if current else None,
            )
            item = await self.contribute(value, current[0].id if current else None, managed_iwiki=bool(current))
            if scope.can_review and not require_review:
                item = await self.review(
                    UUID(str(item["id"])), int(str(item["revision"])), True, "iWiki 汇总导入", request_id,
                )
            result.append(item)
        return result

    async def _contribute(
        self, value: Contribution, entry_id: UUID | None, fingerprint: str, *, publish_mr: bool,
        quality: bool = False, managed_iwiki: bool = False,
        wiki_links: Sequence[CodeLink] = (), inherited: Sequence[Declaration] = (),
    ) -> tuple[KnowledgeEntry, KnowledgeRevision]:
        scope, now = self.scope, datetime.now(UTC)
        authored_at = value.authored_at
        repromoting = False
        namespace = value.source.get("namespace")
        if namespace and entry_id is not None:
            raise conflict("修订必须沿用原知识目录。")
        folders = scope.namespaces.learnings if scope.namespaces is not None else None
        if not namespace and entry_id is None and value.kind == "learning" and folders is not None:
            # 显式团队路径与 --namespace 走同一个目录门槛。
            location = entry_path(value.operation_id, value.kind, value.source)
            parts = location.split("/") if location else []
            if len(parts) > 2 and parts[0] == "learnings":
                namespace = parts[1]
        if namespace and namespace not in (folders or ()):
            raise KnowledgeError("KNOWLEDGE_NAMESPACE_UNAVAILABLE", "该经验目录未在当前职能或项目中启用。", 403)
        paths = value.rule_paths
        if value.kind == "rule" and paths is None:
            paths = markdown_rule_paths(value.body)
        if value.promoted_from is not None:
            source = await self._entry(value.promoted_from, exact_scope=True)
            existing = await self.db.scalar(select(KnowledgeEntry.id).where(
                KnowledgeEntry.promoted_from == source.id, KnowledgeEntry.status.in_(("pending", "active")),
            ))
            if (
                entry_id is not None or value.kind not in {"skill", "rule", "doc"}
                or source.kind != "learning" or source.status != "active" or existing is not None
            ):
                raise conflict("当前经验没有可提交的晋升资格。")
            baseline = await self.db.get(KnowledgeRevision, (source.id, source.published_revision))
            if baseline is None or not (await self.totals(source.id)).can_promote(
                baseline.authored_at or source.created_at, now,
            ):
                raise conflict("当前经验没有可提交的晋升资格。")
            provenance = dict(value.source)
            provenance.setdefault("promotion_revision", str(source.published_revision))
            if entry_path(source.id, value.kind, provenance) is None:
                target_kind = type_cast(PromotionKind, value.kind)
                provenance["resource_path"] = promotion_path(source.id, target_kind, await self._first_path(source.id))
            value = replace(value, source=provenance)
            if value.kind == "skill" and value.base_revision is None:
                origin = entry_path(source.id, "skill", value.source)
                if origin and (archived := await self._archived_skill_promotion(source.id, origin)):
                    if await self._occupied_promotion_targets({"skill": origin}, scope.project):
                        raise conflict("晋升目标已存在，请先核对该条目。")
                    entry_id, base_release = archived
                    value = replace(value, base_revision=base_release)
                    repromoting = True
        if entry_id is None:
            authored_at = authored_at or review_authored_day(value.source)
            if value.base_revision is not None:
                raise conflict("新知识条目没有基准版本。")
            registry_proposal = quality and value.source["type"] == REGISTRY_QUALITY_SOURCE
            if value.kind == "skill" and value.promoted_from is None and not registry_proposal:
                raise conflict("Skill 候选必须关联一条可晋升的经验。")
            source_type = value.source["type"]
            self_published = source_type in {"manual", "session", "run"} or (
                source_type == "pull_request" and publish_mr
            )
            direct = value.kind == "learning" and self_published and value.promoted_from is None
            if (
                direct and scope.device_id is not None and source_type in {"manual", "session", "run"}
                and "resource_path" not in value.source
            ):
                # 本机以请求 ID 打开排队经验，发布时沿用同一个逻辑地址。
                value = replace(value, source=value.source | {"resource_path": f"learnings/{value.operation_id}"})
            identifier = uuid4()
            origin = entry_path(identifier, value.kind, value.source)
            if origin and not registry_proposal and value.kind in {"doc", "rule", "skill"}:
                target = type_cast(PromotionKind, value.kind)
                if await self._occupied_promotion_targets(
                    {target: origin}, scope.project, promotions_only=value.promoted_from is None,
                ):
                    raise conflict("晋升目标已存在，请先核对该条目。")
            entry = KnowledgeEntry(
                id=identifier, workspace_id=scope.workspace, project_id=scope.project, kind=value.kind,
                status="active" if direct else "pending", created_by=scope.user, created_at=now,
                promoted_from=value.promoted_from, published_revision=1 if direct else None,
                resource_key=resource_key(value.source) if value.kind != "code" else None,
            )
            self.db.add(entry)
            await self.db.flush()
            sequence = 1
        else:
            entry = await self._entry(entry_id, exact_scope=True)
            if entry.codebase_id is not None:
                raise conflict("代码证据由索引生成，请重新导入来源。")
            if entry.kind == "skill" and not (quality or repromoting):
                raise conflict("请在 Skill Registry 创建后续版本。")
            origin = await self._first_path(entry.id)
            if "resource_path" in value.source and (
                origin is None or entry_path(entry.id, entry.kind, value.source) != origin
            ):
                raise conflict("修订必须沿用该知识的逻辑路径。")
            if not repromoting and entry.created_by != scope.user and not scope.can_review and not managed_iwiki:
                raise KnowledgeError("KNOWLEDGE_FORBIDDEN", "只有作者或管理员可以提交修订。", 403)
            expected_status = "archived" if repromoting else "active"
            if (
                entry.status != expected_status or entry.kind != value.kind
                or entry.published_revision != value.base_revision
            ):
                raise conflict("知识类型或基准版本已变化，请刷新后再提交。")
            previous = await self.db.scalar(select(KnowledgeRevision).where(
                KnowledgeRevision.entry_id == entry.id,
            ).order_by(KnowledgeRevision.revision.desc()).limit(1))
            assert previous is not None
            if previous.status == "pending":
                raise conflict("该知识已有待审核版本。")
            if repromoting:
                # 已有发布指针的待审 Skill 是重新晋升。待审期间保留历史包并占用晋升目标。
                entry.status = "pending"
            baseline = previous if previous.revision == value.base_revision else await self.db.get(
                KnowledgeRevision, (entry.id, entry.published_revision),
            )
            if authored_at is None and baseline is not None:
                # 修订未提供日期时沿用当前发布版，已驳回草稿不能改变年龄。
                authored_at = baseline.authored_at
            if value.kind == "rule" and paths is None:
                paths = baseline.rule_paths if baseline is not None else []
            direct, sequence = False, previous.revision + 1
        explicit = {link["target"] for link in value.relations}
        relations = value.relations + [
            {
                "kind": "MAPS_TO", "target": link.page.id, "derived": "iwiki",
                "source_revision": str(sequence), "target_revision": link.page.revision,
                "evidence": link.term, "confidence": link.confidence,
            }
            for link in wiki_links if link.page.id not in explicit
        ]
        declared, findings = declarations(entry.kind, origin, value.related)
        revision = KnowledgeRevision(
            entry_id=entry.id, revision=sequence, workspace_id=scope.workspace, author_id=scope.user,
            operation_id=value.operation_id, request_digest=fingerprint, status="published" if direct else "pending",
            title=value.title, body=value.body, tags=value.tags, source=value.source, relations=relations,
            source_session_key=learning_session_key(value.source) if entry.kind == "learning" else None,
            created_at=now, authored_at=authored_at, rule_paths=paths or [],
            relation_findings=findings if value.related is not None or inherited else None,
        )
        self._event(entry, "contributed" if direct else "submitted", sequence, value.source.get("promotion_reason", ""))
        await add_indexed_revisions(self.db, [revision], kind=entry.kind, path=origin)
        index_markdown(self.db, entry, revision, origin, declared or inherited)
        return entry, revision

    async def review_preview(self, entry_id: UUID, revision: int) -> dict[str, object]:
        """在精确范围预览版本和关系校验，不推进审核状态。"""
        self.require_reviewer()
        row = (await self.db.execute(
            select(KnowledgeEntry, KnowledgeRevision).join(
                KnowledgeRevision, KnowledgeRevision.entry_id == KnowledgeEntry.id,
            ).where(
                KnowledgeEntry.workspace_id == self.scope.workspace,
                KnowledgeEntry.project_id == self.scope.project,
                KnowledgeEntry.id == entry_id, KnowledgeRevision.revision == revision,
            )
        )).one_or_none()
        if row is None:
            raise missing()
        return await self.view(*row, include_body=True)

    async def review(
        self, entry_id: UUID, revision: int, accept: bool, reason: str, request_id: str,
    ) -> dict[str, object]:
        """同一版本只能评审一次；重试相同决定时直接返回当前记录。"""
        self.require_reviewer()
        if accept:
            await self._lock_promotion_targets()
        entry = await self._entry(entry_id, exact_scope=True)
        version = await self.db.get(KnowledgeRevision, (entry.id, revision))
        if version is None:
            raise missing()
        decision = "published" if accept else "rejected"
        if version.status == decision:
            return await self.view(entry, version)
        if accept and version.status == "pending" and entry.status == "archived":
            raise conflict("请先恢复已归档的条目，再接受这份修订。")
        reviewable = entry.status in {"active", "pending"} or (
            not accept and entry.status == "archived" and entry.published_revision is not None
        )
        if version.status != "pending" or not reviewable:
            raise conflict("该知识版本已经处理。")
        if accept and any(item["level"] == "fail" for item in version.relation_findings or []):
            raise KnowledgeError("KNOWLEDGE_RELATED_REVIEW", "该版本的 related 声明有错误，请查看审核预览。", 422)
        base = entry.published_revision
        repromoting = base is not None and entry.status == "pending" and entry.kind == "skill"
        if accept and base is None and entry.kind == "skill" and entry.promoted_from is not None:
            # 升级前的 Registry 记录可能抢占了待审稿路径。保留两个原状态，交由评审处理。
            registered = await published_skill_reference(
                self.db, self.scope.workspace, entry.project_id, path=await self._first_path(entry.id),
            )
            if registered is not None and registered.project_id == entry.project_id:
                raise conflict("晋升目标已存在，请先核对该条目。")
        registry_proposal = version.source.get("type") == REGISTRY_QUALITY_SOURCE
        if accept and registry_proposal:
            actor = SkillActor(self.scope.user, self.scope.workspace, self.scope.role)
            release = await publish_registry_quality(
                self.db, actor, entry.project_id, UUID(version.source["skill_id"]),
                UUID(version.source["base_skill_version_id"]), version.body, request_id, self.storage,
            )
            # 审核记录保留正文，实际发布指针只在 Registry 中更新。
            entry.status = "archived"
            self._event(entry, "registry_quality_published", revision, str(release))
        elif accept:
            entry.status, entry.published_revision = "active", revision
        elif repromoting:
            entry.status = "archived"
        elif entry.published_revision is None:
            entry.status = "rejected"
        version.status = decision
        if accept and entry.kind == "skill" and not registry_proposal:
            previous: tuple[UUID, str] | None = None
            if base is not None:
                skill = await self.db.scalar(select(KnowledgeEvent.reason).where(
                    KnowledgeEvent.entry_id == entry.id, KnowledgeEvent.revision == base,
                    KnowledgeEvent.action == "skill_published",
                ))
                baseline = await self.db.get(KnowledgeRevision, (entry.id, base))
                if skill is None or baseline is None:
                    raise conflict("基准版本没有对应的 Skill 发布记录。")
                # 历史版本按原规则回读，已同步的版本直接使用 Registry 的发布记录。
                previous = UUID(skill), baseline.source.get(REGISTRY_VERSION, f"1.0.{base - 1}")
            await self.db.flush()
            skill_release = await publish_knowledge_skill(
                self.db, workspace=self.scope.workspace, project_id=entry.project_id,
                reviewer=self.scope.user, role=self.scope.role,
                entry=entry.id, title=version.title, body=version.body, request_id=request_id,
                revision=revision, previous=previous, source=version.source, repromoting=repromoting,
            )
            version.source = {
                **version.source, REGISTRY_VERSION: skill_release.version, REGISTRY_VERSION_ID: str(skill_release.id),
            }
            self._event(entry, "skill_published", revision, str(skill_release.skill_id))
        self._event(entry, decision, revision, reason)
        return await self.view(entry, version)

    def require_reviewer(self) -> None:
        if not self.scope.can_review:
            raise KnowledgeError("KNOWLEDGE_FORBIDDEN", "只有工作空间管理员可以处理此请求。", 403)

    async def detail(
        self, entry_id: UUID, session: str | None = None, *, recall: bool = False, actor: UUID | None = None,
    ) -> dict[str, object]:
        view = await self._recall_view(recall)
        scope = view.scope
        row = (await self.db.execute(published(scope).where(KnowledgeEntry.id == entry_id))).one_or_none()
        if row is not None:
            return await view._open(*row, session=session, recall=recall, actor=actor)
        skill = await published_skill_reference(
            self.db, scope.workspace, scope.project, identity=entry_id,
            directories=scope.skill_directories, include_shared=scope.include_shared,
        )
        if skill is None:
            raise missing()
        return await view._open_skill(skill, session, actor=actor)

    async def detail_path(
        self, path: str, session: str | None = None, *, recall: bool = False, actor: UUID | None = None,
    ) -> dict[str, object]:
        """打开可见的路径版本。项目同名资源优先，同范围歧义由用户选择 ID。"""
        normalized = declared_path(path) or normalize_resource_path(path)
        if normalized is None:
            raise KnowledgeError("KNOWLEDGE_PATH_INVALID", "请输入团队内的相对资源路径。", 422)
        view = await self._recall_view(recall)
        scope = view.scope
        paths = KnowledgeMarkdownPath
        statement = published(scope).where(~overridden(scope)).join(
            paths, (paths.entry_id == KnowledgeEntry.id) & (paths.revision == KnowledgeRevision.revision),
        ).where(
            paths.position == 0, paths.key == hashlib.sha256(normalized.encode()).hexdigest(),
        ).order_by(KnowledgeEntry.project_id.is_(None), KnowledgeEntry.id).limit(2)
        matches = (await self.db.execute(statement)).all()
        if normalized.startswith("skills/"):
            skill = await published_skill_reference(
                self.db, scope.workspace, scope.project, path=normalized,
                directories=scope.skill_directories, include_shared=scope.include_shared,
            )
            if skill is not None:
                if not matches or (skill.project_id is not None and matches[0][0].project_id is None):
                    return await view._open_skill(skill, session, actor=actor)
                if skill.project_id == matches[0][0].project_id:
                    # 晋升条目和同名安装包是同一资产，按知识 ID 打开才能保留出处与召回反馈。
                    mirrored = await self.db.scalar(select(KnowledgeEvent.id).where(
                        KnowledgeEvent.entry_id == matches[0][0].id,
                        KnowledgeEvent.action == "skill_published", KnowledgeEvent.reason == str(skill.id),
                    ).limit(1))
                    if mirrored is None:
                        raise conflict("团队路径对应多个可见来源，请按知识条目 ID 打开。")
        if not matches:
            raise missing()
        entry, revision = matches[0]
        if len(matches) == 2 and entry.project_id == matches[1][0].project_id:
            raise conflict("团队路径对应多个可见来源，请按知识条目 ID 打开。")
        return await view._open(entry, revision, session=session, recall=recall, actor=actor)

    async def _open_skill(
        self, reference: PublishedSkillReference, session: str | None, *, actor: UUID | None,
    ) -> dict[str, object]:
        workspace, project = self.scope.workspace, self.scope.project
        body = await skill_markdown(self.db, workspace, reference, self.storage)
        current = await published_skill_reference(
            self.db, workspace, project, identity=reference.id, lock=session is not None,
            directories=self.scope.skill_directories, include_shared=self.scope.include_shared,
        )
        if current is None:
            raise missing()
        if current.version_id != reference.version_id:
            raise conflict("Skill 发布版本已变化，请重新打开。")
        scoped = published(self.scope).where(~overridden(self.scope))
        links = (await markdown_relations(
            self.db, scoped, [], workspace=workspace, project=project, skills=[current],
            skill_directories=self.scope.skill_directories,
        )).get(reference.id, [])
        if session:
            if actor is None or await self.journal.can_adopt(
                session, current.id, actor, skill_version_id=current.version_id,
            ):
                await self.skill_feedback.opened(current, session)
            await self.journal.opened(session, current.id, skill_version_id=current.version_id, actor=actor)
        return self._skill_view(SkillSearchView(current, [], body[:360]), links) | {"body": body}

    @staticmethod
    def _skill_view(view: SkillSearchView, links: list[dict[str, str]]) -> dict[str, object]:
        item = view.reference
        declared = [{key: value for key, value in link.items() if key != "via"}
                    for link in links if link.get("via") == "declared_path"]
        declared_targets = {link["target"] for link in declared}
        automatic = [link for link in links if "via" not in link and link["target"] not in declared_targets]
        return {
            "id": str(item.id), "project_id": str(item.project_id) if item.project_id else None,
            "kind": "skill", "status": "active", "revision": item.version, "revision_status": "published",
            "title": item.title, "tags": view.tags, "created_at": item.published_at.isoformat(),
            "author_id": str(item.author), "relations": (declared + automatic)[:5],
            "source": {
                "type": "skill_registry", "ref": item.path, "resource_path": item.path,
                "version_id": str(item.version_id), "content_sha256": item.content_sha256,
            },
            "excerpt": view.excerpt,
        }

    async def _open(
        self, entry: KnowledgeEntry, revision: KnowledgeRevision, *,
        session: str | None, recall: bool, actor: UUID | None,
    ) -> dict[str, object]:
        if session:
            await self._record(
                entry.id, session, opened_revision=revision.revision,
                credit_open=not recall or entry.project_id == self.scope.project, actor=actor,
            )
            await self.journal.opened(session, entry.id, actor=actor)
        return await self.view(entry, revision, include_body=True)

    async def knowledge_gaps(self, query: str) -> list[dict[str, object]]:
        if self.scope.project is None:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_SCOPE", "代码缺口需要选择知识库项目。", 422)
        return await published_gaps(self.db, published(self.scope), query)

    async def search(
        self, query: str, limit: int, session: str | None = None, check_only: bool = False,
        *, recall: bool = False, request_id: str | None = None, depth: RecallDepth | None = None,
        pending_feedback: list[PendingFeedback] | None = None,
        queued_learnings: list[QueuedLearning] | None = None, recall_actor: UUID | None = None,
        check_details: bool = False, kind: KnowledgeKind | None = None, source_type: str | None = None,
    ) -> dict[str, object]:
        """只对实际返回的发布版本记录召回，本机经验由设备呈现。"""
        view = await self._recall_view(recall)
        return await view._search(
            query, limit, session, check_only, recall=recall, request_id=request_id, depth=depth,
            pending_feedback=pending_feedback, queued_learnings=queued_learnings, recall_actor=recall_actor,
            check_details=check_details, kind=kind, source_type=source_type,
        )

    async def _search(
        self, query: str, limit: int, session: str | None, check_only: bool, *,
        recall: bool, request_id: str | None, depth: RecallDepth | None,
        pending_feedback: list[PendingFeedback] | None, queued_learnings: list[QueuedLearning] | None,
        recall_actor: UUID | None, check_details: bool, kind: KnowledgeKind | None, source_type: str | None,
    ) -> dict[str, object]:
        # 范围内的条目先按类型和来源过滤，再参加相关性排序和条数限制。
        hidden = overridden(self.scope)
        authorized = published(self.scope)
        if kind is not None:
            authorized = authorized.where(KnowledgeEntry.kind == kind)
        if source_type == "github_pr":
            authorized = authorized.where(
                KnowledgeRevision.source["type"].as_string() == "pull_request",
                KnowledgeRevision.source["ref"].as_string().like("https://github.com/%/pull/%"),
            )
        local = QueueRecall()
        queue_metadata: dict[str, object] = {}
        if queued_learnings:
            if self.scope.device_id is None:
                raise KnowledgeError("KNOWLEDGE_QUEUE_DEVICE_REQUIRED", "请使用已配对设备查询贡献队列。", 403)
            folders = self.scope.namespaces
            local = await resolve_queue(
                self.db, authorized.where(~hidden), self.scope.workspace, self.scope.user, self.scope.project,
                folders.learnings if folders is not None else None,
                [item for item in queued_learnings
                 if self.scope.include_shared or item.project_id == self.scope.project],
            )
            queue_metadata["queued_visible"] = [str(item) for item in local.visible]
            if local.hidden:
                hidden = or_(hidden, KnowledgeEntry.id.in_(local.hidden))
        if (
            query and self.scope.project is not None
            and (check_details or (not check_only and recall and depth in {"context", "lookup"}))
            and (gaps := await self.knowledge_gaps(query))
        ):
            queue_metadata["knowledge_gaps"] = gaps
        if check_only and local.matches:
            verdict = {"score": None, "threshold": None, "reason": "local-queue"} if check_details else {}
            return {"relevant": True, **verdict, **queue_metadata}
        if not check_only:
            limit = max(0, limit - local.matches)
        statement = authorized
        wiki = KnowledgeRevision.source["wiki_page"].as_string()
        if depth == "context":
            # 默认召回只纳入概览、模块和文档，目录及根层分类页留给深度查询。
            statement = statement.where(or_(KnowledgeEntry.codebase_id.is_(None), is_context_page()))
        elif depth == "lookup":
            statement = statement.where(or_(wiki.is_(None), wiki != GAP_PAGE))
        else:
            statement = statement.where(wiki.is_(None))
        routes = await code_route(self.db, statement) if depth == "route" else []
        if check_only and routes and query:
            verdict = {"score": None, "threshold": None, "reason": "code-route"} if check_details else {}
            return {"relevant": True, **verdict, **queue_metadata}
        if limit == 0:
            return {"items": [], **queue_metadata, **({"code_route": routes} if depth == "route" else {})}
        if depth == "route":
            statement = statement.where(KnowledgeEntry.codebase_id.is_(None))
        include_registry = kind in {None, "skill"} and source_type is None
        registry = standalone_skill_catalog(
            self.scope.workspace, self.scope.project, directories=self.scope.skill_directories,
            include_shared=self.scope.include_shared,
        )
        hits: dict[tuple[UUID, int], SearchHit] = {}
        ranked: list[SearchHit] = []
        related: RelatedFiles = {}
        if query:
            ranked = await rank_published(
                self.db, statement, self.scope.workspace, query, recall=recall, hidden=hidden,
                with_coverage=not check_only or check_details,
                feedback=FeedbackProjection(self.scope.user, self.scope.project, pending_feedback)
                if pending_feedback else None,
                registry=registry if include_registry else None,
            )
            if depth == "lookup":
                ranked, related = await lookup_graph(self.db, statement, ranked, query=query)
            elif depth == "context" and self.scope.project is not None:
                ranked = await context_graph(self.db, authorized, ranked, query=query)
            ranked = order_hits(ranked, code_limit=3 if depth in {"context", "lookup"} else None, authored_date=recall)
            if check_only:
                top = ranked[0] if ranked else None
                if check_details:
                    return (await describe_check(self.db, authorized.where(~hidden), registry, top)) | queue_metadata
                return {"relevant": top is not None and top.relevant, **queue_metadata}
            hits = {(hit.candidate.entry, hit.candidate.revision): hit for hit in ranked[:limit]}
            statement = statement.where(KnowledgeEntry.id.in_([entry_id for entry_id, _ in hits]))
        if check_only:
            return (empty_check() if check_details else {"relevant": False}) | queue_metadata
        rows = (await self.db.execute(statement.where(~hidden).order_by(
            KnowledgeRevision.created_at.desc(), KnowledgeEntry.id,
        ).limit(limit))).tuples().all()
        versions = [
            item.candidate.skill_version_id for item in hits.values() if item.candidate.skill_version_id is not None
        ] if query else None
        skills = await skill_search_views(self.db, registry, limit, versions) if include_registry else []
        if hits:
            # 审核或归档可能在排序和详情读取之间发生，不能使用旧版本的分数。
            current = {(entry.id, version.revision): (entry, version) for entry, version in rows}
            rows = [current[key] for key in hits if key in current]
        timestamps = {entry.id: version.created_at for entry, version in rows}
        timestamps.update((view.reference.id, view.reference.published_at) for view in skills)
        if query:
            order = [identity for identity, _ in hits if identity in timestamps]
        else:
            order = sorted(
                timestamps, key=lambda identity: (-timestamps[identity].replace(tzinfo=UTC).timestamp(), identity),
            )[:limit]
        selected = set(order)
        rows = [row for row in rows if row[0].id in selected]
        skills = [view for view in skills if view.reference.id in selected]
        if session:
            stale: set[UUID] = set()
            # 固定加锁顺序，避免两个交叉命中集合并发记录时相互等待。
            for entry_id, revision in sorted(
                (entry.id, version.revision) for entry, version in rows
                if not recall or entry.project_id == self.scope.project
            ):
                try:
                    await self._record(entry_id, session, returned_revision=revision)
                except KnowledgeError as error:
                    if error.code not in {"KNOWLEDGE_NOT_FOUND", "KNOWLEDGE_PUBLICATION_CHANGED"}:
                        raise
                    # 作废的命中没有返回给调用者，也不能写入该次召回的账本。
                    stale.add(entry_id)
            for view in sorted(skills, key=lambda item: item.reference.id):
                ref = view.reference
                if (not recall or ref.project_id == self.scope.project) and not await self.skill_feedback.returned(
                    ref, session,
                ):
                    stale.add(ref.id)
            rows = [row for row in rows if row[0].id not in stale]
            skills = [view for view in skills if view.reference.id not in stale]
            order = [identity for identity in order if identity not in stale]
        changes: dict[str, object] = {}
        change_targets: dict[UUID, list[str]] = {}
        cross_edges: list[PublishedCrossEdge] | None = None
        if depth == "lookup" and self.scope.project is not None:
            change_hits = [
                (entry, version) for entry, version in rows
                if (hit := hits.get((entry.id, version.revision)))
                and (hit.candidate.matches or hit.candidate.named_module)
            ]
            scoped = authorized.where(~hidden)
            sources = await source_groups(self.db, scoped, change_hits, ranked)
            if sources.groups:
                returned_files = {entry.id for entry, version in rows if version.source.get("path")}
                cross_edges = await published_cross_repository_edges(
                    self.db, scoped, self.scope.project, self.scope.code_connections, sources.file_ids | returned_files,
                )
                changes, change_targets = await change_evidence(self.db, scoped, sources, cross_edges)
        derived = await self._derived_for(
            rows, skills=[view.reference for view in skills], queue_hidden=local.hidden, cross_edges=cross_edges,
        )
        items: dict[UUID, dict[str, object]] = {}
        snapshots: dict[UUID, RecalledItem] = {}
        for entry, version in rows:
            item = await self.view(entry, version, derived=derived.get(entry.id, []), queue_hidden=local.hidden)
            hit = hits.get((entry.id, version.revision))
            if hit:
                item.update(hit.evidence(version.title, version.tags))
            files = related.get(entry.id, [])
            if depth == "lookup" and hit and entry.codebase_id is not None:
                item["excerpt"] = version.body[:16_000]
                item["related_files"] = files
            if session:
                links = type_cast(list[dict[str, str]], item["relations"])
                destinations = [
                    *(link["target"] for link in links), *(file["id"] for file in files),
                    *change_targets.get(entry.id, []),
                ]
                snapshots[entry.id] = RecalledItem(
                    entry.id, version.revision, hit.score if hit else 0.0, bool(hit and hit.relevant),
                    entry.project_id == self.scope.project, list(dict.fromkeys(destinations)),
                )
            items[entry.id] = item
        for view in skills:
            ref = view.reference
            hit = hits.get((ref.id, 0))
            item = self._skill_view(view, derived.get(ref.id, []))
            if hit:
                item.update(hit.evidence(ref.title, view.tags))
            if session:
                links = type_cast(list[dict[str, str]], item["relations"])
                snapshots[ref.id] = RecalledItem(
                    ref.id, 0, hit.score if hit else 0.0, bool(hit and hit.relevant),
                    ref.project_id == self.scope.project, list(dict.fromkeys(link["target"] for link in links)),
                    skill_version_id=ref.version_id,
                )
            items[ref.id] = item
        response: dict[str, object] = {"items": [items[key] for key in order], **queue_metadata, **changes}
        if depth == "route":
            response["code_route"] = routes
        if session and (order or not (routes or local.matches)):
            response["run_id"] = await self.journal.record(
                session, [snapshots[key] for key in order], request_id, recall_actor=recall_actor,
            )
        return response

    async def learning_overlaps(self, body: str) -> list[dict[str, object]]:
        """比较可见且近期创建的已发布经验，无需记录召回或保存草稿。"""
        draft = keywords(body)
        if not draft:
            return []
        statement = (
            published(self.scope)
            .with_only_columns(
                KnowledgeEntry.id, KnowledgeRevision.title, KnowledgeRevision.body,
                KnowledgeRevision.author_id, KnowledgeRevision.operation_id,
            )
            .where(KnowledgeEntry.kind == "learning", KnowledgeEntry.created_at >= datetime.now(UTC) - LOOKBACK)
            .order_by(KnowledgeEntry.created_at.desc(), KnowledgeEntry.id)
            .execution_options(yield_per=100)
        )
        matches: list[tuple[float, dict[str, object]]] = []
        rows = await self.db.stream(statement)
        try:
            async for entry_id, title, current_body, author_id, operation_id in rows.tuples():
                ratio = overlap(draft, keywords(current_body))
                if ratio >= MIN_OVERLAP:
                    item: dict[str, object] = {"id": str(entry_id), "title": title, "overlap": ratio}
                    # 设备可能在服务端提交成功后断开。用本人的请求键折叠还在本机的同一条记录。
                    if author_id == self.scope.user:
                        item["operation_id"] = str(operation_id)
                    matches.append((ratio, item))
        finally:
            await rows.close()
        matches.sort(key=lambda entry: entry[0], reverse=True)
        return [item for _, item in matches]

    async def hint_policy(self) -> dict[str, object]:
        row = await self.db.scalar(select(KnowledgeHintPolicy).where(
            KnowledgeHintPolicy.workspace_id == self.scope.workspace,
        ).order_by(KnowledgeHintPolicy.id.desc()).limit(1))
        if row is None:
            return {"enabled": True, "lookback_days": 7, "max_mrs": 10, "source": "default"}
        return {
            "enabled": row.enabled, "lookback_days": row.lookback_days, "max_mrs": row.max_mrs, "source": "workspace",
        }

    async def session_policies(self) -> dict[BooleanSessionPolicy, dict[str, object]]:
        version = select(func.max(KnowledgeSessionPolicy.id)).where(
            KnowledgeSessionPolicy.workspace_id == self.scope.workspace,
            KnowledgeSessionPolicy.kind != "plan-reminder",
        ).group_by(KnowledgeSessionPolicy.kind)
        rows = await self.db.scalars(
            select(KnowledgeSessionPolicy).where(KnowledgeSessionPolicy.id.in_(version))
        )
        policies: dict[BooleanSessionPolicy, dict[str, object]] = {
            "recall": {"enabled": False, "source": "default"},
            "share-reminder": {"enabled": True, "source": "default"},
            "recall-adoption": {"enabled": False, "source": "default"},
            "usage-report": {"enabled": True, "source": "default"},
        }
        for row in rows:
            if row.kind != "plan-reminder":
                policies[row.kind] = {"enabled": row.enabled, "source": "workspace"}
        return policies

    async def set_session_policy(self, kind: BooleanSessionPolicy, enabled: bool) -> dict[str, object]:
        self.require_reviewer()
        if self.scope.project is not None:
            raise KnowledgeError("KNOWLEDGE_INVALID_SCOPE", "会话策略在工作空间级别配置。", 422)
        await self.lock_session_policy(kind, update=True)
        value = {"enabled": enabled, "source": "workspace"}
        if (await self.session_policies())[kind] != value:
            self.db.add(KnowledgeSessionPolicy(
                workspace_id=self.scope.workspace, kind=kind, enabled=enabled,
                updated_by=self.scope.user, updated_at=datetime.now(UTC),
            ))
        return value

    async def plan_reminder_policy(self) -> PlanReminderSettings:
        latest = await self.db.scalar(select(KnowledgeSessionPolicy).where(
            KnowledgeSessionPolicy.workspace_id == self.scope.workspace,
            KnowledgeSessionPolicy.kind == "plan-reminder",
        ).order_by(KnowledgeSessionPolicy.id.desc()).limit(1))
        if latest is None:
            return {"enabled": True, "language": None, "message": None, "source": "default"}
        return {
            "enabled": latest.enabled, "language": latest.plan_language, "message": latest.plan_message,
            "source": "workspace",
        }

    async def set_plan_reminder_policy(self, change: PlanReminderChange) -> PlanReminderSettings:
        """在同一事务读取最新设置并追加变更人，防止并发修改覆盖其他字段。"""
        self.require_reviewer()
        if self.scope.project is not None:
            raise KnowledgeError("KNOWLEDGE_INVALID_SCOPE", "计划召回提醒在工作空间级别配置。", 422)
        await self.lock_session_policy("plan-reminder", update=True)
        current = await self.plan_reminder_policy()
        value: PlanReminderSettings = {
            "enabled": change.get("enabled", current["enabled"]),
            "language": change.get("language", current["language"]),
            "message": change.get("message", current["message"]),
            "source": "workspace",
        }
        if value != current:
            self.db.add(KnowledgeSessionPolicy(
                workspace_id=self.scope.workspace, kind="plan-reminder", enabled=value["enabled"],
                plan_language=value["language"], plan_message=value["message"],
                updated_by=self.scope.user, updated_at=datetime.now(UTC),
            ))
        return value

    async def require_usage_reporting(self) -> None:
        """在统计写入事务内复核工作空间策略，使停用与新上报串行。"""
        await self.lock_session_policy("usage-report", update=False)
        if (await self.session_policies())["usage-report"]["enabled"] is not True:
            raise KnowledgeError("KNOWLEDGE_USAGE_DISABLED", "工作空间已关闭活动统计上报。", 403)

    async def lock_session_policy(self, kind: SessionPolicyKind, *, update: bool) -> None:
        if self.db.get_bind().dialect.name == "postgresql":
            # 策略更新与读取该策略的评分结算串行，确保撤销及时生效。
            key = hashlib.sha256(f"knowledge-session:{self.scope.workspace}:{kind}".encode()).digest()[:8]
            lock = func.pg_advisory_xact_lock if update else func.pg_advisory_xact_lock_shared
            await self.db.execute(select(lock(int.from_bytes(key, "big", signed=True))))

    async def set_hint_policy(self, enabled: bool, lookback_days: int, max_mrs: int) -> dict[str, object]:
        self.require_reviewer()
        if self.scope.project is not None:
            raise KnowledgeError("KNOWLEDGE_INVALID_SCOPE", "提醒团队设置在工作空间级别配置。", 422)
        value = {"enabled": enabled, "lookback_days": lookback_days, "max_mrs": max_mrs, "source": "workspace"}
        if await self.hint_policy() != value:
            self.db.add(KnowledgeHintPolicy(
                workspace_id=self.scope.workspace, enabled=enabled, lookback_days=lookback_days,
                max_mrs=max_mrs, updated_by=self.scope.user, updated_at=datetime.now(UTC),
            ))
        return value

    async def imported_reviews(self, urls: list[str]) -> list[str]:
        """曾发布过的来源无需再提示，包含改版和归档前的历史。"""
        ref = KnowledgeRevision.source["ref"].as_string()
        github = "https://github.com/"
        canonical_github = {url.lower() for url in urls if url.lower().startswith(github)}
        result = await self.db.scalars(select(ref).distinct().join(
            KnowledgeEntry, KnowledgeEntry.id == KnowledgeRevision.entry_id,
        ).where(
            KnowledgeEntry.workspace_id == self.scope.workspace, visible(self.scope),
            KnowledgeEntry.kind == "learning", KnowledgeRevision.status == "published",
            KnowledgeRevision.source["type"].as_string() == "pull_request",
            or_(ref.in_(urls), func.lower(ref).in_(canonical_github)),
        ))
        found = set(result.all())
        folded = {url.lower() for url in found if url.lower().startswith(github)}
        return sorted({url for url in urls if url in found or url.lower() in folded})

    async def pending(self, limit: int) -> dict[str, object]:
        self.require_reviewer()
        rows = (await self.db.execute(
            select(KnowledgeEntry, KnowledgeRevision)
            .join(KnowledgeRevision, KnowledgeRevision.entry_id == KnowledgeEntry.id)
            .where(
                KnowledgeEntry.workspace_id == self.scope.workspace,
                KnowledgeEntry.project_id == self.scope.project,
                KnowledgeRevision.status == "pending",
            ).order_by(KnowledgeRevision.created_at, KnowledgeEntry.id).limit(limit)
        )).all()
        return {"items": [await self.view(entry, revision, include_body=True) for entry, revision in rows]}

    async def _record(
        self, entry_id: UUID, session: str, *, returned_revision: int | None = None,
        opened_revision: int | None = None, positive: bool | None = None, credit_open: bool = True,
        operation: UUID | None = None, occurred_at: datetime | None = None, actor: UUID | None = None,
    ) -> None:
        expected = opened_revision if opened_revision is not None else returned_revision
        entry = await self._entry(entry_id, refresh=expected is not None)
        if entry.status != "active":
            raise missing()
        if self.scope.namespaces is not None:
            selectable = select(KnowledgeEntry.id).where(
                KnowledgeEntry.id == entry.id, namespace_visible(self.scope.namespaces),
            )
            if await self.db.scalar(selectable) is None:
                raise missing()
        if expected is not None and entry.published_revision != expected:
            raise KnowledgeError("KNOWLEDGE_PUBLICATION_CHANGED", "知识发布版本已变化，请重新打开。", 409)
        if opened_revision is not None and not credit_open:
            return
        key = session_digest(session)
        row = await self.db.get(KnowledgeRecall, (entry_id, self.scope.user, key))
        now = datetime.now(UTC)
        if opened_revision is not None:
            if row is None or row.recall_count == 0 or row.positive is not None or row.last_recalled_at is None:
                return
            last = row.last_recalled_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if not timedelta() <= now - last <= _ADOPTION_WINDOW:
                return
            if row.recalled_revision is not None and row.recalled_revision != opened_revision:
                return
            if actor is not None and not await self.journal.can_adopt(
                session, entry.id, actor, revision=opened_revision,
            ):
                return
        if row is None:
            row = KnowledgeRecall(entry_id=entry_id, user_id=self.scope.user, session_key=key, recall_count=0)
            self.db.add(row)
        if returned_revision is not None:
            row.recall_count += 1
            row.last_recalled_at = now
            if entry.project_id == self.scope.project:
                row.recalled_revision = returned_revision
        if opened_revision is not None:
            row.positive, row.credit_source, row.voted_at = True, "opened", now
        elif positive is not None:
            self._manual_feedback(entry, row, positive, operation, occurred_at or now, now)

    def _manual_feedback(
        self, entry: KnowledgeEntry, row: KnowledgeRecall, positive: bool,
        operation: UUID | None, occurred_at: datetime, now: datetime,
    ) -> None:
        previous = row.credit_source
        _, changed = apply_manual_vote(row, positive, operation, occurred_at, now)
        if changed:
            label = "positive" if positive else "negative"
            if previous == "judge":
                self._event(entry, "judge_overridden", row.recalled_revision, label)
            evidence = json.dumps({
                "decision": label, "operation_id": str(operation) if operation is not None else None,
                "occurred_at": occurred_at.astimezone(UTC).isoformat(),
            }, separators=(",", ":"))
            self._event(entry, "manual_feedback_recorded", entry.published_revision, evidence)

    async def feedback(
        self, entry_id: UUID, session: str, positive: bool, *,
        operation: UUID | None = None, occurred_at: datetime | None = None,
    ) -> None:
        try:
            await self._record(entry_id, session, positive=positive, operation=operation, occurred_at=occurred_at)
        except KnowledgeError as error:
            if error.code != "KNOWLEDGE_NOT_FOUND" or not await self.skill_feedback.vote(
                entry_id, session, positive, operation, occurred_at,
            ):
                raise

    async def totals(self, entry_id: UUID) -> FeedbackTotals:
        statement = select(*recall_aggregates()).where(KnowledgeRecall.entry_id == entry_id)
        return feedback_totals((await self.db.execute(statement)).mappings().one())

    async def adoption_enabled(self, *, lock: bool = False, automatic: bool = False) -> bool:
        if lock:
            await self.lock_session_policy("recall-adoption", update=False)
        # 目录开关负责自动生成问题。手工入口继续按工作空间的采纳授权运行。
        if automatic:
            override = await adoption_override(self)
            if override is not None:
                return override
        return (await self.session_policies())["recall-adoption"]["enabled"] is True

    async def _require_adoption(self, *, lock: bool = False, automatic: bool = False) -> None:
        if not await self.adoption_enabled(lock=lock, automatic=automatic):
            raise KnowledgeError("KNOWLEDGE_ADOPTION_DISABLED", "工作空间尚未启用召回采纳评分。", 403)

    def recall_matches_capture(
        self, session_key: str | ColumnElement[str | None], entry: UUID | ColumnElement[UUID],
        revision: int | ColumnElement[int | None], captured_at: datetime | ColumnElement[datetime | None],
    ) -> ColumnElement[bool]:
        run, hit = KnowledgeRecallRun, KnowledgeRecallRunHit
        return select(run.id).join(hit, hit.run_id == run.id).where(
            run.workspace_id == self.scope.workspace, run.project_id == self.scope.project,
            run.user_id == self.scope.user, run.session_key == session_key, run.created_at <= captured_at,
            hit.entry_id == entry, hit.revision == revision, hit.eligible.is_(True),
        ).correlate(KnowledgeEntry, KnowledgeRecall, KnowledgeSharedSessionRevision).exists()

    async def adoption_candidates(
        self, session: str, limit: int, after: UUID | None, evidence: AdoptionEvidence | None = None,
        *, template: KnowledgeSessionScorer | None = None, include_resolved: bool = False,
    ) -> dict[str, object]:
        """只提供当前成员在指定作用域实际召回的不可变正文片段。"""
        await self._require_adoption()
        session_key = session_digest(session)
        shown = aliased(KnowledgeRevision)
        statement = published(self.scope).join(KnowledgeRecall, KnowledgeRecall.entry_id == KnowledgeEntry.id).join(
            shown, (shown.entry_id == KnowledgeEntry.id) & (shown.revision == KnowledgeRecall.recalled_revision),
        ).where(
            KnowledgeEntry.project_id == self.scope.project,
            KnowledgeRecall.user_id == self.scope.user, KnowledgeRecall.session_key == session_key,
            KnowledgeRecall.recall_count > 0,
        ).with_only_columns(
            KnowledgeEntry.id, KnowledgeEntry.kind, shown.revision, shown.title, func.substr(shown.body, 1, 1200),
            shown.source,
        )
        if not include_resolved:
            statement = statement.where(KnowledgeRecall.positive.is_(None))
        if evidence is not None:
            statement = statement.where(self.recall_matches_capture(
                session_key, KnowledgeEntry.id.expression, KnowledgeRecall.recalled_revision.expression,
                evidence.captured_at,
            ))
        if after is not None:
            statement = statement.where(KnowledgeEntry.id > after)
        rows = (await self.db.execute(statement.order_by(KnowledgeEntry.id).limit(limit + 1))).tuples().all()
        items: list[dict[str, object]] = []
        for entry, kind, revision, title, body, source in rows[:limit]:
            item: dict[str, object] = {
                "id": str(entry), "kind": kind, "revision": revision, "title": title, "excerpt": body,
                "name": adoption_question(entry)[0],
            }
            if template is not None and (rubric := render_adoption_rubric(template, entry, source)) is not None:
                item["rubric"] = rubric
            items.append(item)
        return {"items": items, "next_after": str(rows[limit - 1][0]) if len(rows) > limit else None}

    async def judge_adoption(
        self, session: str, verdicts: dict[UUID, tuple[int, bool]], evidence: AdoptionEvidence | None = None,
    ) -> dict[str, object]:
        return await self.judge_adoption_for_key(session_digest(session), verdicts, evidence)

    async def judge_adoption_for_key(
        self, session_key: str, verdicts: dict[UUID, tuple[int, bool]], evidence: AdoptionEvidence | None,
        *, automatic: bool = False,
    ) -> dict[str, object]:
        """同一根会话的采纳只记一次，和正文打开共享条目行锁。"""
        await self._require_adoption(lock=True, automatic=automatic)
        candidates = await self.db.scalars(select(KnowledgeEntry).where(
            KnowledgeEntry.workspace_id == self.scope.workspace, KnowledgeEntry.project_id == self.scope.project,
            visible(self.scope), KnowledgeEntry.id.in_(verdicts), KnowledgeEntry.status == "active",
        ).order_by(KnowledgeEntry.id).with_for_update())
        applied: set[UUID] = set()
        now = datetime.now(UTC)
        audit = session_key if evidence is None else json.dumps({
            "session_key": session_key, "archive_id": str(evidence.archive_id), "archive_revision": evidence.revision,
            **({"attempt_id": str(evidence.attempt_id), "evidence": "local" if evidence.local else "archive"}
               if evidence.attempt_id is not None else {}),
            **({"judge_call_id": str(evidence.judge_call_id)} if evidence.judge_call_id is not None else {}),
        })
        for entry in candidates:
            revision, adopted = verdicts[entry.id]
            row = await self.db.get(KnowledgeRecall, (entry.id, self.scope.user, session_key))
            if row is None or row.recall_count == 0 or row.recalled_revision != revision or row.positive is not None:
                continue
            if evidence is not None and not await self.db.scalar(select(
                self.recall_matches_capture(session_key, entry.id, revision, evidence.captured_at),
            )):
                continue
            row.positive, row.credit_source, row.voted_at = adopted, "judge", now
            self._event(entry, "judge_adopted" if adopted else "judge_ignored", revision, audit)
            applied.add(entry.id)
        return {
            "applied": [str(entry) for entry in verdicts if entry in applied],
            "skipped": [str(entry) for entry in verdicts if entry not in applied],
        }

    async def quality_candidates(
        self, *, after: UUID | None = None, limit: int = 100, include_shared: bool = False,
    ) -> dict[str, object]:
        """只列出达到阈值的正式资源，已提交修订的条目交由审核队列处理。"""
        self.require_reviewer()
        pending = select(KnowledgeRevision.entry_id).where(KnowledgeRevision.status == "pending")
        stmt = lifecycle_published(self.scope, include_shared).where(
            KnowledgeEntry.kind.in_(("rule", "doc", "skill")),
            KnowledgeEntry.id.not_in(pending),
        ).order_by(KnowledgeEntry.id).limit(limit + 1)
        if after:
            stmt = stmt.where(KnowledgeEntry.id > after)
        catalog = self._quality_catalog(include_shared)
        skill_page = select(catalog).order_by(catalog.c.id).limit(limit + 1)
        if after:
            skill_page = skill_page.where(catalog.c.id > after)
        page_catalog = skill_page.cte("registry_quality_page")
        skill_stmt = registry_quality_feedback(page_catalog).order_by(page_catalog.c.id)
        # 两种资源共享一个按资产 ID 前进的游标，浏览空候选页也会继续。
        entries = {entry.id: (entry, version) for entry, version in await self.db.execute(stmt)}
        skills = {row["id"]: row for row in (await self.db.execute(skill_stmt)).mappings()}
        ids = sorted(entries.keys() | skills.keys())
        candidates: list[dict[str, object]] = []
        for entry_id in ids[:limit]:
            if entry_id in entries:
                entry, version = entries[entry_id]
                totals = await self.totals(entry_id)
                metadata: dict[str, object] = {"title": version.title, "kind": entry.kind, "revision": version.revision}
                origin = entry.project_id
            else:
                skill = skills[entry_id]
                totals = feedback_totals(skill)
                metadata = {"title": skill["title"], "kind": "skill", "skill_version_id": str(skill["version_id"])}
                origin = skill["project_id"]
            if include_shared:
                metadata["project_id"] = str(origin) if origin else None
            if totals.needs_quality_revision():
                candidates.append({
                    "id": str(entry_id), **metadata,
                    "recalls": totals.recalls, "upvotes": totals.upvotes, "recallers": totals.recallers,
                })
        return {"items": candidates, "next_after": str(ids[limit - 1]) if len(ids) > limit else None}

    def _quality_catalog(self, include_shared: bool) -> CTE:
        return registry_quality_catalog(
            self.scope.workspace, self.scope.project, include_shared=include_shared,
            directories=self.scope.skill_directories if include_shared else None,
        )

    async def quality_context(self, entry_id: UUID, *, include_shared: bool = False) -> dict[str, object]:
        """读取已发布正文和少量相关、已获采纳且当前可见的经验。"""
        self.require_reviewer()
        row = (await self.db.execute(lifecycle_published(self.scope, include_shared).where(
            KnowledgeEntry.id == entry_id,
        ))).one_or_none()
        if row is None:
            return await self._registry_quality_context(entry_id, include_shared=include_shared)
        entry, version = row
        totals = await self.totals(entry.id)
        if entry.kind not in {"rule", "doc", "skill"} or not totals.needs_quality_revision():
            raise conflict("当前知识未达到质量修订的反馈条件。")
        related = await self._adopted_examples(
            version.title, version.tags, version.source.get("ref", ""), origin=entry.project_id,
        )
        return {
            "id": str(entry.id), "kind": entry.kind, "revision": version.revision,
            **({"project_id": entry.project_id} if include_shared else {}),
            "title": version.title, "body": version.body, "tags": version.tags,
            "recalls": totals.recalls, "upvotes": totals.upvotes, "recallers": totals.recallers, "related": related,
        }

    async def _registry_quality_context(self, entry_id: UUID, *, include_shared: bool) -> dict[str, object]:
        catalog = self._quality_catalog(include_shared)
        row = (await self.db.execute(select(catalog).where(catalog.c.id == entry_id))).one_or_none()
        if row is None:
            raise missing()
        reference = PublishedSkillReference(*row[:9])
        totals = await registry_skill_totals(self.db, entry_id)
        if not totals.needs_quality_revision():
            raise conflict("当前 Skill 未达到质量修订的反馈条件。")
        _, body = skill_markdown_parts(await skill_markdown(self.db, self.scope.workspace, reference, self.storage))
        return {
            "id": str(entry_id), "kind": "skill", "skill_version_id": str(reference.version_id),
            **({"project_id": reference.project_id} if include_shared else {}),
            "title": reference.title, "body": body, "tags": row.tags,
            "recalls": totals.recalls, "upvotes": totals.upvotes, "recallers": totals.recallers,
            "related": await self._adopted_examples(
                reference.title, row.tags, reference.path, origin=reference.project_id,
            ),
        }

    async def _adopted_examples(
        self, title: str, tags: list[str], ref: str, *, origin: UUID | None,
    ) -> list[dict[str, str]]:
        name = ref.replace("\\", "/").rsplit("/", 1)[-1]
        topic = keywords(" ".join([title, *tags, name.removesuffix(".md")]))
        if not topic:
            return []
        votes = (
            select(KnowledgeRecall.entry_id, func.count().label("count"))
            .where(KnowledgeRecall.positive.is_(True)).group_by(KnowledgeRecall.entry_id).subquery()
        )
        examples = published(self.scope).where(KnowledgeEntry.kind == "learning", ~overridden(self.scope))
        if origin is None:
            # 共享草稿面向整个工作空间，正文生成不能使用项目专属证据。
            examples = examples.where(KnowledgeEntry.project_id.is_(None))
        adopted = examples.join(votes, votes.c.entry_id == KnowledgeEntry.id).with_only_columns(
            KnowledgeEntry.id, KnowledgeRevision.title, votes.c.count,
        )
        ranked: list[tuple[int, int, UUID]] = []
        rows = await self.db.stream(adopted.execution_options(yield_per=100))
        try:
            async for entry_id, title, count in rows.tuples():
                if relevance := len(topic & keywords(title)):
                    ranked.append((relevance, count, entry_id))
        finally:
            await rows.close()
        keys = [item[2] for item in sorted(ranked, key=lambda item: (-item[0], -item[1], item[2]))[:5]]
        if not keys:
            return []
        visible_rows = await self.db.execute(examples.where(
            KnowledgeEntry.id.in_(keys),
        ).with_only_columns(KnowledgeEntry.id, KnowledgeRevision.title, KnowledgeRevision.body))
        indexed = {entry_id: (title, body) for entry_id, title, body in visible_rows}
        return [
            {"id": str(entry_id), "title": indexed[entry_id][0], "body": indexed[entry_id][1][:4000]}
            for entry_id in keys if entry_id in indexed
        ]

    async def submit_quality_revision(
        self, entry_id: UUID, base: int | UUID, body: str, operation: UUID,
    ) -> dict[str, object]:
        self.require_reviewer()
        # 幂等摘要绑定草稿、条目、旧版本和作用域，重放时无需重新读取已切换的基线。
        identity = str(base) if isinstance(base, UUID) else base
        payload = json.dumps(["quality", str(self.scope.project), str(entry_id), identity, body], ensure_ascii=False)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        if result := await self._replay(operation, fingerprint):
            return result
        if isinstance(base, UUID):
            return await self._submit_registry_quality(entry_id, base, body, operation, fingerprint)
        entry = await self._entry(entry_id, exact_scope=True)
        if result := await self._replay(operation, fingerprint):
            return result
        if entry.status != "active" or entry.published_revision != base:
            raise conflict("知识基准版本已变化，请重新生成草稿。")
        kind = entry.kind
        if kind not in ("rule", "doc", "skill") or not (await self.totals(entry.id)).needs_quality_revision():
            raise conflict("当前知识未达到质量修订的反馈条件。")
        version = await self.db.get(KnowledgeRevision, (entry.id, base))
        if version is None:
            raise missing()
        if body.strip() == version.body.strip():
            raise conflict("草稿正文没有变化。")
        value = Contribution(
            operation, type_cast(KnowledgeKind, kind), version.title, body, version.tags,
            version.source, version.relations, base_revision=base, rule_paths=version.rule_paths,
        )
        try:
            async with self.db.begin_nested():
                inherited = await saved_declarations(self.db, version) if version.relation_findings is not None else []
                entry, updated = await self._contribute(
                    value, entry_id, fingerprint, publish_mr=False, quality=True, inherited=inherited,
                )
        except IntegrityError:
            if result := await self._replay(operation, fingerprint):
                return result
            raise
        self._event(entry, "quality_draft", updated.revision, str(base))
        return await self.view(entry, updated)

    async def _submit_registry_quality(
        self, skill_id: UUID, base: UUID, body: str, operation: UUID, fingerprint: str,
    ) -> dict[str, object]:
        scope = self.scope
        # Registry 行锁同时保护发布版本和待审提案数量，提交不锁审核记录。
        reference = await published_skill_reference(
            self.db, scope.workspace, scope.project, identity=skill_id, lock=True,
        )
        if result := await self._replay(operation, fingerprint):
            return result
        if reference is None or reference.project_id != scope.project:
            raise missing()
        if reference.version_id != base:
            raise conflict("Skill 基准版本已变化，请重新生成草稿。")
        catalog = standalone_skill_catalog(scope.workspace, scope.project, delivered=False)
        metadata = select(catalog.c.tags, catalog.c.title).where(catalog.c.id == skill_id)
        row = (await self.db.execute(metadata)).one_or_none()
        if row is None:
            raise missing()
        pending = pending_skill_proposals(scope.workspace, scope.project)
        if await self.db.scalar(select(pending.c.skill_id).where(pending.c.skill_id == str(skill_id))):
            raise conflict("该 Skill 已有待审核的质量修订。")
        if not (await registry_skill_totals(self.db, skill_id)).needs_quality_revision():
            raise conflict("当前 Skill 未达到质量修订的反馈条件。")
        _, previous = skill_markdown_parts(await skill_markdown(self.db, scope.workspace, reference, self.storage))
        if body.strip() == previous.strip():
            raise conflict("草稿正文没有变化。")
        value = Contribution(operation, "skill", row.title, body, row.tags, {
            "type": REGISTRY_QUALITY_SOURCE, "skill_id": str(skill_id), "base_skill_version_id": str(base),
            "ref": reference.path, "resource_path": f"{reference.path}/SKILL.md",
        }, [])
        try:
            async with self.db.begin_nested():
                entry, revised = await self._contribute(value, None, fingerprint, publish_mr=False, quality=True)
        except IntegrityError:
            if result := await self._replay(operation, fingerprint):
                return result
            raise
        self._event(entry, "quality_draft", revised.revision, str(base))
        return await self.view(entry, revised)

    def _promotion_sources(self, include_shared: bool) -> Select[tuple[KnowledgeEntry, KnowledgeRevision]]:
        target = aliased(KnowledgeEntry)
        claimed = select(target.id).where(
            target.promoted_from == KnowledgeEntry.id, target.status.in_(("pending", "active")),
        )
        return lifecycle_published(self.scope, include_shared).where(
            KnowledgeEntry.kind == "learning", ~claimed.exists(),
        )

    async def promotion_candidates(
        self, *, limit: int, after: UUID | None, include_shared: bool = False,
    ) -> dict[str, object]:
        """按作用域列出已满足阈值的经验，普通成员也能提出待审晋升。"""
        now = datetime.now(UTC)
        measures = (
            select(KnowledgeRecall.entry_id, *recall_aggregates())
            .join(KnowledgeEntry, KnowledgeEntry.id == KnowledgeRecall.entry_id)
            .where(
                KnowledgeEntry.workspace_id == self.scope.workspace, lifecycle_projects(self.scope, include_shared),
            )
            .group_by(KnowledgeRecall.entry_id)
            .subquery()
        )
        sources = self._promotion_sources(include_shared)
        date = lifecycle_date()
        statement = sources.join(measures, measures.c.entry_id == KnowledgeEntry.id).where(
            date <= now - timedelta(days=14), measures.c.upvotes >= 5, measures.c.voters >= 2,
        ).with_only_columns(
            KnowledgeEntry.id, KnowledgeEntry.project_id, date.label("authored_at"), KnowledgeRevision.title,
            KnowledgeRevision.body, KnowledgeRevision.revision,
            *(column for column in measures.c if column.key != "entry_id"),
        ).order_by(KnowledgeEntry.id)
        if after is not None:
            statement = statement.where(KnowledgeEntry.id > after)
        rows = await self.db.stream(statement.execution_options(yield_per=100))
        items: list[dict[str, object]] = []
        try:
            async for row in rows.mappings():
                totals = feedback_totals(row)
                if not totals.can_promote(row["authored_at"], now):
                    continue
                items.append({
                    "id": str(row["id"]), "title": row["title"], "revision": row["revision"],
                    "kind": suggested_kind(row["title"], row["body"]), "confidence": totals.confidence(now),
                    "upvotes": totals.upvotes, "voters": totals.voters,
                    **({"project_id": row["project_id"]} if include_shared else {}),
                })
                if len(items) > limit:
                    break
        finally:
            await rows.close()
        return {"items": items[:limit], "next_after": items[limit - 1]["id"] if len(items) > limit else None}

    async def promotion_context(self, entry_id: UUID, *, include_shared: bool = False) -> dict[str, object]:
        """预览可晋升的发布版本，冲突必须在原条目的范围内检查。"""
        row = (await self.db.execute(
            self._promotion_sources(include_shared).where(KnowledgeEntry.id == entry_id),
        )).one_or_none()
        if row is None:
            raise missing()
        entry, version = row
        if not (await self.totals(entry.id)).can_promote(version.authored_at or entry.created_at, datetime.now(UTC)):
            raise conflict("当前经验尚未达到晋升条件。")
        targets = promotion_targets(entry.id, await self._first_path(entry.id))
        return {
            "id": str(entry.id), "revision": version.revision, "title": version.title, "body": version.body,
            "kind": suggested_kind(version.title, version.body), "project_id": entry.project_id,
            "targets": targets,
            "occupied_kinds": sorted(await self._occupied_promotion_targets(targets, entry.project_id)),
        }

    async def _occupied_promotion_targets(
        self, targets: dict[PromotionKind, str], project: UUID | None, *, promotions_only: bool = False,
    ) -> set[str]:
        paths = [
            (kind, hashlib.sha256(canonical.encode()).hexdigest())
            for kind, path in targets.items()
            if (canonical := declared_path(path) or normalize_resource_path(path)) is not None
        ]
        indexed = KnowledgeMarkdownPath
        # 待审条目尚无发布版本，也已经占用其首发草稿的逻辑路径。
        candidates = select(KnowledgeEntry.kind).join(indexed, indexed.entry_id == KnowledgeEntry.id)
        if promotions_only:
            candidates = candidates.where(KnowledgeEntry.promoted_from.is_not(None))
        rows = await self.db.scalars(candidates.where(
            KnowledgeEntry.workspace_id == self.scope.workspace, KnowledgeEntry.project_id == project,
            indexed.revision == 1, indexed.position == 0,
            tuple_(KnowledgeEntry.kind, indexed.key).in_(paths),
            KnowledgeEntry.status.in_(("pending", "active")),
        ))
        occupied = set(rows)
        if not promotions_only and (target := targets.get("skill")) is not None and "skill" not in occupied:
            registered = await published_skill_reference(
                self.db, self.scope.workspace, project, path=declared_path(target),
            )
            if registered is not None and registered.project_id == project:
                occupied.add("skill")
        return occupied

    async def _archived_skill_promotion(self, source: UUID, path: str) -> tuple[UUID, int] | None:
        """在原经验、原项目和精确路径内查找可复用的历史发布资产。"""
        paths = KnowledgeMarkdownPath
        rows = (await self.db.execute(
            select(KnowledgeEntry.id, KnowledgeEntry.published_revision).join(
                paths, paths.entry_id == KnowledgeEntry.id,
            ).where(
                KnowledgeEntry.workspace_id == self.scope.workspace, KnowledgeEntry.project_id == self.scope.project,
                KnowledgeEntry.promoted_from == source, KnowledgeEntry.kind == "skill",
                KnowledgeEntry.status == "archived", KnowledgeEntry.published_revision.is_not(None),
                paths.revision == 1, paths.position == 0, paths.key == hashlib.sha256(path.encode()).hexdigest(),
            ).limit(2)
        )).tuples().all()
        if len(rows) > 1:
            raise conflict("存在多份同路径的归档 Skill，请先核对历史资产。")
        if not rows:
            return None
        entry, revision = rows[0]
        assert revision is not None
        return entry, revision

    async def submit_promotion(
        self, entry_id: UUID, *, operation: UUID, base: int, kind: PromotionKind, reason: str, body: str,
    ) -> dict[str, object]:
        """锁定目标范围及原经验，待审稿保留来源版本和晋升理由。"""
        await self._lock_promotion_targets()
        entry = await self._entry(entry_id, exact_scope=True)
        version = await self.db.get(KnowledgeRevision, (entry.id, base))
        if version is None:
            raise conflict("晋升所依据的发布版本已变化。")
        path = promotion_path(entry.id, kind, await self._first_path(entry.id))
        source = {
            "type": "manual", "ref": str(entry.id), "resource_path": path,
            "promotion_revision": str(base), "promotion_reason": reason,
        }
        value = Contribution(
            operation, kind, version.title, body, version.tags, source,
            [{"kind": "REFERENCES", "target": str(entry.id)}], promoted_from=entry.id,
        )
        if replay := await self._replay(operation, value.digest(self.scope, None)):
            return replay
        if entry.published_revision != base:
            raise conflict("晋升所依据的发布版本已变化，请重新预览。")
        return await self.contribute(value)

    def _maintenance_page(
        self, limit: int, after: UUID | None, *, write: bool, include_shared: bool,
    ) -> Select[tuple[KnowledgeEntry, KnowledgeRevision]]:
        self.require_reviewer()
        stmt = lifecycle_published(self.scope, include_shared).where(
            KnowledgeEntry.kind == "learning",
        ).order_by(KnowledgeEntry.id).limit(limit + 1)
        if after:
            stmt = stmt.where(KnowledgeEntry.id > after)
        return stmt.with_for_update(of=KnowledgeEntry) if write else stmt

    async def maintenance(
        self, *, archive: bool = False, limit: int = 100, after: UUID | None = None,
        threshold: float = DEFAULT_PRUNE_THRESHOLD, candidates_only: bool = False, include_shared: bool = False,
        entry_id: UUID | None = None,
    ) -> dict[str, object]:
        """返回低置信度检查结果和翻页位置。归档时锁定候选，复核资格与待审版本后写入归档审计。"""
        page = self._maintenance_page(limit, after, write=archive, include_shared=include_shared)
        if entry_id is not None:
            # 控制台只处理用户确认的条目，资格仍在本事务内复核。
            page = page.where(KnowledgeEntry.id == entry_id)
        rows = (await self.db.execute(page)).all()
        now = datetime.now(UTC)
        entries: list[dict[str, object]] = []
        for entry, version in rows[:limit]:
            totals = await self.totals(entry.id)
            confidence = totals.confidence(now)
            authored_at = version.authored_at or entry.created_at
            reason = totals.prune_reason(authored_at, now, threshold)
            if candidates_only and reason is None:
                continue
            # 待审版本仍需原发布条目承接，保留该条目供审核处理。
            if archive and reason:
                pending = await self.db.scalar(select(KnowledgeRevision).where(
                    KnowledgeRevision.entry_id == entry.id, KnowledgeRevision.status == "pending",
                ))
                if pending is None:
                    entry.status = "archived"
                    self._event(entry, "archived", entry.published_revision, reason)

            existing_promotion = await self.db.scalar(select(KnowledgeEntry.id).where(
                KnowledgeEntry.promoted_from == entry.id, KnowledgeEntry.status.in_(("pending", "active")),
            ))
            entries.append({
                "id": entry.id, "title": version.title, "confidence": confidence, "cold": bool(reason),
                **({"project_id": entry.project_id} if include_shared else {}),
                "prune_reason": reason, "status": entry.status,
                "promotable": existing_promotion is None and totals.can_promote(authored_at, now),
                "recalls": totals.recalls, "sessions": totals.sessions, "upvotes": totals.upvotes,
                "voters": totals.voters, "judged": totals.judged, "judge_upvotes": totals.judge_upvotes,
            })
        return {"items": entries, "next_after": str(rows[limit - 1][0].id) if len(rows) > limit else None}

    async def confidence_writeback(
        self, *, apply: bool, limit: int = 100, after: UUID | None = None, include_shared: bool = False,
    ) -> dict[str, object]:
        page = self._maintenance_page(limit, after, write=apply, include_shared=include_shared)
        rows = (await self.db.execute(page)).all()
        now = datetime.now(UTC)
        entries: list[dict[str, object]] = []
        for entry, version in rows[:limit]:
            totals = await self.totals(entry.id)
            if not totals.observations:
                continue
            # 置信度保留两位小数。以整数比较，避免浮点误差触发 0.05 的边界写入。
            score = round(totals.confidence(now) * 100)
            saved = await self.db.get(KnowledgeConfidenceSnapshot, (entry.id, version.revision))
            previous = saved.score if saved else None
            if previous is not None and abs(score - previous) <= 5:
                continue
            entries.append({
                "id": str(entry.id), "revision": version.revision, "title": version.title,
                **({"project_id": entry.project_id} if include_shared else {}),
                "confidence": score / 100, "previous_confidence": previous / 100 if previous is not None else None,
            })
            if apply:
                if saved is None:
                    saved = KnowledgeConfidenceSnapshot(entry_id=entry.id, revision=version.revision)
                    self.db.add(saved)
                saved.score, saved.updated_at, saved.updated_by = score, now, self.scope.user
                reason = json.dumps({"previous": previous, "score": score}, separators=(",", ":"))
                self._event(entry, "confidence_updated", version.revision, reason)
        return {
            "items": entries, "preview": not apply,
            "next_after": str(rows[limit - 1][0].id) if len(rows) > limit else None,
        }

    async def restore(self, entry_id: UUID, request_id: str) -> dict[str, object]:
        """归档保留发布版本，恢复时重新使用该版本及原召回记录。"""
        self.require_reviewer()
        await self._lock_promotion_targets()
        entry = await self._entry(entry_id, exact_scope=True)
        if entry.codebase_id is not None:
            raise conflict("代码证据由索引管理，请重新导入来源。")
        if entry.published_revision is None or entry.status not in {"archived", "active"}:
            raise conflict("只有存在发布版本的条目可以恢复。")
        version = await self.db.get(KnowledgeRevision, (entry.id, entry.published_revision))
        if version is None:
            raise missing()
        if entry.kind == "skill" and entry.promoted_from is not None:
            asset = await self.db.scalar(select(KnowledgeEvent.reason).where(
                KnowledgeEvent.entry_id == entry.id, KnowledgeEvent.revision == entry.published_revision,
                KnowledgeEvent.action == "skill_published",
            ))
            if asset is None:
                raise conflict("已发布的知识版本缺少对应的 Skill 资产。")
            registry = await scoped_skill_registry(
                self.db, entry.project_id, self.storage, publication=KNOWLEDGE_SKILL_PUBLICATION,
            )
            try:
                await registry.set_archived(
                    actor=SkillActor(self.scope.user, self.scope.workspace, self.scope.role),
                    workspace_id=self.scope.workspace, skill_id=UUID(asset), archived=False, request_id=request_id,
                )
            except SkillRegistryError as exc:
                raise KnowledgeError("KNOWLEDGE_SKILL_RESTORE_FAILED", exc.message, 409) from exc
            # Registry 的子事务已同步状态，主会话重新读取后再返回。
            await self.db.refresh(entry)
        elif entry.status == "archived":
            entry.status = "active"
            self._event(entry, "restored", entry.published_revision)
        return await self.view(entry, version)

    async def reconcile_code(self, *, preview: bool) -> dict[str, object]:
        project = self.scope.project
        if project is None:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_SCOPE", "产品文档调和需要选择项目。", 422)
        if not preview:
            self.require_reviewer()
            await serialize_publication(self.db, project)
        scoped = published(self.scope).where(~overridden(self.scope))
        report = await build_report(self.db, scoped, project, preview=preview)
        if preview:
            return {**report, "preview": True, "changed": False}
        changes = await replace_mappings(self.db, project, report["mappings"])
        for entry in await changed_products(self.db, self.scope.workspace, project, tuple(changes)):
            upserted, removed = changes[entry.id]
            self._event(entry, "code_reconciled", entry.published_revision, f"写入 {upserted} 条，移除 {removed} 条。")
        return {**report, "preview": False, "changed": bool(changes)}

    async def _derived_for(
        self, items: Sequence[tuple[KnowledgeEntry, KnowledgeRevision]], *,
        skills: Sequence[PublishedSkillReference] = (), queue_hidden: frozenset[UUID] = frozenset(),
        cross_edges: Sequence[PublishedCrossEdge] | None = None,
    ) -> dict[UUID, list[dict[str, str]]]:
        scoped = published(self.scope).where(~overridden(self.scope))
        if queue_hidden:
            scoped = scoped.where(KnowledgeEntry.id.not_in(queue_hidden))
        links = await markdown_relations(
            self.db, scoped, items, workspace=self.scope.workspace, project=self.scope.project, skills=skills,
            skill_directories=self.scope.skill_directories,
        )
        project = self.scope.project
        if project is None:
            return links
        rules: dict[UUID, int] = {}
        pages: dict[UUID, int] = {}
        reconciliation: dict[UUID, int] = {}
        for entry, revision in items:
            if entry.project_id != project or entry.status != "active" or entry.published_revision != revision.revision:
                continue
            if entry.kind == "rule" and revision.rule_paths:
                rules[entry.id] = revision.revision
            elif entry.codebase_id is not None and is_code(revision.source):
                reconciliation[entry.id] = revision.revision
                if revision.source.get("path"):
                    pages[entry.id] = revision.revision
            elif entry.kind == "doc" and is_product(revision.source.get("resource_path", "")):
                reconciliation[entry.id] = revision.revision
        if not rules and not pages and not reconciliation:
            return links
        edges = await derived_path_edges(self.db, scoped, project, set(rules), set(pages))
        relations: dict[UUID, list[dict[str, str]]] = defaultdict(list)
        for edge in edges:
            # 召回后若有并发审核，保持返回的正文版本和关系版本一致。
            if rules.get(edge.rule) == edge.rule_revision:
                relations[edge.rule].append({
                    "kind": "MAPS_TO", "target": str(edge.page), "title": edge.page_title, "direction": "out",
                })
            if pages.get(edge.page) == edge.page_revision:
                relations[edge.page].append({
                    "kind": "MAPS_TO", "target": str(edge.rule), "title": edge.rule_title, "direction": "in",
                })
        if pages:
            if cross_edges is None:
                cross_edges = await published_cross_repository_edges(
                    self.db, scoped, project, self.scope.code_connections, set(pages),
                )
            for link in cross_edges:
                if pages.get(link.source.identity) == link.source.revision:
                    relations[link.source.identity].append(link.view(inbound=False))
                if pages.get(link.target.identity) == link.target.revision:
                    relations[link.target.identity].append(link.view(inbound=True))
        if reconciliation:
            for key, mappings in (await active_mappings(self.db, scoped, project, reconciliation)).items():
                relations[key].extend(mappings)
        for derived in relations.values():
            derived.sort(key=lambda link: (link["title"], link["target"]))
        for entry_id, references in links.items():
            existing = {(link["target"], link["direction"]) for link in relations[entry_id]}
            relations[entry_id].extend(
                link for link in references
                if link.get("via") == "declared_path" or (link["target"], link["direction"]) not in existing
            )
        return relations

    async def _relations(
        self, entry_id: UUID, revision: KnowledgeRevision, derived: list[dict[str, str]],
        *, queue_hidden: frozenset[UUID] = frozenset(),
    ) -> list[dict[str, str]]:
        # 先解析目标的可见性，避免通过关系泄漏其他项目中的条目存在性。
        relations = [
            link for link in revision.relations if link.get("derived") != "iwiki" or revision.status == "published"
        ]
        keys = [UUID(relation["target"]) for relation in relations]
        scoped = published(self.scope).where(~overridden(self.scope))
        if queue_hidden:
            scoped = scoped.where(KnowledgeEntry.id.not_in(queue_hidden))
        stmt = scoped.where(KnowledgeEntry.id.in_(keys)).with_only_columns(
            KnowledgeEntry.id, KnowledgeRevision.title, KnowledgeRevision.revision,
        )
        targets = {
            key: (title, version) for key, title, version in (await self.db.execute(stmt)).tuples()
        } if keys else {}
        stored_outgoing = [
            rendered for item, key in zip(relations, keys, strict=True)
            if (current := targets.get(key))
            and (rendered := self._relation_view(item, key, current[0], "out", revision.revision, current[1]))
        ]
        outgoing = [row for row in stored_outgoing if row.get("derived") != "iwiki"]
        if len(outgoing) >= 5:
            return outgoing[:5]
        target = str(entry_id)
        if self.db.get_bind().dialect.name == "postgresql":
            stored_relations = cast(KnowledgeRevision.relations, JSONB)
            scoped = scoped.where(stored_relations.contains([{"target": target}]))
            links = func.jsonb_array_elements(stored_relations).table_valued("value")
            record = cast(links.c.value, JSONB)
            target_match = record["target"].as_string()
            link_kind = record["derived"].as_string()
            source_version = record["source_revision"].as_string()
            target_version = record["target_revision"].as_string()
        else:
            # SQLite 仅用于隔离测试，使用同样的版本及数组成员条件。
            links = func.json_each(KnowledgeRevision.relations).table_valued("value")
            target_match = func.json_extract(links.c.value, "$.target")
            link_kind = func.json_extract(links.c.value, "$.derived")
            source_version = func.json_extract(links.c.value, "$.source_revision")
            target_version = func.json_extract(links.c.value, "$.target_revision")
        current_wiki = (
            (source_version == cast(KnowledgeRevision.revision, String))
            & (target_version == str(revision.revision))
            & (KnowledgeRevision.status == "published")
        ) if revision.status == "published" else false()
        references = select(links.c.value).where(
            target_match == target, or_(link_kind.is_distinct_from("iwiki"), current_wiki),
        ).correlate(KnowledgeRevision).exists()
        inbound = scoped.where(
            references, KnowledgeEntry.id != entry_id, KnowledgeRevision.source["wiki_page"].as_string().is_(None),
        ).with_only_columns(
            KnowledgeEntry.id, KnowledgeRevision.title, KnowledgeRevision.relations, KnowledgeRevision.revision,
        ).order_by(KnowledgeEntry.id).limit(5)
        stored_incoming = [
            rendered for origin, title, stored, source_revision in (await self.db.execute(inbound)).tuples()
            for link in stored if link["target"] == target
            and (link.get("derived") != "iwiki" or revision.status == "published")
            and (rendered := self._relation_view(link, origin, title, "in", source_revision, revision.revision))
        ]
        incoming = [row for row in stored_incoming if row.get("derived") != "iwiki"]
        path_declared = [
            {key: value for key, value in row.items() if key != "via"}
            for row in derived if row.get("via") == "declared_path"
        ]
        ordered = outgoing + [row for row in path_declared if row["direction"] == "out"]
        ordered.extend(incoming + [row for row in path_declared if row["direction"] == "in"])
        declared = []
        seen = set()
        for row in ordered:
            key = row["kind"], row["target"], row["direction"]
            if key not in seen:
                seen.add(key)
                declared.append(row)
        pairs = {(row["target"], row["direction"]) for row in declared}
        iwiki = [
            row for row in stored_outgoing + stored_incoming
            if row.get("derived") == "iwiki" and (row["target"], row["direction"]) not in pairs
        ]
        pairs.update((row["target"], row["direction"]) for row in iwiki)
        automatic = []
        for row in derived:
            key = row["kind"], row["target"], row["direction"]
            if row.get("via") != "declared_path" and key not in seen and key[1:] not in pairs:
                seen.add(key)
                automatic.append(row)
        return (declared + iwiki + automatic)[:5]

    @staticmethod
    def _relation_view(
        link: dict[str, str], target: UUID, title: str, direction: str,
        source_revision: int, target_revision: int,
    ) -> dict[str, str]:
        if link.get("derived") == "iwiki":
            version = (link.get("source_revision"), link.get("target_revision"))
            if version != (str(source_revision), str(target_revision)):
                return {}
            return link | {"target": str(target), "title": title, "direction": direction}
        return {"kind": link["kind"], "target": str(target), "title": title, "direction": direction}

    async def view(
        self, entry: KnowledgeEntry, revision: KnowledgeRevision, *, include_body: bool = False,
        derived: list[dict[str, str]] | None = None, queue_hidden: frozenset[UUID] = frozenset(),
    ) -> dict[str, object]:
        include_findings = derived is None and revision.relation_findings is not None
        if derived is None:
            derived = (await self._derived_for([(entry, revision)])).get(entry.id, [])
        result: dict[str, object] = {
            "id": str(entry.id), "project_id": str(entry.project_id) if entry.project_id else None,
            "kind": entry.kind, "status": entry.status,
            "revision": revision.revision, "revision_status": revision.status, "title": revision.title,
            "tags": revision.tags, "source": dict(revision.source), "created_at": revision.created_at.isoformat(),
            "author_id": str(revision.author_id),
            "promoted_from": str(entry.promoted_from) if entry.promoted_from else None,
            "relations": await self._relations(entry.id, revision, derived, queue_hidden=queue_hidden),
            "excerpt": revision.body[:360],
        }
        if include_findings:
            result["relation_findings"] = await declaration_findings(
                self.db, published(self.scope).where(~overridden(self.scope)), entry, revision,
                workspace=self.scope.workspace, project=self.scope.project,
                skill_directories=self.scope.skill_directories,
            )
        if entry.kind == "rule":
            result["rule_paths"] = revision.rule_paths
        if authored := revision.authored_at:
            normalized = authored.astimezone(UTC) if authored.tzinfo else authored.replace(tzinfo=UTC)
            result["authored_at"] = normalized.isoformat()
        if include_body and entry.kind == "learning":
            saved = await self.db.get(KnowledgeConfidenceSnapshot, (entry.id, revision.revision))
            if saved:
                result["confidence_snapshot"] = {
                    "confidence": saved.score / 100, "revision": saved.revision,
                    "updated_at": saved.updated_at.isoformat(),
                }
        return result | ({"body": revision.body} if include_body else {})
