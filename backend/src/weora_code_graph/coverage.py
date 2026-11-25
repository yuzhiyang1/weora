"""从已采集事实识别知识缺口，范围仅限当前索引。"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from sys import stdlib_module_names
from typing import NotRequired, TypedDict

from weora_code_graph.extractor import FileFacts, unresolved_imports
from weora_code_graph.javascript_index import JavaScriptIndex

GAP_PAGE = "gaps/detected.md"
GAP_EVIDENCE = "gaps/evidence.json"
_INTERFACE = {"interface", "protocol", "trait"}
_COMPONENT = {"class", "struct", "record", "function", "func", "def", "enum", "component"}
_QUERIES: dict[str, tuple[str, ...]] = {
    "EXTERNAL_DEP_UNDOCUMENTED": ("外部依赖", "外部包", "external dependency", "external dependencies"),
    "EXTERNAL_IMPORT": ("外部导入", "external import"),
    "UNRESOLVED_IMPORT": ("未解析导入", "unresolved import"),
    "IMPL_MISSING": ("接口实现", "未实现接口", "interface implementation"),
    "LOW_CONNECTIVITY": ("孤立节点", "孤立组件", "orphan nodes", "graph connectivity"),
    "ERROR_HANDLING_UNDOCUMENTED": ("错误处理", "异常处理", "error handling", "exception handling"),
    "CONFIG_UNDOCUMENTED": ("环境变量", "部署配置", "configuration", "environment variable"),
    "PARSE_SKIP": ("语法解析", "解析失败", "parse skip", "parse error"),
    "SCAN_TRUNCATED": ("索引范围", "索引截断", "scan coverage", "index truncation"),
}


class GapSource(TypedDict):
    name: str
    path: str
    line: int | None
    alias: NotRequired[str]


class GapFinding(TypedDict):
    id: str
    kind: str
    description: str
    examples: list[str]
    paths: list[str]
    sources: NotRequired[list[GapSource]]


def detect_gaps(
    manifest: dict[str, FileFacts], edges: Mapping[str, Sequence[str]], implementations: set[tuple[str, str]],
    javascript: JavaScriptIndex, candidates: int,
) -> list[GapFinding]:
    findings: list[GapFinding] = []

    def add(identifier: str, kind: str, description: str, samples: Sequence[str] = (),
            sources: Sequence[GapSource] = ()) -> None:
        finding: GapFinding = {
            "id": identifier, "kind": kind, "description": description,
            "examples": sorted(set(samples))[:24], "paths": sorted({item["path"] for item in sources})[:24],
        }
        if sources:
            finding["sources"] = sorted(sources, key=lambda row: (row["name"], row["path"], row["line"] or 0))
        findings.append(finding)

    files = set(edges)
    external: dict[str, list[GapSource]] = defaultdict(list)
    unresolved_paths: dict[str, list[GapSource]] = defaultdict(list)
    missing: list[GapSource] = []
    symbols = [symbol for path in files for symbol in manifest[path]["symbols"]]
    components = {symbol["name"].casefold() for symbol in symbols if symbol["kind"] in _COMPONENT}
    components_count = sum(symbol["kind"] in _COMPONENT for symbol in symbols)
    implemented = {(path, name.casefold()) for path, name in implementations}
    for path in sorted(files):
        facts = manifest[path]
        unresolved = set(unresolved_imports(path, facts, files, javascript=javascript))
        local_python = {
            spec.split(".")[0] for spec in set(facts["imports"]) - unresolved if not spec.startswith(".")
        } if facts["language"] == "Python" else set()
        locations: dict[str, set[int | None]] = defaultdict(set)
        for site in facts.get("import_sites", []):
            locations[site["specifier"]].add(site["line"])
        if "import_sites" not in facts:
            for spec in unresolved:
                locations[spec].add(None)
        for spec in sorted(unresolved & locations.keys()):
            occurrences: list[GapSource] = [
                {"name": spec, "path": path, "line": line} for line in locations[spec]
            ]
            if spec.startswith((".", "/", "@")):
                unresolved_paths[spec].extend(occurrences)
                continue
            if spec.startswith("node:"):
                continue
            name = spec
            if facts["language"] == "Python":
                # Python 同时保存模块和成员导入，只报告位于扫描范围外的顶层依赖。
                name = spec.split(".")[0]
                if name in local_python or name in stdlib_module_names:
                    continue
            for occurrence in occurrences:
                if name != spec:
                    occurrence["alias"] = name
            external[name].extend(occurrences)
        for symbol in facts["symbols"]:
            name = symbol["name"].casefold()
            aliases = {name, name.removeprefix("i"), name + "impl"}
            if symbol["kind"] in _INTERFACE and not (
                aliases & components or (path, name) in implemented
            ):
                missing.append({"name": symbol["name"], "path": path, "line": symbol["line"]})

    if unresolved_paths:
        add("unresolved-imports", "UNRESOLVED_IMPORT", "当前索引尚未解析这些相对路径或别名导入。",
            list(unresolved_paths), [item for group in unresolved_paths.values() for item in group])
    if external:
        sources = [item for group in external.values() for item in group]
        add("external-imports", "EXTERNAL_IMPORT", "这些外部导入缺少本索引的目标源码。", list(external), sources)
        if len(external) > 5:
            add("unresolved-external-deps", "EXTERNAL_DEP_UNDOCUMENTED",
                f"有 {len(external)} 个外部依赖缺少本索引内的源码证据。", list(external), sources)
    if len(missing) > 3:
        add("interface-no-impl", "IMPL_MISSING", f"有 {len(missing)} 个接口的实现未在当前索引中确认。",
            [item["name"] for item in missing], missing)
    connected = {source for source, targets in edges.items() if targets}
    connected.update(target for targets in edges.values() for target in targets)
    isolated = sorted(files - connected)
    if len(isolated) > 5 and len(isolated) > len(files) * 0.3:
        add("high-orphan-ratio", "LOW_CONNECTIVITY", f"有 {len(isolated)}/{len(files)} 个源码节点缺少关系证据。",
            sources=[{"name": "", "path": path, "line": None} for path in isolated])
    if components_count > 10 and not any(
        symbol["kind"] == "error" or symbol["name"].endswith(("Error", "Exception")) for symbol in symbols
    ):
        add("no-error-patterns", "ERROR_HANDLING_UNDOCUMENTED", "当前索引尚未识别出错误处理类型。")
    has_config = any(symbol["kind"] == "config" for symbol in symbols) or any(
        "javascript_config" in fact and fact["javascript_config"]["valid"] for fact in manifest.values()
    )
    if components_count > 10 and not has_config:
        add("no-config-detected", "CONFIG_UNDOCUMENTED", "当前索引尚未识别出配置字段。")
    skipped = sorted(
        path for path, fact in manifest.items() if fact["parser"] == "skipped"
        or (fact["language"] == "Python" and fact["parser"] != "ast")
        or ("javascript_config" in fact and not fact["javascript_config"]["valid"])
    )
    if skipped:
        add("parse-skipped", "PARSE_SKIP", f"有 {len(skipped)} 个文件未完成完整语法解析。",
            sources=[{"name": "", "path": path, "line": None} for path in skipped])
    if candidates > len(manifest):
        add("scan-truncated", "SCAN_TRUNCATED",
            f"候选文件共 {candidates} 个，有 {candidates - len(manifest)} 个未纳入本次索引。")
    return findings


def render_gaps(findings: Sequence[GapFinding]) -> str:
    lines = ["# 知识缺口", "", "缺口来自当前索引的扫描事实，仍需结合源码或团队文档核对。", "",
             "| ID | 类别 | 状态 | 说明 | 源码或标识样例 |", "| --- | --- | --- | --- | --- |"]
    for gap in findings:
        samples = (gap["examples"][:3] + gap["paths"][:3])[:5]
        literals = [item.replace("|", "／").replace("`", "ˋ").replace("\n", " ").replace("\r", " ") for item in samples]
        sources = ", ".join(f"`{item}`" for item in literals)
        lines.append(f"| {gap['id']} | {gap['kind']} | open | {gap['description']} | {sources or '—'} |")
    if not findings:
        lines.append("| — | — | — | 本次索引未发现预定义的知识缺口 | — |")
    return "\n".join([*lines, ""])


def matches_gap(gap: GapFinding, query: str, terms: set[str]) -> bool:
    """匹配已登记名称、源码路径或明确的缺口主题。"""
    return any(
        sample.casefold() in terms for sample in gap["examples"]
    ) or any(
        path.casefold() in terms or path.rsplit("/", 1)[-1].casefold() in terms for path in gap["paths"]
    ) or any(phrase in query.casefold() for phrase in _QUERIES.get(gap["kind"], ()))
