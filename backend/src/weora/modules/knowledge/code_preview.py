"""比较固定评审提交的可检索文件、声明节点与代码关系。"""

from collections import Counter
from dataclasses import asdict, dataclass

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

MAX_REVIEW_PATHS = 50
_MAX_ITEMS_PER_GROUP = 100


@dataclass(frozen=True)
class Node:
    path: str
    kind: str
    name: str
    line: int | None


def _nodes(
    files: dict[str, FileFacts], skipped: set[str],
) -> dict[tuple[str, str, str, int], tuple[Node, str]]:
    nodes: dict[tuple[str, str, str, int], tuple[Node, str]] = {}
    for path in sorted(files.keys() - skipped):
        file = files[path]
        nodes[(path, "file", path, 0)] = Node(path, "file", path, None), file["oid"]
        repeated: Counter[tuple[str, str]] = Counter()
        for fact in file["symbols"]:
            identity = fact["kind"], fact["name"]
            repeated[identity] += 1
            nodes[(path, *identity, repeated[identity])] = Node(path, *identity, fact["line"]), ""
    return nodes


def preview_delta(before: dict[str, FileFacts], after: dict[str, FileFacts]) -> dict[str, object]:
    """任一侧的源码无法提取时，跳过此文件，避免把未知节点报成新增或删除。"""
    skipped = {path for files in (before, after) for path, file in files.items() if file["parser"] == "skipped"}
    old, new = _nodes(before, skipped), _nodes(after, skipped)
    groups = {
        "added": [new[key][0] for key in sorted(new.keys() - old.keys())],
        "updated": [new[key][0] for key in sorted(new.keys() & old.keys()) if new[key] != old[key]],
        "removed": [old[key][0] for key in sorted(old.keys() - new.keys())],
    }
    return {
        "nodes": {kind: [asdict(node) for node in nodes[:_MAX_ITEMS_PER_GROUP]] for kind, nodes in groups.items()},
        "counts": {kind: len(nodes) for kind, nodes in groups.items()},
        "skipped": sorted(skipped),
    }


def _edges(
    files: dict[str, FileFacts], skipped: set[str],
) -> set[tuple[str, str, str]]:
    available = {path for path, facts in files.items() if facts["parser"] != "skipped"}
    go = go_code_index(files, available)
    swift = swift_code_index(files, available)
    javascript = JavaScriptIndex(files, available)
    edges: set[tuple[str, str, str]] = set()
    for path in available - skipped:
        # 先按各自提交解析，再省略无法对照的端点，避免误报其关系被移除。
        targets = relation_targets(
            dependencies(path, files[path], available, go=go, javascript=javascript),
            resolved_calls(path, files, available, go=go, swift=swift, javascript=javascript),
            resolved_implementations(path, files, available, javascript=javascript),
        )
        edges.update(
            (kind, path, target) for kind, destinations in targets.items()
            for target in destinations if target not in skipped
        )
    return edges


def relation_delta(before: dict[str, FileFacts], after: dict[str, FileFacts]) -> dict[str, object]:
    """跨完整索引范围比较，因此也能发现未修改的调用方受目标变更影响。"""
    skipped = {path for files in (before, after) for path, facts in files.items() if facts["parser"] == "skipped"}
    old, new = _edges(before, skipped), _edges(after, skipped)
    groups = {"added": sorted(new - old), "removed": sorted(old - new)}
    return {
        "relations": {
            change: [
                {"kind": kind, "source": source, "target": target}
                for kind, source, target in edges[:_MAX_ITEMS_PER_GROUP]
            ]
            for change, edges in groups.items()
        },
        "relation_counts": {change: len(edges) for change, edges in groups.items()},
        "relation_skipped": len(skipped),
    }
