"""在离线目录和平台项目中运行同一个代码设计生成流程。"""

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal, cast

from weora_code_graph.design import (
    ARCHITECTURE_DOCUMENT as ARCHITECTURE,
)
from weora_code_graph.design import (
    COMPONENT_PROFILE_MANIFEST,
    COMPONENT_PROFILE_SUFFIX,
    ComponentProfile,
    DesignEvidence,
    assign_code_domain,
    component_contexts,
    component_profile_manifest,
    compose_repository_overview,
    context_lines,
    design_context,
    design_evidence,
    domain_context,
    graph_documents,
    read_code_domain,
    read_component_profile,
    repository_narrative_context,
    scenario_connections,
)
from weora_code_graph.design import (
    DOMAIN_DOCUMENT as DOMAIN,
)
from weora_code_graph.design import (
    REPOSITORY_OVERVIEW_DOCUMENT as REPOSITORY_OVERVIEW,
)
from weora_code_graph.design import (
    SCENARIOS_DOCUMENT as SCENARIOS,
)
from weora_code_graph.extractor import FileFacts

from weora_cli.collab_client import Client, CollabError, as_object, items
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_classifier import (
    code_design_driver,
    draft_code_design,
    draft_code_domain,
    draft_component_profile,
)
from weora_cli.knowledge_offline_code import (
    _artifact,
    _baseline,
    _load_verified,
    _object,
    _read,
    _valid_slug,
    _write,
    offline_code_writer,
    read_offline_domain,
    write_project_index,
    write_source_index,
)

_REVISION = re.compile(r"[0-9a-f]{64}")
type PublishDocument = Callable[[str, str], str]


@dataclass(frozen=True, slots=True)
class ComponentTask:
    slug: str
    title: str
    file_count: int
    context: dict[str, JsonValue]

    @property
    def name(self) -> str:
        return f"{self.slug}.md"

    @property
    def profile_name(self) -> str:
        return f"{self.slug}{COMPONENT_PROFILE_SUFFIX}"


@dataclass(slots=True)
class DesignPlan:
    project: str
    revision: str
    components: list[ComponentTask]
    architecture: dict[str, JsonValue]
    domain_context: dict[str, JsonValue]
    interactions: set[tuple[str, str]]
    documents: dict[str, str]
    profile_components: list[ComponentTask]
    overview_context: dict[str, JsonValue] | None

    def profiles(self) -> dict[str, ComponentProfile]:
        return {
            item.slug: profile for item in self.profile_components
            if (profile := read_component_profile(self.documents.get(item.profile_name, "")))
        }

    def report(self, *, preview: bool) -> dict[str, JsonValue]:
        pending: list[JsonValue] = [item.slug for item in self.components if item.name not in self.documents]
        scenario = "generated" if SCENARIOS in self.documents else "pending" if self.interactions else "skipped"
        catalog: list[JsonValue] = [
            {"slug": item.slug, "title": item.title, "file_count": item.file_count} for item in self.components
        ]
        profile = read_code_domain(self.documents.get(DOMAIN, ""))
        overview_status = "skipped"
        if self.overview_context is not None:
            overview_status = "generated" if REPOSITORY_OVERVIEW in self.documents else "pending"
        return {
            "project": self.project, "revision": self.revision, "preview": preview,
            "components": catalog, "missing_components": pending, "scenarios": scenario,
            "missing_profiles": [
                item.slug for item in self.profile_components if item.profile_name not in self.documents
            ],
            "missing_architecture": ARCHITECTURE not in self.documents,
            "domain_status": "generated" if profile else "pending",
            "domain": profile.domain if profile else None,
            "repository_overview": overview_status,
        }


@dataclass(frozen=True, slots=True)
class OfflineEvidence:
    root: Path
    evidence: DesignEvidence
    baseline: dict[str, JsonValue]

    @property
    def directory(self) -> str:
        return _artifact(self.evidence.project, self.evidence.revision)

    def read(self, name: str) -> str | None:
        if name == DOMAIN:
            return read_offline_domain(self.root, self.evidence.project, self.evidence.revision)
        path = name if name == REPOSITORY_OVERVIEW else f"docs/{name}"
        content = _read(self.root, f"{self.directory}/{path}")
        limit = 2_000_000 if name == REPOSITORY_OVERVIEW else 64_000
        if content is None or len(content) > limit:
            return None
        try:
            body = content.decode("utf-8").strip()
        except UnicodeError:
            return None
        if name.endswith(COMPONENT_PROFILE_SUFFIX) and read_component_profile(body) is None:
            return None
        return body if body and "\x00" not in body else None

    def write(self, name: str, body: str) -> str:
        if name == DOMAIN:
            profile = read_code_domain(body)
            assert profile is not None
            body = profile.serialize(item.name for item in self.evidence.components)
        if name == REPOSITORY_OVERVIEW:
            evidence = _load_verified(self.root, self.evidence.project, self.baseline, "overview.md")
            if evidence is None:
                raise CollabError("KNOWLEDGE_CODE_ENRICH_SOURCE", "代码库缺少可验证的源码概览，请重新导入。")
            body = compose_repository_overview(evidence.decode(), body)
        path = name if name in {DOMAIN, REPOSITORY_OVERVIEW} else f"docs/{name}"
        _write(self.root, f"{self.directory}/{path}", (body.rstrip() + "\n").encode())
        return body


def _load(root: Path, project: str) -> OfflineEvidence:
    baseline = _baseline(root, project)
    revision = baseline.get("revision")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise CollabError("KNOWLEDGE_CODE_ENRICH_SOURCE", "未找到代码库基线，请先导入代码证据。")
    facts = _object(_load_verified(root, project, baseline, "facts.json"))
    if not facts:
        raise CollabError("KNOWLEDGE_CODE_ENRICH_SOURCE", "代码库缺少可验证的文件事实，请重新导入。")
    # facts.json 来自已验证的同版本提取器产物。模型收到的是声明与关系证据。
    evidence = design_evidence(project, revision, cast(dict[str, FileFacts], facts))
    if not evidence.components:
        raise CollabError("KNOWLEDGE_CODE_ENRICH_EMPTY", "当前代码索引没有可分析的组件。")
    return OfflineEvidence(root, evidence, baseline)


def _offline_plan(wiki: OfflineEvidence, maximum: int) -> DesignPlan:
    evidence = wiki.evidence
    components = list(evidence.components[:maximum or None])
    names = [
        *(f"{item.slug}.md" for item in components), *(item.profile_name for item in evidence.components),
        ARCHITECTURE, DOMAIN, SCENARIOS,
    ]
    remote = wiki.baseline.get("verifiedRemote") is True
    if remote:
        names.append(REPOSITORY_OVERVIEW)
    docs = {name: content for name in names if (content := wiki.read(name))}
    pending = {item.slug for item in components if f"{item.slug}.md" not in docs}
    contexts = component_contexts(evidence, [
        item for item in evidence.components if item.slug in pending or item.profile_name not in docs
    ])
    tasks = {
        item.slug: ComponentTask(item.slug, item.name, len(item.paths), dict(contexts.get(item.slug, {})))
        for item in evidence.components
    }
    architecture = design_context(evidence)
    overview_context: dict[str, JsonValue] | None = None
    if remote:
        overview_context = {} if REPOSITORY_OVERVIEW in docs else dict(
            repository_narrative_context(evidence, architecture)
        )
    return DesignPlan(
        evidence.project, evidence.revision, [tasks[item.slug] for item in components], dict(architecture),
        dict(domain_context(evidence, architecture)) if DOMAIN not in docs else {},
        scenario_connections(evidence, components), docs, list(tasks.values()), overview_context,
    )


def _component_stage(
    plan: DesignPlan, driver: str, pending: list[ComponentTask], concurrency: int,
    publish: PublishDocument, failures: dict[str, str], *, shallow: bool = False,
) -> None:
    if not pending:
        return
    draft = partial(draft_component_profile, driver) if shallow else partial(draft_code_design, driver, "component")
    profiles = {} if shallow else plan.profiles()
    with ThreadPoolExecutor(max_workers=min(concurrency, len(pending))) as executor:
        jobs = {}
        for item in pending:
            context = item.context
            if profile := profiles.get(item.slug):
                context = context | {
                    "module_domain": profile.domain, "module_layer": profile.layer,
                    "module_summary": profile.summary, "responsibilities": list(profile.responsibilities),
                }
            asset = item.profile_name if shallow else item.name
            jobs[executor.submit(draft, context)] = asset
        for future in as_completed(jobs):
            name = jobs[future]
            try:
                body = future.result()
            except CollabError as error:
                failures[name] = error.code
            else:
                # 平台若已有另一个成员先提交的同版本文档，后续场景使用正式正文。
                plan.documents[name] = publish(name, body)


def _narrative(
    plan: DesignPlan, driver: str, stage: str, name: str, context: dict[str, JsonValue],
    publish: PublishDocument, failures: dict[str, str],
) -> None:
    try:
        body = draft_code_design(driver, stage, context)
    except CollabError as error:
        failures[name] = error.code
    else:
        plan.documents[name] = publish(name, body)


def _run(
    plan: DesignPlan, driver: str | None, concurrency: int,
    publish: PublishDocument, publish_graphs: Callable[[], None],
) -> dict[str, str]:
    pending = [item for item in plan.components if item.name not in plan.documents]
    needs_scenarios = SCENARIOS not in plan.documents and bool(plan.interactions)
    needs_architecture = ARCHITECTURE not in plan.documents
    needs_domain = DOMAIN not in plan.documents
    overview = plan.overview_context if REPOSITORY_OVERVIEW not in plan.documents else None
    missing_profiles = [item for item in plan.profile_components if item.profile_name not in plan.documents]
    # 先沉淀模块职责，再归纳仓库领域。已提交的同版画像可继续用于剩余阶段。
    needs_model = pending or missing_profiles or needs_architecture or needs_scenarios or needs_domain or overview
    tool = code_design_driver(driver) if needs_model else None
    failures: dict[str, str] = {}
    if tool:
        if overview is not None:
            _narrative(plan, tool, "repository_narrative", REPOSITORY_OVERVIEW, overview, publish, failures)
        _component_stage(plan, tool, missing_profiles, min(3, concurrency), publish, failures, shallow=True)
    profiles = plan.profiles()
    briefs = context_lines(
        (f"{item.title} · {profile.domain} · {profile.layer} · {profile.summary} · "
         + "、".join(profile.responsibilities)
         for item in plan.profile_components if (profile := profiles.get(item.slug))),
        6000,
    )
    if tool and needs_domain and (profiles or not plan.profile_components):
        try:
            domain = draft_code_domain(tool, plan.domain_context | {"module_profiles": briefs})
        except CollabError as error:
            # 领域是辅助元数据，生成失败仍可用索引编写设计，下次执行补齐画像。
            failures[DOMAIN] = error.code
        else:
            plan.documents[DOMAIN] = publish(DOMAIN, domain.serialize())
    context = plan.architecture | ({"module_profiles": briefs} if briefs else {})
    if saved := plan.documents.get(DOMAIN):
        repository_domain = read_code_domain(saved)
        if repository_domain is None:
            raise CollabError("KNOWLEDGE_CODE_ENRICH_RESPONSE", "发布返回的业务领域画像无效。")
        context = context | {
            "repository_domain": repository_domain.domain,
            "repository_description": repository_domain.description,
            "repository_keywords": list(repository_domain.keywords),
        }
    if tool:
        _component_stage(plan, tool, pending, concurrency, publish, failures)
        if needs_architecture:
            _narrative(plan, tool, "architecture", ARCHITECTURE, context, publish, failures)

    publish_graphs()
    finished = {item.slug: item for item in plan.components if item.name in plan.documents}
    architecture = plan.documents.get(ARCHITECTURE)
    if tool and needs_scenarios and architecture and any(
        left in finished and right in finished for left, right in plan.interactions
    ):
        designs = context_lines(
            (f"{item.title} · {plan.documents[item.name][:280]}" for item in finished.values()), 3000,
        )
        _narrative(
            plan, tool, "scenarios", SCENARIOS,
            context | {"architecture": architecture[:2600], "designed_components": designs}, publish, failures,
        )
    return failures


def _finish(plan: DesignPlan, failures: dict[str, str], location: dict[str, JsonValue]) -> dict[str, JsonValue]:
    report = plan.report(preview=False)
    complete = not (report["missing_components"] or report["missing_architecture"] or report["scenarios"] == "pending")
    result: dict[str, JsonValue] = report | location | {"complete": complete, "failures": dict(failures)}
    if not complete:
        raise CollabError("KNOWLEDGE_CODE_ENRICH_INCOMPLETE", "部分代码设计文档缺失，重新运行可续做。", result)
    return result


def _index_documents(
    wiki: OfflineEvidence, plan: DesignPlan, graph: dict[str, str], failures: dict[str, str],
) -> None:
    prefix = f"来源版本 `{wiki.evidence.revision}`"
    overview = REPOSITORY_OVERVIEW if REPOSITORY_OVERVIEW in plan.documents else "overview.md"
    links = [f"# {wiki.evidence.project} · 设计与图谱", "", prefix, "", f"[源码概览](../{overview})", ""]
    if profiles := plan.profiles():
        manifest = component_profile_manifest(wiki.evidence, profiles)
        _write(
            wiki.root, f"{wiki.directory}/{COMPONENT_PROFILE_MANIFEST}",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
        )
        links.append(f"- [模块职责与依赖](../{COMPONENT_PROFILE_MANIFEST})")
    if DOMAIN in plan.documents:
        links.append(f"- [仓库业务领域](../{DOMAIN})")
    if ARCHITECTURE in plan.documents:
        links.append(f"- [架构总览]({ARCHITECTURE})")
    # 汇总所有当前版本已有的组件文档，先前按更大范围生成的文档仍可浏览。
    links.extend(
        f"- [`{item.name.replace(chr(96), 'ˋ')}`]({item.slug}.md)"
        for item in wiki.evidence.components if wiki.read(f"{item.slug}.md")
    )
    links.extend(f"- [{body.splitlines()[0][2:]}]({name})" for name, body in sorted(graph.items()))
    if SCENARIOS in plan.documents:
        links.append(f"- [G5 核心业务场景]({SCENARIOS})")
    links.append("- [生成与证据报告](../_review/k4-quality-report.md)")
    wiki.write("README.md", "\n".join(links))
    report = [
        "# 代码知识检查", "", prefix, "",
        f"- 计划组件数 {len(plan.components)}", f"- 失败文档数 {len(failures)}",
        *(f"- `{name}` · `{code}`" for name, code in sorted(failures.items())),
        "- 图谱文件来自索引证据。架构和场景正文包含 Agent 的归纳，需要结合出处核对。", "",
    ]
    _write(wiki.root, f"{wiki.directory}/_review/k4-quality-report.md", "\n".join(report).encode())
    write_project_index(wiki.root, wiki.evidence.project, wiki.evidence.revision)


def enrich_offline(
    output: Path, project: str, *, driver: str | None, max_modules: int,
    concurrency: int, preview: bool,
) -> dict[str, JsonValue]:
    if not _valid_slug(project):
        raise CollabError("KNOWLEDGE_OFFLINE_PROJECT", "请使用导入输出的代码库名称。")
    try:
        if preview:
            wiki = _load(output.resolve() / "teamwiki", project)
            return _offline_plan(wiki, max_modules).report(preview=True)
        root = output.resolve() / "teamwiki"
        if not _baseline(root, project):
            raise CollabError("KNOWLEDGE_CODE_ENRICH_SOURCE", "未找到代码库基线，请先导入代码证据。")
        # 持导入共用的写锁后读取当前版本，生成期间项目导航不会指向其他索引。
        with offline_code_writer(output) as writer:
            try:
                result = enrich_offline_snapshot(
                    writer.root, project, driver=driver, max_modules=max_modules, concurrency=concurrency,
                )
            except CollabError as error:
                if error.code == "KNOWLEDGE_CODE_ENRICH_INCOMPLETE":
                    write_source_index(writer.root)
                raise
            write_source_index(writer.root)
            return result
    except OSError as exc:
        raise CollabError("KNOWLEDGE_CODE_ENRICH_IO", "无法读取或写入离线设计文档。") from exc


def enrich_offline_snapshot(
    root: Path, project: str, *, driver: str | None | Literal[False], max_modules: int = 0, concurrency: int = 5,
    expected_revision: JsonValue = None, domain_override: str | None = None,
) -> dict[str, JsonValue]:
    """调用方持有 teamwiki 写锁，整个导入和生成过程沿用同一份索引。"""
    wiki = _load(root, project)
    if expected_revision is not None and wiki.evidence.revision != expected_revision:
        raise CollabError("KNOWLEDGE_CODE_DESIGN_STALE", "导入后索引已变化，请对当前代码库重新生成。")
    plan = _offline_plan(wiki, max_modules)
    if domain_override is not None:
        previous = read_code_domain(plan.documents.get(DOMAIN, ""))
        profile = assign_code_domain(domain_override, previous)
        if profile is None:
            raise CollabError("KNOWLEDGE_CODE_DOMAIN_BODY", "代码库业务领域格式或大小无效。")
        if profile != previous:
            plan.documents[DOMAIN] = wiki.write(DOMAIN, profile.serialize())
        write_project_index(wiki.root, project, wiki.evidence.revision)
    if driver is False:
        return plan.report(preview=False) | {"complete": True, "skipped": "ai_enrichment"}
    graph = graph_documents(wiki.evidence)

    def publish_graphs() -> None:
        for name, body in graph.items():
            wiki.write(name, body)

    failures = _run(plan, driver, concurrency, wiki.write, publish_graphs)
    _index_documents(wiki, plan, graph, failures)
    return _finish(plan, failures, {"docs": str(wiki.root / wiki.directory / "docs")})


def _platform_plan(response: dict[str, JsonValue]) -> tuple[DesignPlan, dict[str, JsonValue]]:
    project, revision = response.get("project"), response.get("revision")
    interactions = response.get("interactions")
    if not isinstance(project, str) or not isinstance(revision, str) or not isinstance(interactions, list):
        raise CollabError("KNOWLEDGE_CODE_ENRICH_RESPONSE", "平台返回的代码设计计划无效。")
    components = _platform_components(response.get("components"))
    profile_components = _platform_components(response.get("profile_components", []))
    links: set[tuple[str, str]] = set()
    for edge in interactions:
        if not isinstance(edge, list) or len(edge) != 2 or not all(isinstance(item, str) for item in edge):
            raise CollabError("KNOWLEDGE_CODE_ENRICH_RESPONSE", "平台返回的组件关系无效。")
        links.add((str(edge[0]), str(edge[1])))
    bodies = {}
    entries: dict[str, JsonValue] = {}
    for name, value in as_object(response.get("documents")).items():
        row = as_object(value)
        body, entry = row.get("body"), row.get("id")
        if not isinstance(body, str) or not isinstance(entry, str):
            raise CollabError("KNOWLEDGE_CODE_ENRICH_RESPONSE", "平台返回的设计文档无效。")
        bodies[name], entries[name] = body, entry
    overview_context = response.get("repository_narrative_context")
    plan = DesignPlan(
        project, revision, components, as_object(response.get("architecture_context")),
        as_object(response.get("domain_context")), links, bodies, profile_components,
        as_object(overview_context) if overview_context is not None else None,
    )
    return plan, entries


def _platform_components(value: JsonValue) -> list[ComponentTask]:
    components = []
    for part in items({"items": value}):
        slug, title, count = part.get("slug"), part.get("title"), part.get("file_count")
        if not isinstance(slug, str) or not isinstance(title, str) or type(count) is not int:
            raise CollabError("KNOWLEDGE_CODE_ENRICH_RESPONSE", "平台返回的组件计划无效。")
        components.append(ComponentTask(slug, title, count, as_object(part.get("context"))))
    return components


def enrich_platform(
    client: Client, project: str, design_path: str, *, driver: str | None | Literal[False],
    max_modules: int, concurrency: int, preview: bool, expected_index: dict[str, JsonValue] | None = None,
    domain_override: str | None = None,
) -> dict[str, JsonValue]:
    response = call(client, project, design_path, max_modules=max_modules)
    if expected_index is not None and any(
        expected_index.get(field) is not None and response.get(field) != expected_index[field]
        for field in ("revision", "generation", "connection_version")
    ):
        raise CollabError("KNOWLEDGE_CODE_DESIGN_STALE", "导入后索引已变化，请对当前代码库重新生成。")
    basis = response.get("basis")
    if not isinstance(basis, str) or not _REVISION.fullmatch(basis):
        raise CollabError("KNOWLEDGE_CODE_ENRICH_RESPONSE", "平台返回的设计基线无效。")
    plan, entries = _platform_plan(response)
    if preview:
        return plan.report(preview=True) | {"basis": basis, "entries": entries}

    def publish(payload: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
        result = call(client, project, design_path, method="POST", payload=payload | {"basis": basis})
        if result.get("basis") != basis:
            raise CollabError("KNOWLEDGE_CODE_ENRICH_RESPONSE", "平台返回了不同的设计基线。")
        rows = items(result)
        for row in rows:
            name, entry = row.get("name"), row.get("id")
            if not isinstance(name, str) or not isinstance(entry, str):
                raise CollabError("KNOWLEDGE_CODE_ENRICH_RESPONSE", "平台返回的发布结果无效。")
            entries[name] = entry
        return rows

    def published_body(name: str, payload: dict[str, JsonValue]) -> str:
        rows = publish(payload)
        if len(rows) != 1 or rows[0].get("name") != name or not isinstance(text := rows[0].get("body"), str):
            raise CollabError("KNOWLEDGE_CODE_ENRICH_RESPONSE", "平台返回的设计正文无效。")
        return text

    def publish_document(name: str, body: str) -> str:
        return published_body(name, {"name": name, "body": body})

    def publish_graphs() -> None:
        publish({})

    if domain_override is not None:
        plan.documents[DOMAIN] = published_body(DOMAIN, {"domain_override": domain_override})
    location: dict[str, JsonValue] = {"basis": basis, "entries": entries}
    if driver is False:
        return plan.report(preview=False) | location | {"complete": True, "skipped": "ai_enrichment"}
    failures = _run(plan, driver, concurrency, publish_document, publish_graphs)
    return _finish(plan, failures, location)
