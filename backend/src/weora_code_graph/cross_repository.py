"""用可复现的导入、声明和数据锚点连接不同代码来源。"""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

from weora_code_graph.config_facts import CONFIG_LANGUAGES
from weora_code_graph.extractor import FileFacts, unresolved_imports
from weora_code_graph.javascript_index import JavaScriptIndex

_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_$-]{0,119}")
_CONFIG_DIRS = {"conf", "config", "configs", "configuration", "settings"}
_CONFIG_NAME = re.compile(r"(?:^|[_-])(?:config|settings)(?:$|[_-])", re.I)
_GENERIC = {"app", "base", "common", "config", "index", "init", "main", "settings", "types", "utils"}


@dataclass(frozen=True, slots=True)
class CodeOrigin:
    identity: str
    namespace: str
    revision: str
    files: dict[str, FileFacts]


@dataclass(frozen=True, slots=True)
class FileAnchor:
    origin: str
    namespace: str
    revision: str
    path: str
    blob: str


@dataclass(frozen=True, slots=True)
class CrossRepositoryEdge:
    source: FileAnchor
    target: FileAnchor
    evidence: str
    method: str


def _label(text: str) -> str | None:
    if not _NAME.fullmatch(text):
        return None
    normalized = text.replace("_", "").replace("-", "").casefold()
    if normalized and normalized not in _GENERIC:
        return normalized
    return None


def _import_label(specifier: str, language: str) -> str | None:
    if language == "Python":
        return _label(specifier.rsplit(".", 1)[-1])
    name = PurePosixPath(specifier).name
    if re.search(r"\.(?:tsx?|jsx?|[cm]js|vue|py|go|rs|java|swift|scala)$", name, re.I):
        name = PurePosixPath(name).stem
    return _label(name)


def _file_labels(path: str, facts: FileFacts) -> tuple[set[str], bool]:
    location = PurePosixPath(path)
    if facts["language"] == "SQL" or facts["language"] in CONFIG_LANGUAGES:
        kind = "table" if facts["language"] == "SQL" else "config"
        return {
            label for symbol in facts["symbols"] if symbol["kind"] == kind and (label := _label(symbol["name"]))
        }, True
    configuration = any(part.casefold() in _CONFIG_DIRS for part in location.parts[:-1]) or bool(
        _CONFIG_NAME.search(location.stem),
    )
    return {
        label
        for text in (location.stem, *(symbol["name"] for symbol in facts["symbols"]))
        if (label := _label(text))
    }, configuration


def _unique(anchors: set[FileAnchor], source: FileAnchor) -> FileAnchor | None:
    # 同一登记仓库的 checkout 和子目录属于同源。等名候选有歧义时两边都不连。
    if len(anchors) != 1:
        return None
    match = next(iter(anchors))
    return match if match.namespace != source.namespace else None


def cross_repository_edges(origins: list[CodeOrigin]) -> list[CrossRepositoryEdge]:
    if len({origin.namespace for origin in origins}) < 2:
        return []
    if len({origin.identity for origin in origins}) != len(origins):
        raise ValueError("代码来源身份必须唯一。")
    files: dict[tuple[str, str], FileAnchor] = {}
    targets: dict[str, set[FileAnchor]] = defaultdict(set)
    configuration: dict[str, set[FileAnchor]] = defaultdict(set)
    source_labels: dict[FileAnchor, set[str]] = {}
    components: dict[str, set[FileAnchor]] = defaultdict(set)
    candidates: dict[str, set[str]] = defaultdict(set)
    for origin in origins:
        for path, facts in origin.files.items():
            if facts["parser"] == "skipped":
                continue
            candidates[origin.identity].add(path)
            if facts["language"] == "JSConfig":
                continue
            anchor = FileAnchor(origin.identity, origin.namespace, origin.revision, path, facts["oid"])
            files[(origin.identity, path)] = anchor
            labels, is_config = _file_labels(path, facts)
            for name in labels:
                targets[name].add(anchor)
                if is_config:
                    configuration[name].add(anchor)
                else:
                    components[name].add(anchor)
            if not is_config:
                source_labels[anchor] = labels

    edges: dict[tuple[FileAnchor, FileAnchor], CrossRepositoryEdge] = {}
    for origin in origins:
        available = candidates[origin.identity]
        javascript = JavaScriptIndex(origin.files, available)
        for path in sorted(available):
            facts = origin.files[path]
            if (source := files.get((origin.identity, path))) is None:
                continue
            for spec in unresolved_imports(path, facts, available, javascript=javascript):
                label = _import_label(spec, facts["language"])
                if label and (target := _unique(targets[label], source)) is not None:
                    edges.setdefault((source, target), CrossRepositoryEdge(source, target, spec, "import"))

    for code, labels in source_labels.items():
        for label in sorted(labels):
            if (
                len(label) >= 5 and len(components[label]) == 1
                and (target := _unique(configuration[label], code)) is not None
            ):
                edges.setdefault((code, target), CrossRepositoryEdge(code, target, label, "configuration"))

    return sorted(
        edges.values(), key=lambda item: (item.source.origin, item.source.path, item.target.origin, item.target.path),
    )
