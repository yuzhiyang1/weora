"""Markdown 逻辑路径索引的第一版规则，数据库回填也使用这一版。"""

import re
from hashlib import sha256
from posixpath import dirname, join, normpath
from urllib.parse import unquote
from uuid import UUID

from markdown_it import MarkdownIt

from weora.modules.knowledge.resource_paths import normalize_resource_path

_ROOTS = ("docs/", "rules/", "learnings/", "skills/", "teamwiki/")
_FILE_ROOTS = ("docs/", "rules/", "teamwiki/")
_SCHEME = re.compile(r"^[a-z][a-z\d+.-]*:", re.IGNORECASE)
_DECLARERS = frozenset({"doc", "rule", "learning"})
_MARKDOWN = MarkdownIt("commonmark", {"store_labels": True})


def _canonical(path: str) -> str | None:
    normalized = normalize_resource_path(path)
    if normalized is None:
        return None
    if normalized.startswith(_FILE_ROOTS) and not normalized.endswith(".md"):
        return f"{normalized}.md"
    if normalized.startswith("learnings/") and normalized.lower().endswith(".md"):
        return normalized[:-3]
    if normalized.startswith("skills/") and normalized.lower().endswith("/skill.md"):
        return normalized[:-9] if normalized.count("/") > 1 else None
    return normalized


def declared_path(raw: str) -> str | None:
    """关系声明从团队根解析，允许在仍位于团队树内时归并中间的父目录。"""
    if not raw.strip() or not raw.isprintable() or ":" in raw:
        return None
    path = _canonical(normpath(raw.strip().replace("\\", "/")))
    return path if path is not None and path.startswith(_ROOTS) else None


def entry_path(entry: UUID, kind: str, source: dict[str, str]) -> str | None:
    """用首次贡献的逻辑路径标识条目。手工经验可通过自身 ID 链接。"""
    if kind not in _DECLARERS and kind != "skill":
        return None
    path = source.get("resource_path") or (source.get("ref") if source.get("type") in {"file", "wiki"} else None)
    if path := _canonical(path or ""):
        return path
    return f"learnings/{entry}" if kind == "learning" else None


def _candidate(origin: str, href: str) -> str | None:
    if href.startswith("/") or _SCHEME.match(href):
        return None
    raw = re.split(r"[#?]", href, maxsplit=1)[0]
    try:
        path = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        path = raw
    path = path.replace("\\", "/")
    if not path or path.startswith("/") or _SCHEME.match(path):
        return None
    resolved = normpath(path if path.startswith(_ROOTS) else join(dirname(origin), path))
    return _canonical(resolved)


def indexed_paths(kind: str, origin: str | None, body: str) -> list[tuple[str, str]]:
    """首行代表自身路径，其余按正文顺序保存引用的逻辑路径。"""
    if origin is None:
        return []
    paths = {origin: sha256(origin.encode()).hexdigest()}
    if kind not in _DECLARERS or not ("](" in body or "]:" in body):
        return [(origin, paths[origin])]
    # CommonMark 负责处理代码块、嵌套括号、转义和引用定义。这里只读取链接 token。
    env: dict[str, object] = {}
    tokens = _MARKDOWN.parse(body, env)
    hrefs = [
        href for block in tokens for token in (block.children or [])
        if token.type == "link_open" and "label" not in token.meta
        and isinstance(href := token.attrGet("href"), str)
    ]
    definitions = env.get("references")
    if isinstance(definitions, dict):
        # TeamAI 也索引尚未在正文使用的链接定义，方便预先登记文档依赖。
        hrefs.extend(item["href"] for item in definitions.values() if isinstance(item.get("href"), str))
    for href in hrefs:
        path = _candidate(origin, href)
        if path is not None and path not in paths:
            paths[path] = sha256(path.encode()).hexdigest()
    return list(paths.items())
