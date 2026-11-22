"""从已分类清单逐条确认导入，不向服务端上传用户跳过的条目。"""

import json
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from weora_cli.collab_client import CollabError, as_object
from weora_cli.commands import JsonValue

_KINDS = {"learning", "rule", "doc", "code"}
_SOURCE_TYPES = {"file", "wiki", "pull_request", "repository"}


def review_markdown(item: dict[str, JsonValue]) -> str:
    """PR/MR 导出和草稿重叠比较使用同一份 TeamAI Markdown 结构。"""
    source = as_object(item["source"])
    fields = {
        "title": item["title"],
        "author": source.get("review_author") or "unknown",
        "date": str(source.get("review_date") or "")[:10],
        "tags": item.get("tags", []),
        "confidence": 0.85,
        "source_mr": source["ref"],
    }
    header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    return f"---\n{header}\n---\n\n{item['body']}\n"


def select_imports(path: Path, *, accept_all: bool, project: str | None) -> list[JsonValue]:
    """先校验整个分类清单，再让人逐条接受、改标题或跳过。"""
    if not accept_all and not sys.stdin.isatty():
        raise CollabError("KNOWLEDGE_IMPORT_TERMINAL_REQUIRED", "逐条审阅需要终端，已确认整批时使用 --all。")
    try:
        with path.open(encoding="utf-8-sig") as stream:
            raw = stream.read(2_000_001)
        entries: JsonValue = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise CollabError("KNOWLEDGE_IMPORT_INVALID", "无法读取导入分类清单。") from exc
    if len(raw) > 2_000_000 or not isinstance(entries, list) or len(entries) > 25:
        raise CollabError("KNOWLEDGE_IMPORT_INVALID", "每份清单最多包含 25 条知识。")
    candidates = [_validate(as_object(entry)) for entry in entries]
    accepted: list[JsonValue] = []
    for item in candidates:
        if item is None:
            continue
        payload = item.copy() if accept_all else review_import(item)
        if payload is None:
            continue
        # 同一内容、出处和作用域的清单可以安全重试。
        operation = uuid5(NAMESPACE_URL, json.dumps([project, payload], sort_keys=True, ensure_ascii=False))
        payload["operation_id"] = str(operation)
        accepted.append(payload)
    return accepted


def review_import(
    item: dict[str, JsonValue], *, similar_titles: list[JsonValue] | None = None,
) -> dict[str, JsonValue] | None:
    """逐条决定时展示类型、摘要和出处，预览保持在 stderr。"""
    source = as_object(item.get("source"))
    preview = f"\n{item['title']} ({item['kind']}) · {source['ref']}\n{item['body']}"
    # 正文来自文件和分类器，清掉会被终端解释为控制指令的字符。
    safe_preview = "".join(character for character in preview if character.isprintable() or character in "\n\t")
    print(safe_preview, file=sys.stderr)
    if item.get("tags"):
        print(f"标签 {item['tags']}", file=sys.stderr)
    if item.get("rule_paths"):
        safe_paths = "".join(char for char in str(item["rule_paths"]) if char.isprintable())
        print(f"适用路径 {safe_paths}", file=sys.stderr)
    if similar_titles:
        print("可能相关的已有经验", file=sys.stderr)
        for title in similar_titles:
            print("- " + "".join(character for character in str(title) if character.isprintable()), file=sys.stderr)
    while True:
        print("接受 accept (a) / 改标题 edit (e) / 跳过 skip (s)", file=sys.stderr)
        choice, _, note = input().strip().partition(" ")
        choice = choice.lower()
        if choice in {"s", "skip"}:
            return None
        if choice in {"a", "accept"}:
            return item.copy()
        if choice in {"e", "edit"}:
            if not note.strip():
                print("输入新的标题", file=sys.stderr)
                note = input()
            title = note.strip()
            if 0 < len(title) <= 160 and "\x00" not in title:
                return item | {"title": title}
            print("改后的标题须为 1 至 160 个字符。", file=sys.stderr)


def _validate(item: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    kind = item.get("kind")
    if kind == "personal":
        return None
    title, body = item.get("title"), item.get("body")
    source = as_object(item.get("source"))
    source_type, ref = source.get("type"), source.get("ref")
    resource_path = source.get("resource_path")
    tags = item.get("tags", [])
    paths = item.get("rule_paths")
    if (
        not isinstance(kind, str) or kind not in _KINDS
        or not isinstance(title, str) or not 0 < len(title.strip()) <= 160
        or not isinstance(body, str) or not body.strip() or len(body) > 64000
        or not isinstance(source_type, str) or source_type not in _SOURCE_TYPES
        or not isinstance(ref, str) or not 0 < len(ref.strip()) <= 1000
        or (resource_path is not None and (
            not isinstance(resource_path, str) or not 0 < len(resource_path.strip()) <= 1000
        ))
        or not isinstance(tags, list) or len(tags) > 24
        or any(not isinstance(tag, str) or not 0 < len(tag.strip()) <= 60 for tag in tags)
        or (paths is not None and (
            kind != "rule" or not (isinstance(paths, str) or (
                isinstance(paths, list) and all(isinstance(path, str) for path in paths)
            ))
        ))
    ):
        raise CollabError("KNOWLEDGE_IMPORT_INVALID", "清单中的内容、类型或出处格式无效。")
    provenance: dict[str, JsonValue] = {"type": source_type, "ref": ref}
    if resource_path is not None:
        provenance["resource_path"] = resource_path
    result: dict[str, JsonValue] = {"kind": kind, "title": title, "body": body, "tags": tags, "source": provenance}
    if paths is not None:
        result["rule_paths"] = paths
    if item.get("related") is not None:
        result["related"] = item["related"]
    return result
