"""按规则的适用路径派生关系，所有匹配都在已授权的项目池中进行。"""

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from uuid import UUID

import yaml
from sqlalchemy import Select, and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeRevision

_MAX_GLOBS = 64
_MAX_GLOB_LENGTH = 300
_ALIAS_SCALAR = re.compile(r"^(\s*(?:-\s+|paths:[ \t]+))([*&].*)$")


def _split_alternatives(value: str) -> list[str]:
    starts, depth = 0, 0
    parts = []
    for end, char in enumerate(value):
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(value[starts:end].strip())
            starts = end + 1
    return [*parts, value[starts:].strip()]


def _expand(value: str) -> tuple[str, ...]:
    """括号内的分支独立判断仓库归属，展开总量有界。"""
    pending = [value]
    expanded: list[str] = []
    while pending:
        glob = pending.pop()
        start, depth = 0, 0
        for end, char in enumerate(glob):
            if char == "{":
                if depth == 0:
                    start = end
                depth += 1
            elif char == "}" and depth:
                depth -= 1
                if depth:
                    continue
                alternatives = _split_alternatives(glob[start + 1:end])
                if len(alternatives) > 1:
                    pending.extend(glob[:start] + part + glob[end + 1:] for part in reversed(alternatives))
                    if len(pending) + len(expanded) > _MAX_GLOBS:
                        raise ValueError("规则路径展开后最多允许 64 个模式。")
                    break
        else:
            expanded.append(glob)
    return tuple(expanded)


def normalize_rule_paths(value: object) -> list[str]:
    """接受 Markdown 中的字符串或数组，保留书写形式并校验展开预算。"""
    raw: Sequence[object]
    if isinstance(value, str):
        raw = _split_alternatives(value)
    elif isinstance(value, list):
        raw = value
    else:
        raise ValueError("规则 paths 须为路径字符串或数组。")
    if len(raw) > _MAX_GLOBS:
        raise ValueError("规则路径最多允许 64 个模式。")
    paths: list[str] = []
    total = 0
    for item in raw:
        if not isinstance(item, str) or len(item) > _MAX_GLOB_LENGTH or any(not c.isprintable() for c in item):
            raise ValueError("规则路径必须是长度不超过 300 的文字。")
        path = item.strip()
        if path and path not in paths:
            total += len(_expand(path))
            if total > _MAX_GLOBS:
                raise ValueError("规则路径展开后最多允许 64 个模式。")
            paths.append(path)
    return paths


def _quote_pattern(line: str) -> str:
    """未加引号的 ** 模式在 YAML 中会被当作别名。"""
    match = _ALIAS_SCALAR.fullmatch(line)
    if not match:
        return line
    value = re.split(r"[ \t]+#", match[2], maxsplit=1)[0].rstrip()
    return match[1] + json.dumps(value, ensure_ascii=False)


def markdown_rule_paths(body: str) -> list[str] | None:
    """老版本只从保存在正文开头的前置信息恢复路径。"""
    lines = body.removeprefix("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return None
    raw = "\n".join(lines[1:end])
    if len(raw) > 8192 or not re.search(r"(?m)^paths[ \t]*:", raw):
        return None
    try:
        fields: object = yaml.safe_load(raw)
    except (yaml.YAMLError, RecursionError):
        try:
            fields = yaml.safe_load("\n".join(_quote_pattern(line) for line in lines[1:end]))
        except (yaml.YAMLError, RecursionError):
            return None
    if isinstance(fields, dict) and "paths" in fields:
        try:
            return normalize_rule_paths(fields["paths"])
        except ValueError:
            return None
    return None


@lru_cache(maxsize=512)
def _segments(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.removeprefix("./").split("/") if part)


def path_matches(pattern: str, path: str) -> bool:
    """仓库相对路径逐段匹配，** 跨目录，* 和 ? 只在单段内生效。"""
    parts = _segments(path)
    positions = {0}
    for mask in _segments(pattern):
        if not positions:
            return False
        if mask == "**":
            positions = set(range(min(positions), len(parts) + 1))
        else:
            # TeamAI 把方括号当作普通字符，保留 fnmatch 的有界通配实现。
            literal_brackets = mask.replace("[", "[[]")
            positions = {i + 1 for i in positions if i < len(parts) and fnmatchcase(parts[i], literal_brackets)}
    return len(parts) in positions


@dataclass(frozen=True, slots=True)
class RulePathEdge:
    rule: UUID
    rule_title: str
    rule_revision: int
    page: UUID
    page_title: str
    page_revision: int


async def derived_path_edges(
    db: AsyncSession,
    published: Select[tuple[KnowledgeEntry, KnowledgeRevision]],
    project: UUID,
    rule_ids: set[UUID] | None = None,
    page_ids: set[UUID] | None = None,
) -> list[RulePathEdge]:
    """计算指定结果的边，判断歧义时仍使用项目全部可见代码证据。"""
    path = KnowledgeRevision.source["path"].as_string()
    rules = (KnowledgeEntry.kind == "rule") & (func.json_array_length(KnowledgeRevision.rule_paths) > 0)
    # 只看规则侧时可提前缩小规则池。代码侧需要所有发布规则判断反向边。
    if rule_ids is not None and not page_ids:
        rules &= KnowledgeEntry.id.in_(rule_ids)
    if not await db.scalar(published.where(KnowledgeEntry.project_id == project, rules).with_only_columns(
        KnowledgeEntry.id,
    ).limit(1)):
        return []
    pages = and_(KnowledgeEntry.kind == "code", KnowledgeEntry.codebase_id.is_not(None), path != "")
    rows = (await db.execute(
        published.where(KnowledgeEntry.project_id == project, or_(rules, pages)).with_only_columns(
            KnowledgeEntry.id, KnowledgeRevision.title, KnowledgeRevision.revision, KnowledgeEntry.codebase_id,
            KnowledgeRevision.rule_paths, path,
        ).order_by(path, KnowledgeEntry.id),
    )).tuples().all()
    # 同一个路径模式可能被多条规则使用，完整项目池只匹配一次该模式。
    code_by_repo: dict[UUID, list[tuple[UUID, str, int, str]]] = defaultdict(list)
    scoped_rules: list[tuple[UUID, str, int, list[str]]] = []
    for entry, title, revision, repo, patterns, entry_path in rows:
        if repo is not None and entry_path:
            code_by_repo[repo].append((entry, title, revision, entry_path))
        elif patterns:
            scoped_rules.append((entry, title, revision, patterns))

    @lru_cache(maxsize=_MAX_GLOBS * 4)
    def unique_pages(glob: str) -> tuple[tuple[UUID, str, int], ...]:
        matches: tuple[tuple[UUID, str, int], ...] = ()
        for files in code_by_repo.values():
            found = tuple(
                (entry, title, revision)
                for entry, title, revision, entry_path in files
                if path_matches(glob, entry_path)
            )
            if found:
                if matches:
                    return ()
                matches = found
        return matches

    edges: list[RulePathEdge] = []
    for rule, title, revision, patterns in scoped_rules:
        targets = dict.fromkeys(
            candidate for pattern in patterns for glob in _expand(pattern) for candidate in unique_pages(glob)
        )
        edges.extend(
            RulePathEdge(rule, title, revision, page, page_title, page_revision)
            for page, page_title, page_revision in targets
            if rule_ids is None or rule in rule_ids or (page_ids is not None and page in page_ids)
        )
    return edges
