"""校验 TeamAI 关系声明的结构、适用类型和逻辑路径。"""

from typing import NamedTuple

from pydantic import JsonValue

from weora.modules.knowledge.markdown_paths_v1 import declared_path

_DECLARERS = {"doc", "rule", "learning"}
_RELATIONS = {"MAPS_TO", "REFERENCES", "SUPERSEDES", "CONFLICTS_WITH"}
_KINDS = {"rules": "rule", "docs": "doc", "learnings": "learning", "skills": "skill", "teamwiki": "teamwiki"}
_MAX_TARGETS = 30


class Declaration(NamedTuple):
    kind: str
    path: str


def declarations(
    kind: str, own_path: str | None, related: JsonValue,
) -> tuple[list[Declaration], list[dict[str, str]]]:
    """经验忽略无效声明，规则和文档保留结构问题供审核判断。"""
    links: list[Declaration] = []
    findings: list[dict[str, str]] = []
    level = "warn" if kind == "learning" else "fail"

    def report(code: str, message: str, *, severity: str = level) -> None:
        findings.append({"level": severity, "code": code, "message": message})

    if related is None:
        return links, findings
    if not isinstance(related, dict):
        report("invalid_mapping", "related 需要按关系类型填写目标。")
        return links, findings
    if kind not in _DECLARERS and related:
        report("invalid_source", "规则、文档和经验可以声明 related 关系。")
        return links, findings

    inspected = 0
    for index, (label, targets) in enumerate(related.items()):
        if index >= _MAX_TARGETS:
            report("too_many_targets", f"related 最多检查 {_MAX_TARGETS} 个关系键。")
            break
        relation = label.upper()
        if relation not in _RELATIONS:
            report("invalid_relation", f"related 包含不支持的关系类型 {label[:80]}。")
            continue
        if not isinstance(targets, (str, list)):
            report("invalid_targets", f"{relation} 需要填写团队路径或路径列表。")
            continue
        for target in targets if isinstance(targets, list) else [targets]:
            inspected += 1
            if inspected > _MAX_TARGETS:
                report("too_many_targets", f"related 最多检查 {_MAX_TARGETS} 个声明目标。")
                return links, findings
            path = declared_path(target) if isinstance(target, str) and len(target) <= 1000 else None
            if path is None:
                report("invalid_path", f"{relation} 需要填写有效的团队资源路径。")
                continue
            if path == own_path:
                report("self_reference", f"{relation} 的目标 {path} 是条目自身。", severity="warn")
                continue
            target_kind = _KINDS[path.split("/", 1)[0]]
            if not _allowed(kind, relation, target_kind):
                hint = "。经验与规则有冲突时使用 CONFLICTS_WITH" if kind == "learning" and target_kind == "rule" else ""
                report("incompatible_relation", f"{relation} 不适用于从 {kind} 到 {path} 的关系{hint}。")
                continue
            link = Declaration(relation, path)
            if link not in links:
                links.append(link)
    return links, findings


def _allowed(source: str, relation: str, target: str) -> bool:
    if relation == "REFERENCES":
        return True
    if relation == "MAPS_TO":
        return target == "teamwiki"
    if relation == "SUPERSEDES":
        return target in {"doc", "learning"} or (target == "rule" and source == "rule")
    return target in _DECLARERS
