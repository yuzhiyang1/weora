"""按产品资料和代码证据生成可复现的映射、缺口和冲突报告。"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import TypedDict

_IDENTIFIER = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")
_INLINE = re.compile(r"(?<!\x60)\x60([^\x60\r\n]{2,160})\x60(?!\x60)")
_CHINESE = re.compile(r"[\u4e00-\u9fff]{2,6}")
_ENDPOINT = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_./{}:-]*)")
_ROUTE = re.compile(r"(?<![A-Za-z0-9_./{}:-])(/[A-Za-z0-9_./{}:-]+)(?![A-Za-z0-9_./{}:-])")
_COUNT = re.compile(r"(\d+)\s*(?:states?\b|个状态)", re.IGNORECASE)
_ENUM = re.compile(r"\x60([A-Z_]+(?:\|[A-Z_]+)+)\x60")
_BEHAVIOR = (
    (re.compile(r"\b(?:sync|synchronous)\b", re.I), re.compile(r"\b(?:async|asynchronous)\b", re.I)),
    (re.compile(r"(?<!non-)\bblocking\b", re.I), re.compile(r"\bnon-blocking\b", re.I)),
)


@dataclass(frozen=True, slots=True)
class Page:
    id: str
    title: str
    body: str
    ref: str
    revision: str
    reviewed_targets: frozenset[str] = frozenset()
    updated: date | None = None


class Reference(TypedDict):
    id: str
    path: str
    revision: str


class Mapping(TypedDict):
    source: Reference
    target: Reference
    relation: str
    term: str
    confidence: float


class Finding(TypedDict):
    kind: str
    sources: list[Reference]
    evidence: list[str]


class ApiMatch(TypedDict):
    source: Reference
    target: Reference
    method: str
    path: str
    confidence: float


class StaleWarning(TypedDict):
    source: Reference
    target: Reference
    days_drift: int
    severity: str


class Report(TypedDict):
    mappings: list[Mapping]
    gaps: list[Finding]
    conflicts: list[Finding]
    relation_candidates: list[Mapping]
    api_matches: list[ApiMatch]
    rule_matches: list[Mapping]
    stale_warnings: list[StaleWarning]


def _reference(page: Page) -> Reference:
    return {"id": page.id, "path": page.ref, "revision": page.revision}


def _terms(text: str) -> set[str]:
    inline = {value.strip() for value in _INLINE.findall(text) if len(value.strip()) >= 2}
    return inline | set(_IDENTIFIER.findall(text)) | set(_CHINESE.findall(text))


def _matchers(terms: set[str]) -> dict[str, re.Pattern[str]]:
    # 英文标识符必须完整匹配。中文词和路径可以出现在更长的证据文本中。
    return {
        term: re.compile(r"(?<![\w])" + re.escape(term) + r"(?![\w])" if term.isascii() and term.isidentifier()
                         else re.escape(term))
        for term in sorted(terms)
    }


def _finding(kind: str, pages: Sequence[Page], *evidence: str) -> Finding:
    return {"kind": kind, "sources": [_reference(page) for page in pages], "evidence": list(evidence)}


def documented_update(text: str) -> date | None:
    match = re.search(r"(?im)^updated\s*:\s*['\"]?(\d{4}-\d{2}-\d{2})\b", text)
    if match:
        try:
            return date.fromisoformat(match[1])
        except ValueError:
            pass
    return None


def _conflicts(product: Page, code: Page) -> list[Finding]:
    findings = []
    for kind, pattern in (("COUNT_MISMATCH", _COUNT), ("STATE_MISMATCH", _ENUM)):
        first = set(pattern.findall(product.body))
        second = set(pattern.findall(code.body))
        # 从各页只提取到一组明确声明时才报告，避免将不同对象的多个状态集互相比较。
        if len(first) == len(second) == 1:
            left, right = next(iter(first)), next(iter(second))
            count_differs = kind == "COUNT_MISMATCH" and left.lstrip("0") != right.lstrip("0")
            states_differ = kind == "STATE_MISMATCH" and set(left.split("|")) != set(right.split("|"))
            if count_differs or states_differ:
                findings.append(_finding(kind, (product, code), left, right))
    for positive, negative in _BEHAVIOR:
        left_behavior = [value.group() for pattern in (positive, negative) if (value := pattern.search(product.body))]
        right_behavior = [value.group() for pattern in (positive, negative) if (value := pattern.search(code.body))]
        if len(left_behavior) == len(right_behavior) == 1 and (
            bool(positive.search(left_behavior[0])) != bool(positive.search(right_behavior[0]))
        ):
            findings.append(_finding("BEHAVIOR_MISMATCH", (product, code), *left_behavior, *right_behavior))
    return findings


def _mapping(source: Page, target: Page, term: str, *, near_title: bool) -> Mapping:
    return {
        "source": _reference(source), "target": _reference(target), "relation": "MAPS_TO",
        "term": term, "confidence": 1.0 if near_title else 0.9,
    }


def reconcile(products: Sequence[Page], code: Sequence[Page], resources: Sequence[Page]) -> Report:
    """按两端发布证据计算关系。普通文档和规则只产生待评审候选。"""
    mappings: list[Mapping] = []
    gaps: list[Finding] = []
    conflicts: list[Finding] = []
    candidates: list[Mapping] = []
    api_matches: list[ApiMatch] = []
    rule_matches: list[Mapping] = []
    stale_warnings: list[StaleWarning] = []
    mapped_code: set[str] = set()
    routes: dict[str, list[Page]] = {}
    methods: dict[tuple[str, str], set[str]] = {}
    for page in code:
        for path in set(_ROUTE.findall(page.body)):
            routes.setdefault(path, []).append(page)
        for method, path in _ENDPOINT.findall(page.body):
            methods.setdefault((page.id, path), set()).add(method)

    for product in sorted(products, key=lambda page: page.id):
        matchers = _matchers(_terms(product.body))
        rules = {term for term in _INLINE.findall(product.body) if 3 <= len(term) <= 50}
        matched = False
        for target in code:
            matches = [
                (term, bool(pattern.search(target.title)))
                for term, pattern in matchers.items() if pattern.search(target.body)
            ]
            if matches:
                matched = True
                mapped_code.add(target.id)
                mappings.extend(_mapping(product, target, term, near_title=near) for term, near in matches)
                conflicts.extend(_conflicts(product, target))
                for term, _ in matches:
                    if term in rules:
                        rule_matches.append({
                            "source": _reference(product), "target": _reference(target), "relation": "MAPS_TO",
                            "term": term, "confidence": 0.85,
                        })
                if product.updated and target.updated:
                    days = abs((target.updated - product.updated).days)
                    if days > 30:
                        stale_warnings.append({
                            "source": _reference(product), "target": _reference(target),
                            "days_drift": days, "severity": "critical" if days > 60 else "warning",
                        })
        if not matched:
            gaps.append(_finding("NO_CODE_MAPPING", (product,)))
            gaps.extend(
                _finding("CONCEPT_NOT_IMPLEMENTED", (product,), term)
                for term in sorted(set(_IDENTIFIER.findall(product.body)))
            )
        for method, path in sorted(set(_ENDPOINT.findall(product.body))):
            confirmed = False
            for target in routes.get(path, []):
                implemented = methods.get((target.id, path))
                # 仅路径命中是低置信证据。页面明确列出方法时，错误方法不能消除缺口。
                same_method = implemented is not None and method in implemented
                confirmed |= same_method or not implemented
                api_matches.append({
                    "source": _reference(product), "target": _reference(target), "method": method,
                    "path": path, "confidence": 1.0 if same_method else 0.7,
                })
            if not confirmed:
                gaps.append(_finding("API_DOC_NO_IMPL", (product,), f"{method} {path}"))

    gaps.extend(_finding("NO_PRODUCT_DOC", (target,)) for target in code if target.id not in mapped_code)
    for resource in sorted(resources, key=lambda page: page.id):
        matchers = _matchers(_terms(resource.body))
        for target in code:
            if target.id in resource.reviewed_targets:
                continue
            for term, pattern in matchers.items():
                if pattern.search(target.body):
                    candidates.append(_mapping(resource, target, term, near_title=bool(pattern.search(target.title))))
                    break
    return {
        "mappings": mappings, "gaps": gaps, "conflicts": conflicts,
        "relation_candidates": candidates, "api_matches": api_matches,
        "rule_matches": rule_matches, "stale_warnings": stale_warnings,
    }
