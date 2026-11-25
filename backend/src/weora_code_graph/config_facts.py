"""提取可公开的配置结构，索引只保留名称和行号。"""

import re

CONFIG_LANGUAGES = frozenset({"TOML", "INI", "CONF"})

_SECTION = re.compile(
    r"^(?P<array>\[)?\[\s*(?P<name>[A-Za-z_][A-Za-z0-9_. -]{0,119}?)\s*\](?(array)\])\s*(?:[#;].*)?$",
)
_KEY = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]{2,119})\s*=\s*(?![#;])\S")
_TOML_TOKEN = re.compile(r'(?P<multiline>"{3}|\x27{3})|"(?:\\.|[^"\\])*"|\x27[^\x27]*\x27|#.*$')
_CLOSE_BASIC = re.compile(r'\\.|"{3}')



def configuration_names(text: str, *, toml: bool) -> list[tuple[str, int]]:
    matches: list[tuple[str, int]] = []
    quote = ""
    for line, value in enumerate(text.splitlines(), 1):
        content = value.lstrip().lstrip("\ufeff")
        if not quote:
            match = _SECTION.fullmatch(content.rstrip()) or _KEY.match(content)
            if match:
                matches.append((match["name"], line))
                if len(matches) == 200:
                    break
        if not toml:
            continue
        # TOML 跨行字符串内形似章节和键的内容仍属于值。逐行追踪引号，只在值外提取。
        position = 0
        while position < len(content):
            if quote == '"""':
                closing = next((item for item in _CLOSE_BASIC.finditer(content, position) if item[0] == quote), None)
                if closing is None:
                    break
                position, quote = closing.end(), ""
            elif quote:
                end = content.find(quote, position)
                if end < 0:
                    break
                position, quote = end + len(quote), ""
            else:
                token = _TOML_TOKEN.search(content, position)
                if token is None:
                    break
                position, quote = token.end(), token.group("multiline") or ""
    return matches
