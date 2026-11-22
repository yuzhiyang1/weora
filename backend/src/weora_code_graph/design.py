"""从同一份索引事实归纳组件、调用证据和可重建的设计图谱文档。"""

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from itertools import islice
from typing import Literal, TypedDict

from weora_code_graph.extractor import (
    FileFacts,
    dependencies,
    go_code_index,
    relation_targets,
    resolved_calls,
    resolved_implementations,
    swift_code_index,
)
from weora_code_graph.javascript_index import JavaScriptIndex

DESIGN_EVIDENCE_VERSION = 1
ARCHITECTURE_DOCUMENT = "architecture.md"
DOMAIN_DOCUMENT = "_domains.json"
REPOSITORY_OVERVIEW_DOCUMENT = "_repository-overview.md"
REPOSITORY_NARRATIVE_HEADING = "## AI 架构导读"
COMPONENT_PROFILE_MANIFEST = "_component-profiles.json"
COMPONENT_PROFILE_SUFFIX = ".profile.json"
SCENARIOS_DOCUMENT = "graph-g5-scenarios.md"
GraphTopic = Literal["g1", "g2", "g5", "g6"]
GRAPH_TOPIC_DOCUMENTS: dict[GraphTopic, str] = {
    "g1": "graph-g1-relations.md",
    "g2": "graph-g2-dataflow.md",
    "g5": SCENARIOS_DOCUMENT,
    "g6": "graph-g6-triples.md",
}

_SEQUENCE = re.compile(r"(?mis)^[ \t]*```mermaid[ \t]*\r?\n[ \t]*sequenceDiagram\b.*?^[ \t]*```[ \t]*$")
_INVALID_TEXT = re.compile(r"[\x00\ud800-\udfff]")


_DOMAIN_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")
_INLINE_MARKUP = re.compile(r"[\\`*_\[\]{}<>!&|]")


def escape_domain_text(text: str) -> str:
    return _INLINE_MARKUP.sub(r"\\\g<0>", text)


@dataclass(frozen=True, slots=True)
class CodeDomain:
    """Agent 根据索引推断的仓库级画像，组件清单由调用方从同版索引补齐。"""

    domain: str
    description: str
    keywords: tuple[str, ...]

    def serialize(self, components: Iterable[str] = ()) -> str:
        return json.dumps({
            "domain": self.domain, "description": self.description,
            "keywords": self.keywords, "components": list(components),
        }, ensure_ascii=False)

    def markdown(self) -> str:
        # 模型字段为纯文本，转义后可以通过常规 Markdown 知识条目展示和检索。
        domain = escape_domain_text(self.domain)
        description = escape_domain_text(self.description)
        keywords = "、".join(map(escape_domain_text, self.keywords))
        return "\n".join([
            "# 业务领域", "", f"- 领域 {domain}",
            *([f"- 简介 {description}"] if description else []),
            *([f"- 关键词 {keywords}"] if keywords else []),
        ])


def _domain_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or len(value) > limit or _DOMAIN_CONTROL.search(value):
        return None
    return value.strip()


def parse_code_domain(body: object) -> CodeDomain | None:
    """校验 Agent 及知识条目共用的 JSON。组件关系以索引为准。"""
    if not isinstance(body, dict):
        return None
    domain = _domain_text(body.get("domain"), 80)
    description = _domain_text(body.get("description", ""), 500)
    keywords = body.get("keywords", [])
    if not domain or description is None or not isinstance(keywords, list) or len(keywords) > 24:
        return None
    terms = []
    for term in keywords:
        checked = _domain_text(term, 60)
        if not checked:
            return None
        terms.append(checked)
    return CodeDomain(domain, description, tuple(dict.fromkeys(terms)))


def assign_code_domain(name: str, previous: CodeDomain | None = None) -> CodeDomain | None:
    domain = _domain_text(name, 80)
    if not domain:
        return None
    return replace(previous, domain=domain) if previous else CodeDomain(domain, "", ())


def read_code_domain(body: str) -> CodeDomain | None:
    try:
        return parse_code_domain(json.loads(body))
    except (ValueError, RecursionError):
        return None


class ComponentProfileFields(TypedDict):
    domain: str
    layer: str
    summary: str
    responsibilities: list[str]


@dataclass(frozen=True, slots=True)
class ComponentProfile:
    """从模块索引推断的职责。接口入口和关系由索引单独提供。"""

    domain: str
    layer: str
    summary: str
    responsibilities: tuple[str, ...]

    def fields(self) -> ComponentProfileFields:
        return {
            "domain": self.domain, "layer": self.layer, "summary": self.summary,
            "responsibilities": list(self.responsibilities),
        }

    def markdown(self, entrypoints: Iterable[str]) -> str:
        lines = [
            "# 模块职责", "", f"- 业务域 {escape_domain_text(self.domain)}", f"- 分层 {self.layer}",
            *([f"- 概述 {escape_domain_text(self.summary)}"] if self.summary else []),
            "", "## 职责",
            *(f"- {escape_domain_text(item)}" for item in self.responsibilities),
        ]
        anchors = [f"- {escape_domain_text(item)}" for item in entrypoints]
        if anchors:
            lines.extend(["", "## 源码入口", *anchors])
        return "\n".join(lines)


def parse_component_profile(value: object) -> ComponentProfile | None:
    if not isinstance(value, dict):
        return None
    domain = _domain_text(value.get("domain"), 80)
    layer = value.get("layer")
    summary = _domain_text(value.get("summary", ""), 240)
    raw = value.get("responsibilities", [])
    if (
        not domain or not isinstance(layer, str) or layer not in {"entry", "orchestration", "service", "data"}
        or summary is None or not isinstance(raw, list) or len(raw) > 8
    ):
        return None
    responsibilities = []
    for item in raw:
        checked = _domain_text(item, 180)
        if not checked:
            return None
        responsibilities.append(checked)
    if not (summary or responsibilities):
        return None
    return ComponentProfile(domain, layer, summary, tuple(dict.fromkeys(responsibilities)))


def read_component_profile(body: str) -> ComponentProfile | None:
    try:
        return parse_component_profile(json.loads(body))
    except (ValueError, RecursionError):
        return None


def valid_design_text(body: str) -> bool:
    return bool(body.strip()) and not _INVALID_TEXT.search(body) and len(body.encode("utf-8")) <= 52_000


def valid_repository_narrative(body: str) -> bool:
    return (
        valid_design_text(body) and len(body.encode("utf-8")) <= 16_000
        and body.strip().startswith(("# ", "## ")) and REPOSITORY_NARRATIVE_HEADING not in body
    )


def compose_repository_overview(evidence: str, narrative: str) -> str:
    return f"{evidence.rstrip()}\n\n{REPOSITORY_NARRATIVE_HEADING}\n\n{narrative.strip()}"


def valid_scenario_text(body: str) -> bool:
    diagrams = len(_SEQUENCE.findall(body))
    return 1 <= diagrams <= 5 or (diagrams == 0 and "证据不足" in body)


_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_RESERVED = {
    "architecture", "readme", "con", "prn", "nul", "aux",
    *(f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)),
}


class ComponentManifest(TypedDict):
    slug: str
    title: str
    confidence: str
    files: list[str]


@dataclass(frozen=True, slots=True)
class Component:
    slug: str
    name: str
    paths: tuple[str, ...]
    entrypoints: tuple[str, ...]

    @property
    def profile_name(self) -> str:
        return f"{self.slug}{COMPONENT_PROFILE_SUFFIX}"

    def manifest(self) -> ComponentManifest:
        return {"slug": self.slug, "title": self.name, "confidence": "EXTRACTED", "files": list(self.paths)}


def components_for(facts: dict[str, FileFacts]) -> tuple[Component, ...]:
    """与 TeamAI 的兜底划分一致，按路径首段确定组件边界。"""
    grouped: dict[str, list[str]] = defaultdict(list)
    for path, fact in sorted(facts.items()):
        if fact["parser"] != "skipped":
            grouped[path.split("/", 1)[0]].append(path)
    used = set(_RESERVED)
    components = []
    for name, paths in sorted(grouped.items()):
        candidate = name.lower().rstrip(".")
        if not _SAFE.fullmatch(name) or candidate.split(".", 1)[0] in used or candidate.startswith("graph-g"):
            # 中文目录与保留文件名也能生成文档，磁盘文件名与原始组件名称分开。
            candidate = "module-" + hashlib.sha256(name.encode()).hexdigest()[:24]
        while candidate in used:
            candidate = "module-" + hashlib.sha256((name + ":" + candidate).encode()).hexdigest()[:24]
        used.add(candidate)
        entrypoints = tuple(islice((
            f"{symbol['name']} ({path}:{symbol['line']})"
            for path in paths for symbol in facts[path]["symbols"]
            if re.search(r"handler|route|controller|endpoint|main|server|app", symbol["name"], re.I)
        ), 5))
        components.append(Component(candidate, name, tuple(paths), entrypoints))
    return tuple(components)


@dataclass(frozen=True, slots=True)
class DesignEdge:
    source: str
    target: str
    kind: str


@dataclass(frozen=True, slots=True)
class CallSite:
    source: str
    target: str
    name: str
    line: int


@dataclass(frozen=True, slots=True)
class DesignEvidence:
    project: str
    revision: str
    components: tuple[Component, ...]
    facts: dict[str, FileFacts]
    edges: tuple[DesignEdge, ...]
    calls: tuple[CallSite, ...]

    def owners(self) -> dict[str, str]:
        return {path: component.name for component in self.components for path in component.paths}

    def module_edges(self) -> Counter[tuple[str, str, str]]:
        owners = self.owners()
        return Counter(
            (owners[edge.source], edge.kind, owners[edge.target])
            for edge in self.edges if owners[edge.source] != owners[edge.target]
        )

    def call_lines(self) -> list[str]:
        return [f"{call.source}:{call.line} 调用 {call.name} → {call.target}" for call in self.calls]

    def interface_lines(self) -> list[str]:
        return [
            f"{path}:{symbol['line']} {symbol['kind']} {symbol['name']}"
            for path, fact in sorted(self.facts.items())
            for symbol in fact["symbols"] if symbol["kind"] in {"interface", "protocol", "trait"}
        ]


def design_evidence(project: str, revision: str, facts: dict[str, FileFacts]) -> DesignEvidence:
    components = components_for(facts)
    paths = {path for component in components for path in component.paths}
    go = go_code_index(facts, paths)
    swift = swift_code_index(facts, paths)
    javascript = JavaScriptIndex(facts, paths)
    edges: list[DesignEdge] = []
    call_sites: list[CallSite] = []
    for path in sorted(paths):
        calls = resolved_calls(path, facts, paths, go=go, swift=swift, javascript=javascript)
        facts_relations = relation_targets(
            dependencies(path, facts[path], paths, go=go, javascript=javascript),
            calls, resolved_implementations(path, facts, paths, javascript=javascript),
        )
        edges.extend(DesignEdge(path, target, kind) for kind, targets in facts_relations.items() for target in targets)
        call_sites.extend(CallSite(path, call["target"], call["symbol"], call["line"]) for call in calls)
    return DesignEvidence(project, revision, components, facts, tuple(edges), tuple(call_sites))


def _page(title: str, revision: str, records: list[str], empty: str) -> str:
    bullets = []
    budget = 52_000
    for row in records:
        # 路径和标识来自源码，按行引用，文件名里的 Markdown 标记保持为文本。
        text = row.replace("`", "ˋ").replace("\r", " ").replace("\n", " ")
        line = f"- `{text}`"
        budget -= len(line.encode()) + 1
        if budget < 0:
            break
        bullets.append(line)
    remaining = len(records) - len(bullets)
    if remaining:
        bullets.append(f"另有 {remaining} 项，请查阅原始代码图谱。")
    return "\n".join([f"# {title}", "", f"来源版本 `{revision}`", "", *(bullets or [empty]), ""])


def graph_documents(evidence: DesignEvidence) -> dict[str, str]:
    """从源码索引可确认的内容直接导出。运行时和业务结论保留证据缺口。"""
    edges = evidence.module_edges()
    neighbors: dict[str, set[str]] = defaultdict(set)
    for source, _, target in edges:
        neighbors[source].add(target)
    risks = [
        f"{component.name} 有 {len(component.paths)} 个已采集文件，直接依赖 {len(neighbors[component.name])} 个组件"
        for component in evidence.components
    ]
    cycles = sorted({tuple(sorted((a, b))) for a, adjacent in neighbors.items() for b in adjacent if a in neighbors[b]})
    risks.extend(f"{left} ↔ {right} 有双向源码依赖" for left, right in cycles)
    declarations = [
        (path, symbol["line"], symbol["name"])
        for path, fact in sorted(evidence.facts.items()) for symbol in fact["symbols"]
    ]
    pages = {
        GRAPH_TOPIC_DOCUMENTS["g1"]: (
            "G1 组件依赖矩阵",
            [f"{source} → {target} · {kind} · {count} 条" for (source, kind, target), count in sorted(edges.items())],
            "当前索引缺少跨组件关系。",
        ),
        GRAPH_TOPIC_DOCUMENTS["g2"]: (
            "G2 调用链与数据流", evidence.call_lines(), "当前索引缺少可解析的跨文件调用。",
        ),
        "graph-g3-interfaces.md": ("G3 接口证据", evidence.interface_lines(), "当前索引缺少可确认的接口声明。"),
        "graph-g4-errors.md": (
            "G4 错误声明",
            [f"{path}:{line} {name}" for path, line, name in declarations if re.search("error|exception", name, re.I)],
            "当前索引缺少可定位的错误声明。错误码及其语义需结合源码确认。",
        ),
        GRAPH_TOPIC_DOCUMENTS["g6"]: (
            "G6 知识图谱三元组",
            [f"({edge.source}, {edge.kind}, {edge.target})" for edge in evidence.edges],
            "当前索引缺少文件关系三元组。",
        ),
        "graph-g7-risks.md": ("G7 结构与影响范围线索", risks, "当前索引缺少可分析的组件。"),
        "graph-g8-config.md": (
            "G8 配置入口",
            [
                f"{path}:{line} {name}" for path, line, name in declarations
                if re.search("config|settings", name, re.I)
            ],
            "当前索引缺少可定位的配置声明。配置值和默认值需结合运行环境确认。",
        ),
        "graph-g9-constraints.md": ("G9 业务约束", [], "业务约束需要已发布的产品规则或明确的运行证据。"),
    }
    return {
        name: _page(title, evidence.revision, rows, empty) for name, (title, rows, empty) in pages.items()
    }


def design_context(evidence: DesignEvidence) -> dict[str, str]:
    return {
        "project": evidence.project,
        "revision": evidence.revision,
        "components": context_lines(
            (f"{component.name} · {len(component.paths)} 个索引文件" for component in evidence.components), 3000,
        ),
        "relations": context_lines(
            (
                f"{source} → {target} · {kind} · {total} 条"
                for (source, kind, target), total in sorted(evidence.module_edges().items())
            ),
            5000,
        ),
        "interfaces": context_lines(evidence.interface_lines(), 3500),
        "calls": context_lines(evidence.call_lines(), 3500),
    }


def component_profile_manifest(
    evidence: DesignEvidence, profiles: dict[str, ComponentProfile],
) -> dict[str, object]:
    """覆在原始清单上，缺少画像的组件仍保留可验证的信息。"""
    slugs = {item.name: item.slug for item in evidence.components}
    components: list[dict[str, object]] = []
    for item in evidence.components:
        row: dict[str, object] = dict(item.manifest())
        row["entrypoints"] = list(item.entrypoints)
        if profile := profiles.get(item.slug):
            row.update(dict(profile.fields()))
            row.update(category=profile.layer, confidence="INFERRED")
        components.append(row)
    return {
        "schema_version": 1, "project": evidence.project, "revision": evidence.revision,
        "components": components,
        "edges": [
            {"from": slugs[source], "to": slugs[target], "relation": kind, "confidence": "EXTRACTED", "count": count}
            for (source, kind, target), count in sorted(evidence.module_edges().items())
        ],
    }


def domain_context(evidence: DesignEvidence, architecture: dict[str, str]) -> dict[str, str]:
    # 标识符提供业务线索，结构化输入保持在索引范围内，不发送完整源码。
    declarations = context_lines(
        (
            f"{path}:{symbol['line']} {symbol['kind']} {symbol['name']}"
            for component in evidence.components for path in component.paths
            for symbol in evidence.facts[path]["symbols"][:4]
        ),
        5200,
    )
    return architecture | {"declarations": declarations}


def repository_narrative_context(evidence: DesignEvidence, architecture: dict[str, str]) -> dict[str, str]:
    """从已固定的源码清单提供仓库视角，避免另读工作目录或执行仓库命令。"""
    return domain_context(evidence, architecture) | {
        "indexed_files": context_lines(sorted(evidence.facts), 4800),
    }


def component_contexts(
    evidence: DesignEvidence, components: list[Component],
) -> dict[str, dict[str, str]]:
    owners = evidence.owners()
    connected: dict[str, list[str]] = defaultdict(list)
    for edge in evidence.edges:
        relation = f"{edge.source} → {edge.target} · {edge.kind}"
        source, target = owners[edge.source], owners[edge.target]
        connected[source].append(relation)
        if source != target:
            connected[target].append(relation)
    calls: dict[str, list[str]] = defaultdict(list)
    for call in evidence.calls:
        calls[owners[call.source]].append(f"{call.source}:{call.line} 调用 {call.name} → {call.target}")
    return {
        component.slug: {
            "project": evidence.project,
            "revision": evidence.revision,
            "component": component.name,
            "files": context_lines((f"{path} · {evidence.facts[path]['language']}" for path in component.paths), 3500),
            "declarations": context_lines(
                (
                    f"{path}:{symbol['line']} {symbol['kind']} {symbol['name']}"
                    for path in component.paths for symbol in evidence.facts[path]["symbols"]
                ),
                6200,
            ),
            "relations": context_lines(connected[component.name], 3500),
            "calls": context_lines(calls[component.name], 3500),
            "interfaces": context_lines(
                (
                    f"{path}:{symbol['line']} {symbol['kind']} {symbol['name']}"
                    for path in component.paths for symbol in evidence.facts[path]["symbols"]
                    if symbol["kind"] in {"interface", "protocol", "trait"}
                ),
                2200,
            ),
        }
        for component in components
    }


def scenario_connections(evidence: DesignEvidence, components: list[Component]) -> set[tuple[str, str]]:
    """只有索引已确认的跨组件调用或导入才进入场景计划。"""
    identifiers = {component.name: component.slug for component in components}
    return {
        (identifiers[source], identifiers[target])
        for source, kind, target in evidence.module_edges()
        if source in identifiers and target in identifiers and kind in {"REFERENCES", "DEPENDS_ON"}
    }


def context_lines(values: Iterable[str], limit: int) -> str:
    return "\n".join(islice(values, 350))[:limit]
