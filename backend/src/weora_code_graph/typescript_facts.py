"""代码图谱共享的接口事实和受限模块路径。"""

import re
from typing import NotRequired, TypedDict

_SPECIFIER = re.compile(r"(?:\.{1,2}/)?@?[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*")


class Implementation(TypedDict):
    name: str
    interface: str
    line: int
    # 真正可见的同文件接口由语法树确定。旧提取版本没有这个字段。
    local: NotRequired[bool]


class ImportBinding(TypedDict):
    local: str
    exported: str
    specifier: str


class Interface(TypedDict):
    name: str
    exported: str
    line: int


def _specifier(token: str) -> str | None:
    value = token[1:-1]
    if token[:1] in {"'", '"'} and len(value) <= 380 and _SPECIFIER.fullmatch(value):
        return value
    return None
