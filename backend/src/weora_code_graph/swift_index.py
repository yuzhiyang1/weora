"""将 Swift 调用定位到同一 SwiftPM 目标的可见声明。"""

from collections import defaultdict
from typing import TypedDict

from weora_code_graph.swift_calls import SwiftCalls, SwiftDeclaration
from weora_code_graph.swift_facts import swift_target


class SwiftResolvedCall(TypedDict):
    module: str
    symbol: str
    callee: str
    line: int
    target: str
    confidence: str


class SwiftCodeIndex:
    def __init__(self, files: dict[str, SwiftCalls]) -> None:
        self.files = files
        self.declarations: dict[tuple[str, str], list[tuple[str, SwiftDeclaration]]] = defaultdict(list)
        self.methods: dict[tuple[str, str, str], list[tuple[str, bool]]] = defaultdict(list)
        self.members: dict[str, set[str]] = defaultdict(set)
        for path, facts in sorted(files.items()):
            module = swift_target(path)
            if module is None:
                continue
            self.members[module].update(facts["members"])
            for item in facts["declarations"]:
                if item["visible"]:
                    self.declarations[(module, item["name"])].append((path, item))
            for method in facts["methods"]:
                self.methods[(module, method["owner"], method["name"])].append((path, method["visible"]))

    def resolved_calls(self, path: str) -> list[SwiftResolvedCall]:
        module = swift_target(path)
        if module is None:
            return []
        resolved = []
        for call in self.files[path]["calls"]:
            receiver, symbol = call["receiver"], call["symbol"]
            name = receiver or symbol
            # 成员可能来自同模块的扩展、协议默认实现或父类型。类型内有同名成员时跳过全局推断。
            if call["enclosed_by_type"] and name in self.members[module]:
                continue
            declarations = self.declarations.get((module, name), [])
            if len(declarations) != 1:
                continue
            target, declaration = declarations[0]
            if receiver:
                if declaration["kind"] != "type":
                    continue
                methods = self.methods.get((module, receiver, symbol), [])
                if methods:
                    visible = [file for file, allowed in methods if allowed or file == path]
                    if len(visible) != 1:
                        continue
                    target = visible[0]
            elif declaration["kind"] not in {"type", "function"}:
                continue
            if target != path:
                resolved.append(SwiftResolvedCall(
                    module=module, symbol=symbol, callee=f"{receiver}.{symbol}" if receiver else symbol,
                    line=call["line"], target=target, confidence="INFERRED",
                ))
        return resolved
