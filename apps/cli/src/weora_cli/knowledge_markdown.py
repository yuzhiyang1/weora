"""读取知识 Markdown 元数据，保留工具规则中的适用路径。"""

import json
import re
from datetime import UTC, date, datetime, time

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

_GLOB_SCALAR = re.compile(r"^(\s*(?:-\s+|(?:paths|globs|applyTo):[ \t]+))([*&].*)$")


def parse_metadata(block: str, *, rule: bool) -> object:
    try:
        return YAML(typ="safe").load(block)
    except YAMLError:
        if not rule:
            raise
    # 工具常省略通配符字符串的引号，例如 paths: **/*.py。
    lines = []
    for line in block.splitlines():
        glob = _GLOB_SCALAR.fullmatch(line)
        value = re.split(r"[ \t]+#", glob[2], maxsplit=1)[0].rstrip() if glob else ""
        lines.append(glob[1] + json.dumps(value, ensure_ascii=False) if glob else line)
    return YAML(typ="safe").load("\n".join(lines))


def markdown_date(value: object) -> str | None:
    """兼容 YAML 的日期对象和引号字符串，保留到 UTC 时间的精度。"""
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, time.min, tzinfo=UTC)
        elif isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[Tt ])", text := value.strip()):
            parsed = datetime.fromisoformat(text)
        else:
            return None
        return (parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)).isoformat()
    except (OverflowError, ValueError):
        return None


def markdown_domain(value: object) -> str | None:
    return value if isinstance(value, str) and value in {"technical", "ops", "support", "neutral"} else None


def rule_paths(metadata: object) -> list[str] | str | None:
    if not isinstance(metadata, dict):
        return None
    for key in ("paths", "globs", "applyTo"):
        value = metadata.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return [str(item) for item in value]
    return None


def imported_metadata(text: str) -> object:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for end, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            block = "\n".join(lines[1:end])
            if len(block) <= 8192:
                try:
                    return parse_metadata(block, rule=True)
                except (YAMLError, RecursionError, OverflowError, ValueError):
                    return None
            break
    return None


def imported_rule_paths(text: str) -> list[str] | str | None:
    return rule_paths(imported_metadata(text))
