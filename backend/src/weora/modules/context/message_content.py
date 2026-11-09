"""飞书接收结构的有界纯解析；不请求链接、不下载资源、不执行卡片行为。

结构依据：飞书官方 message-content-description/message_content.md。
只消费已知字段，未知节点留下缺口；供应商原始 JSON 不进入模型或快照。
"""

import html
import json
import re
from dataclasses import dataclass, field
from typing import cast

from weora.modules.context.domain import Json, ParseStatus

MAX_TEXT = 24000
MAX_RESOURCES = 20
MAX_NODES = 200
RESOURCE_LABELS = {
    "image": "图片",
    "file": "文件",
    "folder": "文件夹",
    "audio": "音频",
    "media": "视频",
    "sticker": "表情包",
}


@dataclass
class ParsedContent:
    text: str = ""
    status: ParseStatus = "complete"
    notes: list[str] = field(default_factory=list)
    resources: list[Json] = field(default_factory=list)

    def gap(self, note: str) -> None:
        self.status = "partial"
        if note not in self.notes and len(self.notes) < 32:
            self.notes.append(note)


def _string(value: object, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError
    return value


def _resource(kind: str, node: Json, parsed: ParsedContent) -> str:
    resource: Json = {"type": kind, "downloaded": False, "parsed": False}
    for key in ("file_key", "image_key"):
        if key in node:
            value = _string(node[key], 512)
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                raise ValueError
            resource[key] = value
    required = "image_key" if kind == "image" else "file_key"
    if required not in resource:
        raise ValueError
    if "file_name" in node:
        resource["file_name"] = _string(node["file_name"], 255)
    if "duration" in node:
        duration = node["duration"]
        if type(duration) is not int or not 0 <= duration <= 864000000:
            raise ValueError
        resource["duration"] = duration
    if len(parsed.resources) < MAX_RESOURCES:
        parsed.resources.append(resource)
    else:
        parsed.gap("content_truncated")
    parsed.gap("resource_not_downloaded")
    return f"[{RESOURCE_LABELS[kind]}：仅保留资源元数据，未下载、未解析正文]"


def _markdown(text: str, parsed: ParsedContent) -> str:
    # Markdown 原文只作为文本交付；图片无论是飞书 key 还是外链都不冒充已读内容。
    def image(match: re.Match[str]) -> str:
        target = match.group(1)
        if re.fullmatch(r"img_[A-Za-z0-9_.-]+", target):
            return _resource("image", {"image_key": target}, parsed)
        parsed.gap("resource_not_downloaded")
        return "[外链图片：未下载、未解析正文]"

    result = re.sub(r"!\[[^\]\n]*\]\(([^)\n]+)\)", image, text)
    if "![" in result or re.search(r"<(?:img|audio|video)\b", result, re.IGNORECASE):
        # 引用式图片或 HTML 媒体不由当前小解析器猜测定位，仍需显式承认正文缺口。
        parsed.gap("resource_not_downloaded")
        result += "\n[存在未解析的媒体引用，未下载资源]"
    return result


def _node(node: Json, parsed: ParsedContent, *, card: bool, depth: int = 0) -> str:
    tag = node.get("tag")
    if tag in {"text", "md", "code_block"}:
        text = _string(node.get("text"))
        if node.get("un_escape") is True:
            text = html.unescape(text)
        return _markdown(text, parsed) if tag == "md" else text
    if tag == "a":
        href = _string(node.get("href"), 2048)
        if not href:
            raise ValueError
        return f"{_string(node.get('text'))} ({href})"
    if tag == "at":
        identity = _string(node.get("user_id"), 128)
        if not identity:
            raise ValueError
        name = _string(node.get("user_name", ""), 256)
        return f"@{name} ({identity})" if name else identity
    if tag == "hr":
        return "\n---\n"
    if tag == "emotion":
        return f"[表情：{_string(node.get('emoji_type'), 128)}]"
    if tag in {"img", "media"}:
        return _resource("image" if tag == "img" else "media", node, parsed)
    if card and tag == "button":
        # 按钮标签可读，但未执行的点击行为不是已经取得的上下文事实。
        parsed.gap("card_interaction_not_executed")
        return f"[按钮：{_string(node.get('text'), 512)}，未执行]"
    if card and tag == "note" and depth == 0:
        elements = node.get("elements")
        if not isinstance(elements, list) or len(elements) > MAX_NODES:
            raise ValueError
        return "".join(_safe_node(item, parsed, card=True, depth=1) for item in elements)
    parsed.gap("unknown_card_node" if card else "unknown_post_node")
    return "[未解析的卡片节点]" if card else "[未解析的富文本节点]"


def _safe_node(node: object, parsed: ParsedContent, *, card: bool, depth: int = 0) -> str:
    try:
        if not isinstance(node, dict):
            raise ValueError
        return _node(cast(Json, node), parsed, card=card, depth=depth)
    except (ValueError, TypeError):
        parsed.gap("malformed_content")
        return "[未解析的消息节点]"


def _rich(content: Json, parsed: ParsedContent, *, card: bool) -> str:
    title = _string(content.get("title", ""))
    rows = content.get("elements") if card else content.get("content_v2", content.get("content"))
    if not isinstance(rows, list):
        raise ValueError
    output = [title] if title else []
    count = 0
    for row in rows[:MAX_NODES]:
        if not isinstance(row, list):
            parsed.gap("malformed_content")
            output.append("[未解析的消息段落]")
            continue
        remaining = max(0, MAX_NODES - count)
        output.append("".join(_safe_node(node, parsed, card=card) for node in row[:remaining]))
        count += len(row)
        if count > MAX_NODES:
            parsed.gap("content_truncated")
            break
    if len(rows) > MAX_NODES:
        parsed.gap("content_truncated")
    return "\n".join(output)


def _system(content: Json, parsed: ParsedContent) -> str:
    template = _string(content.get("template"))
    if not template:
        raise ValueError

    def variable(match: re.Match[str]) -> str:
        key = match.group(1)
        value = content.get(key)
        if key in {"from_user", "to_chatters"}:
            if isinstance(value, list) and len(value) <= MAX_NODES:
                try:
                    return "、 ".join(_string(item, 256) for item in value)
                except ValueError:
                    pass
        elif key == "divider_text" and isinstance(value, dict):
            # 官方接收结构中的语言键为小写；默认值必须存在，不能靠字段名猜测文案。
            default = _string(value.get("text"), 512)
            translations = value.get("i18n_text")
            if isinstance(translations, dict) and isinstance(translations.get("zh_cn"), str):
                return _string(translations["zh_cn"], 512)
            return default
        parsed.gap("unknown_system_variable")
        return "[未解析的系统消息变量]"

    if re.search(r"[{}]", re.sub(r"\{[^{}]+\}", "", template)):
        parsed.gap("unknown_system_variable")
    return re.sub(r"\{([^{}]+)\}", variable, template)


def parse_content(kind: str, raw: object, *, deleted: bool = False) -> ParsedContent:
    """保留可确认的事实；错误结构返回类型准确的缺口，不影响同页其他消息。"""
    if deleted:
        return ParsedContent("[消息已撤回]", "recalled", ["message_recalled"])
    parsed = ParsedContent()
    try:
        if not isinstance(raw, str) or len(raw) > 200000:
            raise ValueError
        content = json.loads(raw)
        if not isinstance(content, dict):
            raise ValueError
        if kind == "text":
            if not isinstance(content.get("text"), str):
                raise ValueError
            parsed.text = content["text"]
        elif kind in {"post", "interactive"}:
            parsed.text = _rich(content, parsed, card=kind == "interactive")
        elif kind == "system":
            parsed.text = _system(content, parsed)
        elif kind in RESOURCE_LABELS:
            parsed.text = _resource(kind, content, parsed)
        else:
            return ParsedContent("[尚不支持解析此类型消息]", "unsupported", ["unsupported_message_type"])
    except (ValueError, TypeError, RecursionError):
        label = {"system": "系统消息", "interactive": "卡片消息", "post": "富文本消息"}.get(
            kind,
            RESOURCE_LABELS.get(kind, "消息内容"),
        )
        return ParsedContent(f"[{label}结构未解析]", "unsupported", ["malformed_content"])
    if len(parsed.text) > MAX_TEXT:
        parsed.text = parsed.text[: MAX_TEXT - 12] + "\n[正文已截断]"
        parsed.gap("content_truncated")
    return parsed
