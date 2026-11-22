"""提取 Swift 的协议声明和遵循关系，按 SwiftPM 路径确定可见范围。"""

import re
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterator

from weora_code_graph.typescript_facts import Implementation, Interface

_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,119}")
_TOKEN = re.compile(r'//[^\n]*|/\*|\#+/|\#*"{3}|\#*"|[A-Za-z_][A-Za-z_0-9]*|[^\s]')
_BLOCK = re.compile(r"/\*|\*/")
_REGEX = re.compile(r"/(?:\\[^\n]|\[(?:\\[^\n]|[^\]\\\n])*\]|[^/\[\\\n])+/")
_REGEX_CONTEXT = {"=", "(", ":", "[", ",", "return", "case", "throw"}
_DECLARATIONS = {"actor", "class", "enum", "extension", "func", "protocol", "struct"}
_NOMINAL = {"actor", "class", "enum", "extension", "struct"}
_MODIFIERS = {
    "public", "open", "internal", "package", "private", "fileprivate",
    "final", "indirect", "nonisolated", "distributed", "override",
}
_OPEN = {"<": ">", "(": ")", "[": "]"}


def _skip_block(text: str, start: int) -> int:
    depth = 1
    for marker in _BLOCK.finditer(text, start):
        depth += 1 if marker[0] == "/*" else -1
        if depth == 0:
            return marker.end()
    return len(text)


def _skip_string(text: str, start: int, opener: str, depth: int = 0) -> int:
    delimiter = opener.lstrip("#")
    fence = opener[:len(opener) - len(delimiter)]
    escaped = "\\" + fence
    marker = re.compile(re.escape(delimiter + fence) + "|" + re.escape(escaped) + r"|[\r\n]")
    while match := marker.search(text, start):
        start = match.end()
        if match[0] == delimiter + fence or (delimiter == '"' and match[0] in {"\n", "\r"}):
            return start
        if match[0] != escaped:
            continue
        if text[start:start + 1] == "(":
            if depth >= 32:
                return len(text)
            # 插值可再包含带引号的字符串。整段插值属于字面量，不贡献声明。
            brackets = 1
            for token, position in _tokens(text, start + 1, depth + 1):
                brackets += (token == "(") - (token == ")")
                if brackets == 0:
                    start = position + 1
                    break
            else:
                return len(text)
        else:
            start += 1
    return len(text)


def _tokens(text: str, start: int = 0, depth: int = 0) -> Iterator[tuple[str, int]]:
    previous = ""
    while match := _TOKEN.search(text, start):
        value, start = match[0], match.end()
        if value.startswith("//"):
            continue
        if value == "/*":
            start = _skip_block(text, start)
            continue
        if value.endswith('"'):
            start = _skip_string(text, start, value, depth)
            yield '""', match.start()
        elif value.startswith("#") and value.endswith("/"):
            # Swift 扩展正则字面量使用相同数量的 # 包围。
            end = text.find("/" + value[:-1], start)
            start = len(text) if end < 0 else end + len(value)
            yield "/…/", match.start()
        elif (
            value == "/" and (not previous or previous in _REGEX_CONTEXT)
            and (regex := _REGEX.match(text, match.start()))
        ):
            start = regex.end()
            yield "/…/", match.start()
        else:
            yield value, match.start()
        previous = value


def swift_target(path: str) -> str | None:
    """采用最内层的 Sources/目标 或 Tests/目标，保留仓库内的完整模块路径。"""
    parts = path.split("/")
    for index in range(len(parts) - 3, -1, -1):
        if parts[index] in {"Sources", "Tests"}:
            return "/".join(parts[:index + 2])
    return None


def _protocol_names(values: list[str], start: int) -> list[str]:
    """读取声明的大括号前、where 前的直接协议列表，跳过泛型参数内部的冒号。"""
    stack: list[str] = []
    current: list[str] = []
    conformances: list[str] = []
    inherited = False
    for token in values[start:start + 320]:
        if not stack:
            if token in {"where", "{"}:
                if inherited and current and _NAME.fullmatch(current[0]) and len(current) == 1:
                    conformances.append(current[0])
                return conformances
            if token in _DECLARATIONS or token in {"}", ";", "=", "#"}:
                return []
            if token == ":" and not inherited:
                inherited = True
                continue
            if token in {",", "&"} and inherited:
                if len(current) == 1 and _NAME.fullmatch(current[0]):
                    conformances.append(current[0])
                current = []
                continue
            if inherited and token not in _OPEN:
                current.append(token)
        if token in _OPEN:
            stack.append(_OPEN[token])
        elif stack and token == stack[-1]:
            stack.pop()
    return []


def swift_facts(text: str) -> tuple[list[tuple[str, str, int]], list[Implementation], list[Interface]]:
    tokens = list(_tokens(text))
    values = [token for token, _ in tokens]
    newlines = [match.start() for match in re.finditer("\n", text)]
    symbols: list[tuple[str, str, int]] = []
    implementations: list[Implementation] = []
    interfaces: list[Interface] = []
    depth = 0
    for index, (keyword, position) in enumerate(tokens[:-1]):
        if keyword == "{":
            depth += 1
        elif keyword == "}":
            depth = max(0, depth - 1)
        if keyword not in _DECLARATIONS:
            continue
        name = values[index + 1]
        if not _NAME.fullmatch(name) or name in _DECLARATIONS | {"var", "let", "subscript"}:
            continue
        cursor = index - 1
        modifiers: set[str] = set()
        while cursor >= 0 and values[cursor] in _MODIFIERS:
            modifiers.add(values[cursor])
            cursor -= 1
        if cursor >= 0 and values[cursor] in {"import", "."}:
            continue
        line = bisect_right(newlines, position) + 1
        if keyword == "protocol" and depth == 0:
            exported = "" if modifiers & {"private", "fileprivate"} else name
            interfaces.append({"name": name, "exported": exported, "line": line})
        if keyword != "extension":
            symbols.append(("interface" if keyword == "protocol" else keyword, name, line))
        if keyword in _NOMINAL:
            head = index + 2
            if keyword == "extension":
                while values[head:head + 1] == ["."] and head + 1 < len(values) and _NAME.fullmatch(values[head + 1]):
                    name += "." + values[head + 1]
                    head += 2
            implementations.extend(
                {"name": name, "interface": protocol, "line": line}
                for protocol in dict.fromkeys(_protocol_names(values, head))
            )
    return symbols[:200], implementations[:200], interfaces[:200]


def swift_protocol_locations(
    source: str, interfaces: list[tuple[str, Interface]], names: set[str],
) -> dict[str, str]:
    same_file: dict[str, list[str]] = defaultdict(list)
    same_target: dict[str, list[str]] = defaultdict(list)
    target = swift_target(source)
    for path, interface in interfaces:
        name = interface["name"]
        if name not in names:
            continue
        if path == source:
            same_file[name].append(path)
        elif target and interface["exported"] and swift_target(path) == target:
            same_target[name].append(path)
    resolved: dict[str, str] = {}
    for name in names:
        candidates = same_file.get(name) or same_target.get(name, [])
        if len(candidates) == 1:
            resolved[name] = candidates[0]
    return resolved
