"""用授权的固定 Git 提交刷新代码证据，发布指针与基线同事务生效。"""

import asyncio
import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select

from weora.modules.knowledge.code_preview import preview_delta, relation_delta
from weora.modules.knowledge.code_wiki_pages import wiki_page_id, wiki_pages
from weora.modules.knowledge.code_wiki_paths_v1 import wiki_locations, wiki_namespaces
from weora.modules.knowledge.domain import KnowledgeError, codebase_id
from weora.modules.knowledge.local_source import LocalCodeSnapshot
from weora.modules.knowledge.markdown_references import index_code_paths
from weora.modules.knowledge.models import (
    KnowledgeCodebase,
    KnowledgeCodebaseImport,
    KnowledgeCodebaseReview,
    KnowledgeEntry,
    KnowledgeEvent,
    KnowledgeRevision,
)
from weora.modules.knowledge.recall_gaps import add_gap_sources
from weora.modules.knowledge.review_source import RepoKey, registered_repo, review_repo
from weora.modules.knowledge.search import add_indexed_revisions
from weora.modules.knowledge.store import KnowledgeStore, published
from weora.modules.projects.domain import (
    FixedRepository,
    ProjectActor,
    ProjectError,
    RepositoryBlob,
    RepositoryIndexReader,
)
from weora.modules.projects.repository_sync import SyncTarget
from weora.modules.projects.service import ProjectService
from weora_code_graph.coverage import GAP_PAGE, GapFinding, detect_gaps, render_gaps
from weora_code_graph.design import (
    ARCHITECTURE_DOCUMENT,
    COMPONENT_PROFILE_SUFFIX,
    DESIGN_EVIDENCE_VERSION,
    DOMAIN_DOCUMENT,
    GRAPH_TOPIC_DOCUMENTS,
    REPOSITORY_NARRATIVE_HEADING,
    REPOSITORY_OVERVIEW_DOCUMENT,
    SCENARIOS_DOCUMENT,
    Component,
    GraphTopic,
    assign_code_domain,
    component_contexts,
    component_profile_manifest,
    components_for,
    compose_repository_overview,
    design_context,
    design_evidence,
    domain_context,
    graph_documents,
    read_code_domain,
    read_component_profile,
    repository_narrative_context,
    scenario_connections,
    valid_design_text,
    valid_repository_narrative,
    valid_scenario_text,
)
from weora_code_graph.extractor import (
    DEFAULT_INDEX_FILES,
    EXTRACTOR_VERSION,
    FileFacts,
    dependencies,
    extract,
    go_code_index,
    language_for,
    priority,
    relation_targets,
    resolved_calls,
    resolved_implementations,
    swift_code_index,
)
from weora_code_graph.javascript_index import JavaScriptIndex


@dataclass(frozen=True, slots=True)
class Document:
    title: str
    body: str
    source: dict[str, str]
    tags: list[str]
    relations: list[dict[str, str]]
    gap_sources: tuple[GapFinding, ...] = ()


class _CheckoutMismatch(Exception):
    """当前登记仓库和设备文件无法证明等价。"""


_CHECKOUT_CACHE_ERRORS = {
    "REPOSITORY_SYNC_CACHE_MISSING",
    "REPOSITORY_SYNC_DISABLED",
    "REPOSITORY_SYNC_PENDING",
    "REPOSITORY_SYNC_STALE",
}


def _design_basis(index: KnowledgeCodebase) -> str:
    # 同一个提交可能因采集上限或解析器升级而具有不同的证据，设计绑定实际清单。
    inputs = [
        DESIGN_EVIDENCE_VERSION, str(index.id), index.revision, index.extraction_version,
        index.max_files, index.candidates, index.manifest,
    ]
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _design_id(index: UUID, name: str) -> UUID:
    return uuid5(index, f"\0design:{name}")


def _documents(
    key: UUID,
    name: str,
    provenance: dict[str, str],
    manifest: dict[str, FileFacts],
    candidates: int,
) -> dict[UUID, Document]:
    paths = {path for path, file in manifest.items() if file["parser"] != "skipped"}
    go = go_code_index(manifest, paths)
    swift = swift_code_index(manifest, paths)
    javascript = JavaScriptIndex(manifest, paths)
    graph = {path: dependencies(path, manifest[path], paths, go=go, javascript=javascript) for path in sorted(paths)}
    overview = uuid5(key, "")
    links = {path: str(uuid5(key, path)) for path in graph}
    catalog = "\n".join(f"- {path}" for path in graph)
    stats = f"已采集 {len(graph)} / {candidates} 个候选源码文件。"
    artifacts = {
        overview: Document(
            f"{name} · 代码概览"[:160],
            f"{stats}\n\n## 文件\n{catalog}"[:64_000],
            provenance | {"ref": name, "path": ""},
            ["代码概览"],
            [{"kind": "CONTAINS", "target": target} for target in links.values()],
        )
    }
    connected: dict[str, list[str]] = {}
    implementation_sites: set[tuple[str, str]] = set()
    for path, targets in graph.items():
        file = manifest[path]
        lines = [f"- {symbol['kind']} `{symbol['name']}`（第 {symbol['line']} 行）" for symbol in file["symbols"]]
        implementations = resolved_implementations(path, manifest, paths, javascript=javascript)
        content = [f"源码 {path} · {file['language']} · {file['parser']}", "", "## 声明", *lines]
        if implementations:
            content.extend(["", "## 接口实现"])
            content.extend(
                f"- `{item['name']} implements {item['interface']}` → `{item['target']}`（第 {item['line']} 行）"
                for item in implementations
            )
        calls = resolved_calls(path, manifest, paths, go=go, swift=swift, javascript=javascript)
        if calls:
            content.extend(["", "## 调用引用"])
            for item in calls:
                confidence = f"，{item['confidence']}" if item.get("confidence") else ""
                content.append(f"- `{item['callee']}()` → `{item['target']}`（第 {item['line']} 行{confidence}）")
        content.extend(["", "## 仓库内依赖", *(f"- {item}" for item in targets)])
        # 类型与调用关系比文件依赖更具体，有限的检索关系优先展示它们。
        relations = relation_targets(targets, calls, implementations)
        connected[path] = [target for related in relations.values() for target in related]
        implementation_sites.update((item["target"], item["interface"]) for item in implementations)
        artifacts[UUID(links[path])] = Document(
            path[-160:],
            "\n".join(content)[:64_000],
            provenance | {"ref": path, "path": path, "blob": file["oid"]},
            [file["language"]],
            [{"kind": kind, "target": links[target]} for kind, sources in relations.items() for target in sources],
        )
    pages = wiki_pages(manifest, graph)
    findings = detect_gaps(manifest, connected, implementation_sites, javascript, candidates)
    summaries = [{key: value for key, value in item.items() if key != "sources"} for item in findings]
    artifacts[wiki_page_id(key, GAP_PAGE)] = Document(
        f"{name} · 知识缺口"[:160], render_gaps(findings),
        provenance | {
            "ref": GAP_PAGE, "path": "", "wiki_page": GAP_PAGE,
            "gap_findings": json.dumps(summaries, ensure_ascii=False),
            "gap_evidence_sha": hashlib.sha256(json.dumps(findings, sort_keys=True).encode()).hexdigest(),
        },
        ["知识缺口"], [], tuple(findings),
    )
    for filename, page in pages.items():
        if filename == "index.md":
            wiki_relations = [
                {"kind": "CONTAINS", "target": str(identity)}
                for identity in [
                    overview, *(wiki_page_id(key, page) for page in sorted({*pages, GAP_PAGE} - {filename})),
                ]
            ]
        else:
            wiki_relations = [{"kind": "REFERENCES", "target": links[path]} for path in page.files]
        artifacts[wiki_page_id(key, filename)] = Document(
            f"{name} · {page.title}"[:160], page.body[:64_000],
            provenance | {"ref": filename, "path": "", "wiki_page": filename},
            ["代码证据"], wiki_relations,
        )
    return artifacts


def _index_candidates(blobs: Sequence[RepositoryBlob]) -> list[RepositoryBlob]:
    return sorted((blob for blob in blobs if language_for(blob.path)), key=lambda blob: priority(blob.path))


def _design_document(
    row: KnowledgeCodebase, origin: dict[str, str], basis: str, name: str, text: str, component: Component | None,
    *, domain_profile: str | None = None, explicit_domain: bool = False, component_profile: str | None = None,
) -> Document:
    generated = bool(component or name in {ARCHITECTURE_DOCUMENT, DOMAIN_DOCUMENT, SCENARIOS_DOCUMENT})
    if component:
        description = "模块职责" if component_profile is not None else "组件设计"
        title = f"{origin['ref']} · {component.name} {description}"
    elif name == ARCHITECTURE_DOCUMENT:
        title = f"{origin['ref']} · 架构总览"
    elif name == DOMAIN_DOCUMENT:
        title = f"{origin['ref']} · 业务领域"
    elif name == SCENARIOS_DOCUMENT:
        title = f"{origin['ref']} · 核心业务场景"
    else:
        title = f"{origin['ref']} · {text.splitlines()[0].removeprefix('# ')}"
    provenance = origin | {
        "ref": name, "path": "", "design_asset": name,
        "design_basis": basis, "design_origin": "generated" if generated else "graph",
    }
    paths = component.paths[:24] if component else ()
    links = [{"kind": "REFERENCES", "target": str(uuid5(row.id, path))} for path in ("", *paths)]
    tags = ["代码设计", "组件" if component else "架构" if generated else "图谱"]
    if domain_profile is not None:
        # 同版元数据放在来源中供续做，条目正文继续参与平台的常规检索。
        provenance["domain_profile"] = domain_profile
        provenance["domain_assignment"] = "explicit" if explicit_domain else "inferred"
        tags = ["代码设计", "业务领域"]
    if component_profile is not None:
        provenance["component_profile"] = component_profile
        tags = ["代码设计", "模块职责"]
    return Document(title[:160], text, provenance, tags, links)


class CodebaseIndex:
    def __init__(self, store: KnowledgeStore, projects: ProjectService) -> None:
        self.db, self.projects, self.scope = store.db, projects, store.scope
        self.actor = ProjectActor(self.scope.user, self.scope.workspace)

    def _connection(self, repo: UUID) -> tuple[UUID, int]:
        version = dict(self.scope.code_connections).get(repo)
        if self.scope.project is None or version is None:
            raise KnowledgeError("KNOWLEDGE_REPOSITORY_NOT_FOUND", "请在活动项目中选择已登记的仓库。", 404)
        return self.scope.project, version

    def _require_wiki_access(self, key: UUID) -> None:
        if key in self.scope.hidden_wikis:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_NOT_FOUND", "代码 Wiki 不存在或未在当前目录启用。", 404)

    async def _baseline(self, repo: UUID, version: int) -> KnowledgeCodebase | None:
        return await self._by_id(codebase_id(repo, version))

    async def _by_id(self, key: UUID) -> KnowledgeCodebase | None:
        row: KnowledgeCodebase | None = await self.db.scalar(
            select(KnowledgeCodebase)
            .where(
                KnowledgeCodebase.id == key,
                KnowledgeCodebase.workspace_id == self.scope.workspace,
                KnowledgeCodebase.project_id == self.scope.project,
            )
            .execution_options(populate_existing=True)
        )
        return row

    async def preview_review(
        self, repo: UUID, version: int, base: str, head: str, paths: tuple[str, ...],
    ) -> dict[str, object]:
        """对比评审的固定提交。预览完全独立于已发布的索引和仓库同步状态。"""
        project, current = self._connection(repo)
        if version != current:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_CONNECTION_CHANGED", "仓库连接已变化。", 409)
        source_paths = tuple(path for path in dict.fromkeys(paths) if language_for(path))
        reader = self.projects.reader
        if not isinstance(reader, RepositoryIndexReader):
            raise KnowledgeError("KNOWLEDGE_REPOSITORY_INDEX_UNAVAILABLE", "当前仓库读取器不支持代码索引。", 409)
        revisions = {"base_commit": base, "head_commit": head}
        if base == head or not source_paths:
            return preview_delta({}, {}) | relation_delta({}, {}) | revisions
        baseline = await self._baseline(repo, version)
        try:
            async with AsyncExitStack() as snapshots:
                async with asyncio.timeout(50):
                    old, new = [
                        await snapshots.enter_async_context(self.projects.fixed_repository(
                            self.actor, project, (repo,), repo,
                            revision=revision, connection_version=version, fetch_missing_revision=True,
                        ))
                        for revision in (base, head)
                    ]
                    if (old.revision, new.revision) != (base, head):
                        raise ProjectError("REPOSITORY_REVISION_MISMATCH", "读取结果与评审固定提交不一致。")
                    previous = {blob.path: blob for blob in await reader.index_blobs(old.cache_id, base, source_paths)}
                    proposed = {blob.path: blob for blob in await reader.index_blobs(new.cache_id, head, source_paths)}
                    changed = sorted(
                        path for path in previous.keys() | proposed.keys() if previous.get(path) != proposed.get(path)
                    )
                    before, _ = await self._extract(reader, old, [previous[p] for p in changed if p in previous], {})
                    after, _ = await self._extract(reader, new, [proposed[p] for p in changed if p in proposed], {})
                relations = await self._preview_relations(reader, old, new, baseline, before, after)
        except TimeoutError:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_TIMEOUT", "代码节点预览超时。", 504) from None
        except ProjectError as exc:
            raise KnowledgeError(exc.code, str(exc), 409) from exc
        return preview_delta(before, after) | relations | revisions

    async def _preview_relations(
        self, reader: RepositoryIndexReader, old: FixedRepository, new: FixedRepository,
        baseline: KnowledgeCodebase | None, before: dict[str, FileFacts], after: dict[str, FileFacts],
    ) -> dict[str, object]:
        limit = baseline.max_files if baseline else DEFAULT_INDEX_FILES
        cached = baseline.manifest if baseline and baseline.extraction_version == EXTRACTOR_VERSION else {}
        try:
            async with asyncio.timeout(35):
                previous = _index_candidates(await reader.index_blobs(old.cache_id, old.revision))
                proposed = _index_candidates(await reader.index_blobs(new.cache_id, new.revision))
                # 用对象哈希复用未修改文件，并按索引的候选排序和上限解析两侧关系。
                old_graph, _ = await self._extract(reader, old, previous[:limit], cached | before)
                new_graph, _ = await self._extract(reader, new, proposed[:limit], cached | old_graph | after)
        except TimeoutError:
            return {"relation_warning": "KNOWLEDGE_CODEBASE_RELATIONS_TIMEOUT"}
        except ProjectError as exc:
            return {"relation_warning": exc.code}
        return relation_delta(old_graph, new_graph) | {
            "relation_coverage": {"limit": limit, "base_candidates": len(previous), "head_candidates": len(proposed)},
        }

    async def index(
        self,
        repo: UUID,
        *,
        incremental: bool,
        limit: int | None,
        operation_id: UUID | None = None,
        expected_connection_version: int | None = None,
        checkout_snapshot: LocalCodeSnapshot | None = None,
    ) -> dict[str, object]:
        project, version = self._connection(repo)
        if expected_connection_version is not None and version != expected_connection_version:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_CONNECTION_CHANGED", "计划后仓库连接已变化，请重新解析来源。", 409)
        key = codebase_id(repo, version)
        digest = hashlib.sha256(json.dumps([incremental, limit]).encode()).hexdigest()
        if operation_id and (receipt := await self._import_receipt(key, operation_id, digest)) is not None:
            return receipt
        reader = self.projects.reader
        if not isinstance(reader, RepositoryIndexReader):
            if checkout_snapshot is not None:
                raise _CheckoutMismatch
            raise KnowledgeError("KNOWLEDGE_REPOSITORY_INDEX_UNAVAILABLE", "当前仓库读取器不支持代码索引。", 409)
        base = await self._baseline(repo, version)
        if checkout_snapshot is not None and base and base.max_files != limit:
            raise _CheckoutMismatch
        if limit is None:
            if base is None:
                raise KnowledgeError("KNOWLEDGE_CODEBASE_NOT_INDEXED", "仓库当前连接还没有索引。", 409)
            limit = base.max_files
        generation = base.generation if base else 0
        cache = base.manifest if base and incremental and base.extraction_version == EXTRACTOR_VERSION else {}
        try:
            async with (
                asyncio.timeout(120),
                self.projects.fixed_repository(
                    self.actor,
                    project,
                    (repo,),
                    repo,
                    connection_version=version,
                    local_only=checkout_snapshot is not None,
                ) as fixed,
            ):
                if checkout_snapshot is not None:
                    assert checkout_snapshot.checkout is not None
                    if fixed.revision != checkout_snapshot.checkout.commit:
                        raise _CheckoutMismatch
                if base and incremental and self._same_snapshot(base, fixed, limit):
                    if checkout_snapshot is not None and not checkout_snapshot.matches_git_blobs(
                        base.candidates, {path: facts["oid"] for path, facts in cache.items()},
                    ):
                        raise _CheckoutMismatch
                    manifest, reused, count = cache, len(cache), base.candidates
                else:
                    candidates = _index_candidates(await reader.index_blobs(fixed.cache_id, fixed.revision))
                    selected = candidates[:limit]
                    if checkout_snapshot is not None and not checkout_snapshot.matches_git_blobs(
                        len(candidates), {item.path: item.oid for item in selected},
                    ):
                        raise _CheckoutMismatch
                    manifest, reused = await self._extract(reader, fixed, selected, cache)
                    count = len(candidates)
            # 同仓库发布串行化。耗时的读取先完成，连接确认和知识版本在同一事务中提交。
            await self.projects.lock_repository_connection(self.actor, project, fixed, self.db)
        except TimeoutError:
            if checkout_snapshot is not None:
                raise _CheckoutMismatch from None
            raise KnowledgeError("KNOWLEDGE_CODEBASE_TIMEOUT", "代码库提取超时。原索引可以继续使用。", 504) from None
        except ProjectError as error:
            if checkout_snapshot is not None and error.code in _CHECKOUT_CACHE_ERRORS:
                raise _CheckoutMismatch from error
            raise KnowledgeError(error.code, str(error), 409) from error
        if operation_id and (receipt := await self._import_receipt(key, operation_id, digest)) is not None:
            return receipt
        latest = await self._baseline(repo, version)
        concurrent_change = (latest.generation if latest else 0) != generation
        if (
            latest and (incremental or concurrent_change) and self._same_snapshot(latest, fixed, limit)
            and await self._wiki_ready(latest.id)
        ):
            result = self._status(latest) | {"reused": len(latest.manifest), "extracted": 0, "removed": 0}
            return self._finish_import(key, operation_id, digest, result)
        if concurrent_change:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_CHANGED", "仓库索引已由其他请求更新，可重新执行。", 409)
        removed = len(latest.manifest.keys() - manifest.keys()) if latest else 0
        now = datetime.now(UTC)
        index = latest or KnowledgeCodebase(
            id=key,
            workspace_id=self.scope.workspace,
            project_id=project,
            repository_id=repo,
            connection_version=version,
        )
        index.revision, index.generation, index.extraction_version = fixed.revision, generation + 1, EXTRACTOR_VERSION
        index.max_files, index.candidates, index.manifest = limit, count, manifest
        if not index.wiki_namespaces:
            index.wiki_namespaces = list(wiki_namespaces(fixed.git_url, fixed.provider, None))
        index.indexed_at, index.indexed_by = now, self.scope.user
        self.db.add(index)
        await self.db.flush()
        provenance = {
            "type": "repository",
            "repository_id": str(repo),
            "commit": fixed.revision,
            "connection_version": str(version),
        }
        await self._publish(index, _documents(key, fixed.name, provenance, manifest, count))
        result = self._status(index) | {"reused": reused, "extracted": len(manifest) - reused, "removed": removed}
        return self._finish_import(key, operation_id, digest, result)

    async def index_local(self, snapshot: LocalCodeSnapshot) -> dict[str, object]:
        project = self.scope.project
        if project is None:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_SCOPE", "本地目录导入需要选择项目。", 422)
        owner = self.scope.user
        key = uuid5(project, f"weora:local:{owner}:{snapshot.source_key}")
        hashes = snapshot.hashes()
        await self._lock_local_index(key)
        await self.projects.require_active_project(self.actor, project, self.db)
        baseline = await self._by_id(key)
        if baseline is None and not hashes:
            raise KnowledgeError("KNOWLEDGE_LOCAL_EMPTY", "本地目录没有可索引的源码文件。", 422)
        checkout, canonical = await self._bind_checkout(snapshot, project)
        if canonical is not None:
            assert checkout is not None
            # 相同旧快照只留历史证据。修改过的旧快照仍可检索，供团队追溯。
            if (
                baseline
                and baseline.candidates == snapshot.candidates
                and hashes == {path: facts["oid"] for path, facts in baseline.manifest.items()}
                and baseline.local_checkout != checkout
            ):
                baseline.local_checkout = checkout
                baseline.generation += 1
                baseline.indexed_at, baseline.indexed_by = datetime.now(UTC), owner
                await self._publish(baseline, {})
            return canonical | {"checkout": checkout, "codebase_id": checkout["canonical_codebase_id"]}
        revision = snapshot.revision(hashes, checkout)
        cache = (
            baseline.manifest
            if baseline and snapshot.incremental and baseline.extraction_version == EXTRACTOR_VERSION
            else {}
        )
        if (
            baseline
            and snapshot.incremental
            and baseline.revision == revision
            and baseline.local_checkout == checkout
            and (baseline.extraction_version == EXTRACTOR_VERSION)
            and await self._wiki_ready(key)
        ):
            return self._status(baseline) | {"reused": len(cache), "extracted": 0, "removed": 0}
        manifest: dict[str, FileFacts] = {}
        reused = 0
        for item in snapshot.files:
            oid = hashes[item.path]
            cached = cache.get(item.path)
            if cached and cached["oid"] == oid:
                manifest[item.path] = cached
                reused += 1
            else:
                manifest[item.path] = extract(item.path, oid, item.text)
        removed = len(baseline.manifest.keys() - manifest.keys()) if baseline else 0
        index = baseline or KnowledgeCodebase(
            id=key,
            workspace_id=self.scope.workspace,
            project_id=project,
            local_key=snapshot.source_key,
            local_owner_id=owner,
            generation=0,
        )
        index.local_name, index.local_checkout = snapshot.name, checkout
        if not index.wiki_namespaces:
            # 目录快照身份固定，改显示名称时保留已经共享的链接。
            index.wiki_namespaces = list(wiki_namespaces(None, None, snapshot.name))
        index.revision, index.extraction_version = revision, EXTRACTOR_VERSION
        index.generation += 1
        index.max_files, index.candidates, index.manifest = snapshot.max_files, snapshot.candidates, manifest
        index.indexed_at, index.indexed_by = datetime.now(UTC), owner
        self.db.add(index)
        await self.db.flush()
        provenance = {"type": "local_directory", "codebase_id": str(key), "snapshot": revision}
        if checkout is not None:
            provenance.update({f"checkout_{field}": value for field, value in checkout.items()})
        await self._publish(index, _documents(key, snapshot.name, provenance, manifest, snapshot.candidates))
        return self._status(index) | {"reused": reused, "extracted": len(manifest) - reused, "removed": removed}

    async def _wiki_ready(self, key: UUID) -> bool:
        """旧索引可继续复用已提取事实，刷新时补齐代码目录和缺口页。"""
        required = [wiki_page_id(key, name) for name in ("index.md", GAP_PAGE)]
        state = await self.db.scalars(select(KnowledgeEntry.status).where(
            KnowledgeEntry.id.in_(required), KnowledgeEntry.workspace_id == self.scope.workspace,
            KnowledgeEntry.published_revision.is_not(None),
        ))
        return state.all() == ["active"] * len(required)

    async def _lock_local_index(self, key: UUID) -> None:
        # 本地首次导入、重建与设计发布共用事务锁。
        if self.db.get_bind().dialect.name == "postgresql":
            lock = int.from_bytes(key.bytes[:8], "big", signed=True)
            await self.db.execute(select(func.pg_advisory_xact_lock(lock)))

    async def _design_row(self, identifier: UUID, *, local: bool, write: bool = False) -> KnowledgeCodebase:
        project = self.scope.project
        if project is None:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_SCOPE", "请选择知识库项目。", 422)
        if not local:
            _, version = self._connection(identifier)
            key = codebase_id(identifier, version)
            if write:
                await self.projects.lock_active_repository(self.actor, project, identifier, version, self.db)
            row = await self._by_id(key)
        else:
            row = await self._by_id(identifier)
            if row and row.repository_id is not None:
                _, version = self._connection(row.repository_id)
                if row.connection_version != version:
                    raise KnowledgeError("KNOWLEDGE_CODEBASE_NOT_FOUND", "登记仓库连接已变化。", 404)
                return await self._design_row(row.repository_id, local=False, write=write)
            if write:
                await self._lock_local_index(identifier)
                row = await self._by_id(identifier)
            if row and row.local_checkout and row.local_checkout.get("status") == "reused":
                checkout = self._checkout_view(row.local_checkout)
                if checkout.get("status") != "reused":
                    raise KnowledgeError("KNOWLEDGE_CODEBASE_NOT_FOUND", "登记仓库连接已变化。", 404)
                return await self._design_row(UUID(checkout["repository_id"]), local=False, write=write)
            if write and row and row.local_owner_id != self.scope.user:
                raise KnowledgeError("KNOWLEDGE_CODE_DESIGN_FORBIDDEN", "只有该快照的上传成员可发布其设计。", 403)
        if row is None:
            raise KnowledgeError("KNOWLEDGE_CODE_ENRICH_SOURCE", "该来源还没有代码索引。", 404)
        if not write:
            self._require_wiki_access(row.id)
        return row

    async def _design_overview(self, row: KnowledgeCodebase) -> KnowledgeRevision:
        revision = await self.db.scalar(
            select(KnowledgeRevision).join(
                KnowledgeEntry,
                (KnowledgeEntry.id == KnowledgeRevision.entry_id)
                & (KnowledgeEntry.published_revision == KnowledgeRevision.revision),
            ).where(
                KnowledgeEntry.id == uuid5(row.id, ""), KnowledgeEntry.workspace_id == self.scope.workspace,
                KnowledgeEntry.codebase_id == row.id, KnowledgeEntry.status == "active",
            )
        )
        if revision is None:
            raise KnowledgeError("KNOWLEDGE_CODE_ENRICH_SOURCE", "当前索引缺少代码概览，请重新导入。", 409)
        if revision.source.get("snapshot" if row.local_key else "commit") != row.revision:
            raise KnowledgeError("KNOWLEDGE_CODE_DESIGN_STALE", "读取期间代码索引已变化，请重试。", 409)
        return revision

    async def _design_documents(
        self, row: KnowledgeCodebase, basis: str, names: Sequence[str],
    ) -> dict[str, tuple[UUID, KnowledgeRevision]]:
        if not names:
            return {}
        result = await self.db.execute(
            select(KnowledgeEntry.id, KnowledgeRevision).join(
                KnowledgeRevision,
                (KnowledgeRevision.entry_id == KnowledgeEntry.id)
                & (KnowledgeRevision.revision == KnowledgeEntry.published_revision),
            ).where(
                KnowledgeEntry.id.in_([_design_id(row.id, name) for name in names]),
                KnowledgeEntry.workspace_id == self.scope.workspace,
                KnowledgeEntry.codebase_id == row.id, KnowledgeEntry.status == "active",
                KnowledgeRevision.status == "published",
                KnowledgeRevision.source["design_basis"].as_string() == basis,
            )
        )
        return {revision.source["design_asset"]: (entry, revision) for entry, revision in result.tuples()}

    async def graph_document_catalog(self, topic: GraphTopic, identifier: UUID | None) -> dict[str, object]:
        """按主题定位有权读取的当前发布版本，不进行全文检索或加载正文。"""
        project = self.scope.project
        if project is None:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_SCOPE", "图谱文档需要选择知识库项目。", 422)
        filename = GRAPH_TOPIC_DOCUMENTS[topic]
        indexed = KnowledgeCodebase
        candidates = published(self.scope).join(indexed, indexed.id == KnowledgeEntry.codebase_id).where(
            KnowledgeEntry.project_id == project,
            indexed.workspace_id == self.scope.workspace, indexed.project_id == project,
            KnowledgeEntry.kind == "code", KnowledgeRevision.status == "published",
            KnowledgeRevision.source["design_asset"].as_string() == filename,
        )
        if identifier is not None:
            candidates = candidates.where(indexed.id == identifier)
        rows = (await self.db.execute(candidates.with_only_columns(
            indexed.id, indexed.repository_id, indexed.wiki_namespaces,
            KnowledgeEntry.id, KnowledgeRevision.title,
        ).order_by(KnowledgeRevision.title, KnowledgeEntry.id).limit(101))).tuples().all()
        items = []
        for codebase, repository, namespaces, entry, title in rows[:100]:
            paths = wiki_locations(tuple(namespaces), {"design_asset": filename})
            items.append({
                "entry_id": str(entry), "codebase_id": str(codebase),
                "repository_id": str(repository) if repository is not None else None,
                "title": title, "path": paths[0] if paths else None,
            })
        return {"topic": topic, "items": items, "truncated": len(rows) > 100}

    async def design_plan(self, identifier: UUID, *, local: bool, maximum: int) -> dict[str, object]:
        row = await self._design_row(identifier, local=local)
        overview = await self._design_overview(row)
        basis = _design_basis(row)
        evidence = design_evidence(overview.source["ref"], row.revision, row.manifest)
        if not evidence.components:
            raise KnowledgeError("KNOWLEDGE_CODE_ENRICH_EMPTY", "当前代码索引没有可分析的组件。", 422)
        components = list(evidence.components[:maximum or None])
        names = [
            *(f"{item.slug}.md" for item in components), *(item.profile_name for item in evidence.components),
            ARCHITECTURE_DOCUMENT, DOMAIN_DOCUMENT, SCENARIOS_DOCUMENT,
        ]
        existing = await self._design_documents(row, basis, names)
        missing_design = {item.slug for item in components if f"{item.slug}.md" not in existing}
        missing_profiles = {item.slug for item in evidence.components if item.profile_name not in existing}
        contexts = component_contexts(
            evidence, [item for item in evidence.components if item.slug in missing_design | missing_profiles],
        )
        profiles = {
            item.slug: profile for item in evidence.components
            if (saved := existing.get(item.profile_name))
            and (profile := read_component_profile(saved[1].source["component_profile"]))
        }
        architecture = design_context(evidence)
        narrative_context: dict[str, str] | None = None
        if not local:
            narrative_context = {} if overview.source.get("narrative_basis") == basis else (
                repository_narrative_context(evidence, architecture)
            )
        return {
            "project": evidence.project, "revision": row.revision, "basis": basis,
            "generation": row.generation, "connection_version": row.connection_version,
            "components": [
                {
                    "slug": item.slug, "title": item.name, "file_count": len(item.paths),
                    "context": contexts[item.slug] if item.slug in missing_design else {},
                }
                for item in components
            ],
            "profile_components": [
                {
                    "slug": item.slug, "title": item.name, "file_count": len(item.paths),
                    "context": contexts[item.slug] if item.slug in missing_profiles else {},
                }
                for item in evidence.components
            ],
            "component_manifest": component_profile_manifest(evidence, profiles),
            "architecture_context": architecture,
            "domain_context": domain_context(evidence, architecture) if DOMAIN_DOCUMENT not in existing else {},
            "repository_narrative_context": narrative_context,
            "interactions": sorted(scenario_connections(evidence, components)),
            # 续做只需可定位的正文摘要。完整文档通过常规知识条目接口读取。
            "documents": {
                name: {
                    "id": str(entry),
                    "body": revision.source.get(
                        "domain_profile", revision.source.get(
                            "component_profile", revision.body[:2600 if name == ARCHITECTURE_DOCUMENT else 280],
                        ),
                    ),
                }
                for name, (entry, revision) in existing.items()
            } | (
                {REPOSITORY_OVERVIEW_DOCUMENT: {"id": str(overview.entry_id), "body": overview.body[:2600]}}
                if narrative_context == {} else {}
            ),
        }

    async def publish_design(
        self, identifier: UUID, *, local: bool, basis: str, name: str | None, body: str | None,
        domain_override: str | None = None,
    ) -> dict[str, object]:
        row = await self._design_row(identifier, local=local, write=True)
        if basis != _design_basis(row):
            raise KnowledgeError("KNOWLEDGE_CODE_DESIGN_STALE", "生成期间代码索引已变化，请重新生成。", 409)
        overview = await self._design_overview(row)
        if name == REPOSITORY_OVERVIEW_DOCUMENT:
            if local or row.repository_id is None:
                raise KnowledgeError("KNOWLEDGE_CODE_DESIGN_ASSET", "架构导读仅用于登记仓库。", 422)
            if body is None or not valid_repository_narrative(body):
                raise KnowledgeError("KNOWLEDGE_CODE_REPOSITORY_NARRATIVE_BODY", "仓库架构导读格式或大小无效。", 422)
            return await self._publish_repository_narrative(row, overview, basis, body)
        origin = {key: value for key, value in overview.source.items() if key != "narrative_basis"}
        extracted = components_for(row.manifest)
        components = {asset: item for item in extracted for asset in (f"{item.slug}.md", item.profile_name)}
        domain = None
        module_profile = None
        if domain_override is not None:
            name = DOMAIN_DOCUMENT
        if name is None:
            evidence = design_evidence(origin["ref"], row.revision, row.manifest)
            if not evidence.components:
                raise KnowledgeError("KNOWLEDGE_CODE_ENRICH_EMPTY", "当前代码索引没有可分析的组件。", 422)
            generated = graph_documents(evidence)
        else:
            if name not in components and name not in {ARCHITECTURE_DOCUMENT, DOMAIN_DOCUMENT, SCENARIOS_DOCUMENT}:
                raise KnowledgeError("KNOWLEDGE_CODE_DESIGN_ASSET", "当前索引未计划生成该设计文档。", 422)
            existing = (await self._design_documents(row, basis, [name])).get(name)
            if name == DOMAIN_DOCUMENT:
                previous = existing[1].source["domain_profile"] if existing else ""
                domain = (
                    assign_code_domain(domain_override, read_code_domain(previous))
                    if domain_override is not None else read_code_domain(body or "")
                )
                if domain is None:
                    raise KnowledgeError("KNOWLEDGE_CODE_DOMAIN_BODY", "代码库业务领域格式或大小无效。", 422)
                # 客户端只提出领域内容，组件来自服务器当前索引。
                body = domain.serialize(item.name for item in extracted)
            elif name.endswith(COMPONENT_PROFILE_SUFFIX):
                module_profile = read_component_profile(body or "")
                if module_profile is None:
                    raise KnowledgeError("KNOWLEDGE_CODE_COMPONENT_PROFILE_BODY", "模块职责格式或大小无效。", 422)
                body = json.dumps(module_profile.fields(), ensure_ascii=False)
            elif body is None or not valid_design_text(body) or (
                name == SCENARIOS_DOCUMENT and not valid_scenario_text(body)
            ):
                raise KnowledgeError("KNOWLEDGE_CODE_DESIGN_BODY", "代码设计正文格式或大小无效。", 422)
            if name == SCENARIOS_DOCUMENT:
                await self._require_scenario_evidence(row, basis, origin["ref"])
            if existing:
                entry, saved = existing
                text = saved.source.get("domain_profile", saved.source.get("component_profile", saved.body))
                # 在同一个锁内保留已有简介和关键词。普通模型重试沿用当前已发布条目。
                if domain_override is None or (body == text and saved.source.get("domain_assignment") == "explicit"):
                    return {"basis": basis, "items": [{"id": str(entry), "name": name, "body": text}], "reused": True}
            generated = {name: body.strip()}

        documents = {}
        for filename, text in generated.items():
            component = components.get(filename)
            display = text
            if domain is not None:
                display = domain.markdown()
            if module_profile is not None and component is not None:
                display = module_profile.markdown(component.entrypoints)
            documents[_design_id(row.id, filename)] = _design_document(
                row, origin, basis, filename, display, component,
                domain_profile=text if domain is not None else None, explicit_domain=domain_override is not None,
                component_profile=text if module_profile is not None else None,
            )
        await self._publish(
            row, documents, replace_missing=False, recorded_at=datetime.now(UTC), action="code_design_published",
        )
        return {
            "basis": basis, "items": [
                {"id": str(_design_id(row.id, filename)), "name": filename, **({"body": text} if name else {})}
                for filename, text in generated.items()
            ],
        }

    async def _publish_repository_narrative(
        self, row: KnowledgeCodebase, overview: KnowledgeRevision, basis: str, narrative: str,
    ) -> dict[str, object]:
        reused = overview.source.get("narrative_basis") == basis
        merged = overview.body
        if not reused:
            merged = compose_repository_overview(merged, narrative)
            doc = Document(
                overview.title, merged, overview.source | {"narrative_basis": basis},
                overview.tags, overview.relations,
            )
            # 登记仓库写锁内扩展原概览，保留现有条目 ID、包含关系和历史发布版本。
            await self._publish(
                row, {overview.entry_id: doc}, replace_missing=False, recorded_at=datetime.now(UTC),
                action="code_design_published",
            )
        return {
            "basis": basis, "reused": reused,
            "items": [{"id": str(overview.entry_id), "name": REPOSITORY_OVERVIEW_DOCUMENT, "body": merged}],
        }

    async def _require_scenario_evidence(self, row: KnowledgeCodebase, basis: str, title: str) -> None:
        evidence = design_evidence(title, row.revision, row.manifest)
        names = [ARCHITECTURE_DOCUMENT, *(f"{item.slug}.md" for item in evidence.components)]
        current = await self._design_documents(row, basis, names)
        completed = [item for item in evidence.components if f"{item.slug}.md" in current]
        if ARCHITECTURE_DOCUMENT not in current or not scenario_connections(evidence, completed):
            raise KnowledgeError("KNOWLEDGE_CODE_DESIGN_SCENARIO_ORDER", "先发布架构总览及相关组件设计。", 409)

    async def _bind_checkout(
        self, snapshot: LocalCodeSnapshot, project: UUID,
    ) -> tuple[dict[str, str] | None, dict[str, object] | None]:
        checkout = snapshot.checkout
        if checkout is None:
            return None, None
        metadata = {
            "status": "unregistered",
            "commit": checkout.commit,
            "subdir": checkout.subdir,
            "tree_state": "modified" if checkout.dirty else "clean",
        }
        matches = await self._repository_choices(project)
        identity = registered_repo(checkout.origin)
        candidates = matches.get(identity, []) if identity is not None else []
        if len(candidates) != 1:
            if candidates:
                metadata["status"] = "ambiguous"
            return metadata, None
        repo = candidates[0]
        _, version = self._connection(repo)
        metadata.update(status="linked", repository_id=str(repo), connection_version=str(version))
        if not checkout.dirty and not checkout.subdir:
            try:
                canonical = await self.index(
                    repo, incremental=True, limit=snapshot.max_files,
                    expected_connection_version=version, checkout_snapshot=snapshot,
                )
            except _CheckoutMismatch:
                pass
            else:
                # 已通过登记连接的同步缓存校验，发布同一份正式索引。
                metadata.update(status="reused", canonical_codebase_id=str(codebase_id(repo, version)))
                return metadata, canonical
        try:
            await self.projects.lock_active_repository(self.actor, project, repo, version, self.db)
        except ProjectError as error:
            raise KnowledgeError(error.code, str(error), 409) from error
        return metadata, None

    async def _import_receipt(self, base: UUID, operation_id: UUID, digest: str) -> dict[str, object] | None:
        row = await self.db.get(KnowledgeCodebaseImport, (base, self.scope.user, operation_id))
        if row is None:
            return None
        if row.request_digest != digest:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_OPERATION_CONFLICT", "同一请求标识已用于其他索引参数。", 409)
        return row.result

    def _finish_import(
        self,
        base: UUID,
        operation_id: UUID | None,
        digest: str,
        result: dict[str, object],
    ) -> dict[str, object]:
        if operation_id:
            self.db.add(
                KnowledgeCodebaseImport(
                    codebase_id=base,
                    actor_id=self.scope.user,
                    operation_id=operation_id,
                    request_digest=digest,
                    result=result,
                )
            )
        return result

    async def repository_plan(self, sources: list[str]) -> dict[str, object]:
        """URL 只用于定位已登记且当前可读的连接，同时固定执行时应验证的连接版本。"""
        project = self.scope.project
        if project is None:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_SCOPE", "仓库导入需要选择项目。", 422)
        matches = await self._repository_choices(project)
        versions = dict(self.scope.code_connections)
        rows: list[dict[str, object]] = []
        for source in sources:
            try:
                identifier = UUID(source)
            except ValueError:
                identity = registered_repo(source)
                candidates = matches.get(identity, []) if identity is not None else []
            else:
                candidates = [identifier] if identifier in versions else []
            if len(candidates) == 1:
                repo = candidates[0]
                rows.append(
                    {
                        "source": source,
                        "status": "ready",
                        "repository_id": str(repo),
                        "connection_version": versions[repo],
                    }
                )
            else:
                code = "REPOSITORY_AMBIGUOUS" if candidates else "REPOSITORY_NOT_REGISTERED"
                rows.append({"source": source, "status": "failed", "code": code})
        return {"items": rows}

    async def _repository_choices(self, project: UUID) -> dict[RepoKey, list[UUID]]:
        addresses = await self.projects.active_repository_addresses(self.actor, project, self.db)
        versions = dict(self.scope.code_connections)
        matches: dict[RepoKey, list[UUID]] = defaultdict(list)
        for repo, address in addresses.items():
            if repo in versions and (key := registered_repo(address)) is not None:
                matches[key].append(repo)
        return matches

    async def review_plan(self, urls: list[str]) -> dict[str, object]:
        """按登记仓库规划刷新。只刷新已经有索引的活动连接。"""
        if self.scope.project is None:
            return {"targets": [], "skipped": [{"url": url, "code": "PROJECT_REQUIRED"} for url in urls]}
        addresses = await self.projects.active_repository_addresses(self.actor, self.scope.project, self.db)
        versions = dict(self.scope.code_connections)
        repos: dict[RepoKey, list[UUID]] = defaultdict(list)
        indexed: set[UUID] = set()
        for repo, address in addresses.items():
            key = registered_repo(address)
            version = versions.get(repo)
            if key is None or version is None:
                continue
            repos[key].append(repo)
        grouping: dict[UUID, list[str]] = defaultdict(list)
        skipped: list[dict[str, str]] = []
        checked: set[UUID] = set()
        for url in dict.fromkeys(urls):
            identity = review_repo(url)
            candidates = repos.get(identity, []) if identity is not None else []
            for candidate in candidates:
                if candidate in checked:
                    continue
                checked.add(candidate)
                if await self._baseline(candidate, versions[candidate]) is not None:
                    indexed.add(candidate)
            targets = [repo for repo in candidates if repo in indexed]
            if len(targets) == 1:
                grouping[targets[0]].append(url)
            else:
                reason = (
                    "INVALID_REVIEW_SOURCE"
                    if identity is None
                    else "REPOSITORY_UNREGISTERED"
                    if not candidates
                    else "INDEX_MISSING"
                    if not targets
                    else "AMBIGUOUS_REPOSITORY"
                )
                skipped.append({"url": url, "code": reason})
        return {
            "targets": [{"repository_id": str(repo), "urls": sources} for repo, sources in grouping.items()],
            "skipped": skipped,
        }

    async def refresh_reviews(self, repo: UUID, run: UUID, urls: list[str]) -> dict[str, object]:
        _, version = self._connection(repo)
        sources = list(dict.fromkeys(urls))
        plan = await self.review_plan(sources)
        if plan["targets"] != [{"repository_id": str(repo), "urls": sources}]:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_REVIEW_MISMATCH", "评审链接与已索引的登记仓库不匹配。", 409)
        base = await self._baseline(repo, version)
        if base is None:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_NOT_INDEXED", "仓库当前连接还没有索引。", 409)
        base_id, old_generation = base.id, base.generation
        recorded = await self.db.scalars(
            select(KnowledgeCodebaseReview).where(
                KnowledgeCodebaseReview.codebase_id == base_id,
                KnowledgeCodebaseReview.url.in_(sources),
                KnowledgeCodebaseReview.run_id == run,
                KnowledgeCodebaseReview.indexed_by == self.scope.user,
            )
        )
        if len(recorded.all()) == len(sources):
            return {"repository_id": str(repo), "status": "replayed", "urls": sources}
        shared = self.projects.repository_sync
        if shared is not None and not await shared.run_one(
            SyncTarget(self.scope.workspace, repo, version),
            budget_seconds=45,
        ):
            sync = await shared.status(self.scope.workspace, repo, version)
            reason = "REPOSITORY_SYNC_PENDING" if sync["status"] == "NOT_SCHEDULED" else "REPOSITORY_SYNC_RUNNING"
            raise KnowledgeError(reason, "当前无法立即刷新登记仓库的同步提交。", 409)
        # Index 用现在已发布的固定提交，并在同一连接锁下写入证据和触发来源。
        indexed = await self.index(repo, incremental=True, limit=None)
        await self._record_reviews(base_id, run, str(indexed["revision"]), sources)
        status = "unchanged" if indexed["generation"] == old_generation else "updated"
        return indexed | {"status": status, "urls": sources}

    async def _record_reviews(self, index: UUID, run: UUID, revision: str, urls: list[str]) -> None:
        now = datetime.now(UTC)
        for url in urls:
            row = await self.db.get(KnowledgeCodebaseReview, (index, url, run, self.scope.user))
            if row is None:
                self.db.add(
                    KnowledgeCodebaseReview(
                        codebase_id=index,
                        url=url,
                        run_id=run,
                        indexed_by=self.scope.user,
                        revision=revision,
                        indexed_at=now,
                    )
                )

    @staticmethod
    def _same_snapshot(base: KnowledgeCodebase, fixed: FixedRepository, limit: int) -> bool:
        return (
            base.revision == fixed.revision and base.max_files == limit and base.extraction_version == EXTRACTOR_VERSION
        )

    @staticmethod
    async def _extract(
        reader: RepositoryIndexReader,
        fixed: FixedRepository,
        blobs: Sequence[RepositoryBlob],
        cache: dict[str, FileFacts],
    ) -> tuple[dict[str, FileFacts], int]:
        results = {
            blob.path: cached for blob in blobs if (cached := cache.get(blob.path)) and cached["oid"] == blob.oid
        }
        reused = len(results)
        gate = asyncio.Semaphore(6)

        async def read(blob: RepositoryBlob) -> None:
            async with gate:
                text = await reader.index_text(fixed.cache_id, blob)
                results[blob.path] = extract(blob.path, blob.oid, text)

        # 失败时等待在途读取退出后才释放临时裸库，避免另一个读进程仍在使用它。
        jobs = [asyncio.create_task(read(blob)) for blob in blobs if blob.path not in results]
        try:
            await asyncio.gather(*jobs)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
        return results, reused

    async def _publish(
        self, index: KnowledgeCodebase, documents: dict[UUID, Document], *,
        replace_missing: bool = True, recorded_at: datetime | None = None, action: str = "code_indexed",
    ) -> None:
        stamp = recorded_at or index.indexed_at
        query = (
            select(KnowledgeEntry, KnowledgeRevision)
            .join(
                KnowledgeRevision,
                (KnowledgeRevision.entry_id == KnowledgeEntry.id)
                & (KnowledgeRevision.revision == KnowledgeEntry.published_revision),
            )
            .where(KnowledgeEntry.workspace_id == self.scope.workspace, KnowledgeEntry.codebase_id == index.id)
        )
        if not replace_missing:
            query = query.where(KnowledgeEntry.id.in_(documents))
        rows = await self.db.execute(query)
        old = {entry.id: (entry, rev) for entry, rev in rows.tuples()}
        revisions: list[KnowledgeRevision] = []
        events: list[KnowledgeEvent] = []
        # 同一份证据重新索引时保留已发布导读。证据有变化则只发布新的提取概览。
        overview_id = uuid5(index.id, "")
        prior_overview = old.get(overview_id)
        matching_basis = None
        if replace_missing and prior_overview and (doc := documents.get(overview_id)):
            prior = prior_overview[1]
            if prior_basis := prior.source.get("narrative_basis"):
                matching_basis = _design_basis(index)
                prefix = f"{doc.body.rstrip()}\n\n{REPOSITORY_NARRATIVE_HEADING}\n\n"
                if prior_basis == matching_basis and prior.body.startswith(prefix):
                    documents[overview_id] = replace(
                        doc, body=prior.body, source=doc.source | {"narrative_basis": prior_basis},
                    )
        for identifier, doc in documents.items():
            previous = old.pop(identifier, None)
            if previous:
                entry, prior = previous
                # 文件或关系未变化时保留原始发布证据，当前 checkout 新鲜度单独计算。
                revision_field = "snapshot" if index.local_key is not None else "commit"
                new_anchors = {key: value for key, value in doc.source.items() if key != revision_field}
                if (
                    entry.status == "active"
                    and prior.body == doc.body
                    and prior.title == doc.title
                    and prior.tags == doc.tags
                    and prior.relations == doc.relations
                    and new_anchors == {key: value for key, value in prior.source.items() if key != revision_field}
                    and (doc.source["path"] or prior.source.get(revision_field) == index.revision)
                ):
                    continue
                revision = prior.revision + 1
                entry.status, entry.published_revision = "active", revision
            else:
                revision = 1
                self.db.add(
                    KnowledgeEntry(
                        id=identifier,
                        workspace_id=self.scope.workspace,
                        project_id=index.project_id,
                        kind="code",
                        status="active",
                        created_by=self.scope.user,
                        created_at=stamp,
                        published_revision=revision,
                        codebase_id=index.id,
                    )
                )
            payload = json.dumps([doc.body, doc.source, doc.relations], sort_keys=True).encode()
            digest = hashlib.sha256(payload).hexdigest()
            revisions.append(
                KnowledgeRevision(
                    entry_id=identifier,
                    revision=revision,
                    workspace_id=self.scope.workspace,
                    author_id=self.scope.user,
                    operation_id=uuid4(),
                    request_digest=digest,
                    status="published",
                    title=doc.title,
                    body=doc.body,
                    tags=doc.tags,
                    source=doc.source,
                    relations=doc.relations,
                    created_at=stamp,
                )
            )
            events.append(self._event(identifier, action, revision, index, stamp))
        has_design = any("design_basis" in rev.source for _, rev in old.values())
        basis = (matching_basis or _design_basis(index)) if documents and has_design else ""
        for entry, old_revision in old.values():
            design = old_revision.source.get("design_basis")
            if entry.status == "active" and (not design or basis != design):
                entry.status = "archived"
                kind = "code_design_archived" if design else "code_removed"
                events.append(self._event(entry.id, kind, old_revision.revision, index, stamp))
        await self.db.flush()
        await add_indexed_revisions(self.db, revisions)
        for version in revisions:
            index_code_paths(self.db, version, wiki_locations(tuple(index.wiki_namespaces), version.source))
            if findings := documents[version.entry_id].gap_sources:
                await add_gap_sources(self.db, version, findings)
        self.db.add_all(events)

    def _event(
        self, entry: UUID, action: str, version: int, index: KnowledgeCodebase, stamp: datetime,
    ) -> KnowledgeEvent:
        return KnowledgeEvent(
            id=uuid4(),
            entry_id=entry,
            actor_id=self.scope.user,
            action=action,
            revision=version,
            reason=index.revision,
            occurred_at=stamp,
        )

    def _checkout_view(self, checkout: dict[str, str]) -> dict[str, str]:
        if "repository_id" in checkout and not any(
            checkout["repository_id"] == str(repo) and checkout.get("connection_version") == str(version)
            for repo, version in self.scope.code_connections
        ):
            # 历史路线仍保留在发布证据，当前响应只显示调用者仍有权读取的连接。
            return {
                field: value for field, value in (checkout | {"status": "connection_changed"}).items()
                if field not in {"repository_id", "connection_version", "canonical_codebase_id"}
            }
        return checkout

    def _status(self, row: KnowledgeCodebase) -> dict[str, object]:
        skipped = sum(file["parser"] == "skipped" for file in row.manifest.values())
        source: dict[str, object]
        if row.local_key is None:
            source = {"repository_id": str(row.repository_id), "connection_version": row.connection_version}
        else:
            source = {
                "codebase_id": str(row.id),
                "source": "local_directory",
                "name": row.local_name,
                "freshness": "unknown",
            }
            if row.local_checkout is not None:
                source["checkout"] = self._checkout_view(row.local_checkout)
        if row.wiki_namespaces:
            source["wiki_root"] = f"teamwiki/evidence/code/{row.wiki_namespaces[0]}"
        return {
            **source,
            "revision": row.revision,
            "generation": row.generation,
            "indexed": len(row.manifest) - skipped,
            "skipped": skipped,
            "candidates": row.candidates,
            "truncated": row.candidates > row.max_files,
            "indexed_at": row.indexed_at.isoformat(),
        }

    async def status(self, repo: UUID, *, lint: bool = False, include_reviews: bool = True) -> dict[str, object]:
        _, version = self._connection(repo)
        self._require_wiki_access(codebase_id(repo, version))
        row = await self._baseline(repo, version)
        if row is None:
            empty: dict[str, object] = {
                "repository_id": str(repo),
                "connection_version": version,
                "freshness": "unknown",
                "indexed": 0,
            }
            return empty | {"problems": ["INDEX_MISSING"]} if lint else empty
        synced = (
            await self.projects.repository_sync.status(
                self.scope.workspace,
                repo,
                version,
            )
            if self.projects.repository_sync is not None
            else {}
        )
        latest = synced.get("revision") if synced.get("status") == "READY" else None
        freshness = "unknown" if latest is None else "current" if latest == row.revision else "stale"
        result = self._status(row) | {"freshness": freshness, "source_revision": latest}
        if not include_reviews:
            return result | {"problems": await self._lint(row)} if lint else result
        # 对外展示每个链接的最近一次采集，历史批次留作审计和恢复去重。
        ranked = (
            select(
                KnowledgeCodebaseReview.url,
                KnowledgeCodebaseReview.revision,
                KnowledgeCodebaseReview.indexed_at,
                func.row_number()
                .over(
                    partition_by=KnowledgeCodebaseReview.url,
                    order_by=(KnowledgeCodebaseReview.indexed_at.desc(), KnowledgeCodebaseReview.run_id),
                )
                .label("position"),
            )
            .where(KnowledgeCodebaseReview.codebase_id == row.id)
            .subquery()
        )
        recent = await self.db.execute(
            select(
                ranked.c.url,
                ranked.c.revision,
                ranked.c.indexed_at,
            )
            .where(ranked.c.position == 1)
            .order_by(ranked.c.indexed_at.desc(), ranked.c.url)
            .limit(25)
        )
        result["review_sources"] = [
            {"url": url, "revision": revision, "indexed_at": indexed_at.isoformat()}
            for url, revision, indexed_at in recent.tuples()
        ]
        return result | {"problems": await self._lint(row)} if lint else result

    async def _lint(self, row: KnowledgeCodebase) -> list[str]:
        paths = {path for path, file in row.manifest.items() if file["parser"] != "skipped"}
        problems: list[str] = []
        if row.candidates > row.max_files:
            problems.append("FILE_LIMIT")
        if len(paths) < len(row.manifest):
            problems.append("UNREADABLE_FILES")
        published = await self.db.execute(
            select(
                KnowledgeEntry.id, KnowledgeRevision.relations,
                KnowledgeRevision.source["design_asset"].as_string(), KnowledgeRevision.source["wiki_page"].as_string(),
            )
            .join(
                KnowledgeRevision,
                (KnowledgeRevision.entry_id == KnowledgeEntry.id)
                & (KnowledgeRevision.revision == KnowledgeEntry.published_revision),
            )
            .where(
                KnowledgeEntry.codebase_id == row.id,
                KnowledgeEntry.status == "active",
                KnowledgeRevision.status == "published",
            )
        )
        rows = published.tuples().all()
        published_edges = {str(entry): relations for entry, relations, _, _ in rows}
        overview = str(uuid5(row.id, ""))
        if overview not in published_edges:
            problems.append("OVERVIEW_MISSING")
        expected = {str(uuid5(row.id, path)) for path in paths}
        primary = {str(entry) for entry, _, design, wiki in rows if design is None and wiki is None}
        if primary - {overview} != expected:
            problems.append("MANIFEST_MISMATCH")
        if any(edge.get("target") not in published_edges for edges in published_edges.values() for edge in edges):
            problems.append("DANGLING_RELATIONS")
        return problems

    async def local_status(self, key: UUID, *, lint: bool = False) -> dict[str, object]:
        self._require_wiki_access(key)
        row = await self._by_id(key)
        if row is None:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_NOT_FOUND", "本地代码快照不存在或无权访问。", 404)
        if row.repository_id is not None:
            _, current_version = self._connection(row.repository_id)
            if row.connection_version != current_version:
                raise KnowledgeError("KNOWLEDGE_CODEBASE_NOT_FOUND", "仓库连接已变化。", 404)
            target = await self.status(row.repository_id, lint=lint, include_reviews=False)
            return target | {"codebase_id": str(key)}
        result = self._status(row)
        if row.local_checkout and row.local_checkout["status"] == "reused":
            checkout = self._checkout_view(row.local_checkout)
            if checkout["status"] == "reused":
                target = await self.status(UUID(checkout["repository_id"]), lint=lint, include_reviews=False)
                return target | {"codebase_id": str(key), "checkout": checkout}
            return result | {"problems": ["REPOSITORY_CONNECTION_CHANGED"]} if lint else result
        return result | {"problems": await self._lint(row)} if lint else result

    async def list_status(self, *, lint: bool = False, include_reviews: bool = True) -> dict[str, object]:
        if self.scope.project is None:
            raise KnowledgeError("KNOWLEDGE_CODEBASE_SCOPE", "查看仓库索引需要指定项目。", 422)
        locals_in_project = await self.db.scalars(
            select(KnowledgeCodebase)
            .where(
                KnowledgeCodebase.project_id == self.scope.project,
                KnowledgeCodebase.workspace_id == self.scope.workspace,
                KnowledgeCodebase.local_key.is_not(None),
            )
            .order_by(KnowledgeCodebase.indexed_at.desc(), KnowledgeCodebase.id)
        )
        items = [
            await self.status(repo, lint=lint, include_reviews=include_reviews)
            for repo, version in self.scope.code_connections
            if codebase_id(repo, version) not in self.scope.hidden_wikis
        ]
        for row in locals_in_project:
            if row.id in self.scope.hidden_wikis or (row.local_checkout and row.local_checkout["status"] == "reused"):
                continue
            result = self._status(row)
            items.append(result | {"problems": await self._lint(row)} if lint else result)
        return {"items": items}

    async def report_sources(self) -> object:
        """报告只读已持久化的索引与同步状态，无需查询远端。"""
        if self.scope.project is None:
            return []
        report = await self.list_status(include_reviews=False)
        project = await self.projects.detail(self.actor, self.scope.project)
        repos = project.get("repositories")
        names = {row["id"]: row["name"] for row in repos if isinstance(row, dict)} if isinstance(repos, list) else {}
        if isinstance(rows := report["items"], list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if "repository_id" in row:
                    row["id"] = row["repository_id"]
                    row["name"] = names.get(row["repository_id"], "登记仓库")
                    row["source"] = "repository"
                else:
                    row["id"] = row["codebase_id"]
        return report["items"]

    async def annotate(self, response: dict[str, object]) -> dict[str, object]:
        """只在最终命中读取新鲜度，搜索和打开均无需访问远端。"""
        rows = response.get("items")
        groups: dict[UUID, list[dict[str, object]]] = defaultdict(list)
        for row in rows if isinstance(rows, list) else [response]:
            if not isinstance(row, dict) or not isinstance(source := row.get("source"), dict):
                continue
            if source.get("type") == "local_directory":
                source["freshness"] = "unknown"  # 平台只持有已发布快照，设备改动要通过再次导入确认。
                checkout = {
                    key.removeprefix("checkout_"): source.pop(key)
                    for key in list(source) if key.startswith("checkout_")
                }
                if checkout:
                    source["checkout"] = self._checkout_view(checkout)
            if source.get("type") != "repository" or "repository_id" not in source:
                continue
            source["freshness"] = "unknown"
            with suppress(ValueError):
                groups[UUID(str(source["repository_id"]))].append(source)
        reader = self.projects.reader
        if not groups or not isinstance(reader, RepositoryIndexReader) or self.projects.repository_sync is None:
            return response
        # 确定性知识仍可检索，本地同步故障或超时只降级新鲜度。
        with suppress(TimeoutError):
            async with asyncio.timeout(4):
                for repo, sources in groups.items():
                    with suppress(ProjectError, KnowledgeError):
                        await self._annotate_repository(reader, repo, sources)
        return response

    async def _annotate_repository(
        self,
        reader: RepositoryIndexReader,
        repo: UUID,
        sources: list[dict[str, object]],
    ) -> None:
        project, version = self._connection(repo)
        paths = tuple(dict.fromkeys(str(source["path"]) for source in sources if source.get("path")))
        async with self.projects.fixed_repository(
            self.actor,
            project,
            (repo,),
            repo,
            connection_version=version,
            local_only=True,
        ) as fixed:
            blobs = await reader.index_blobs(fixed.cache_id, fixed.revision, paths) if paths else []
        current = {blob.path: blob.oid for blob in blobs}
        for source in sources:
            if not source.get("path"):
                source["freshness"] = "current" if fixed.revision == source.get("commit") else "stale"
                continue
            oid = current.get(str(source["path"]))
            source["freshness"] = "missing" if oid is None else "current" if oid == source.get("blob") else "stale"
