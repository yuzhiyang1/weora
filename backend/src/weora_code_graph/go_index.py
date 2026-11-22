"""按固定仓库快照中的 Go 模块、包和声明解析跨文件调用。"""

import posixpath
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TypedDict

from weora_code_graph.go_facts import GoFacts


class GoResolvedCall(TypedDict):
    module: str
    symbol: str
    callee: str
    line: int
    target: str
    confidence: str


@dataclass(frozen=True, slots=True)
class GoPackage:
    directory: str
    name: str


class GoCodeIndex:
    def __init__(self, files: dict[str, GoFacts], modules: dict[str, str]) -> None:
        self.files = files
        self.modules = modules
        self.production: dict[GoPackage, list[str]] = defaultdict(list)
        self.package_names: dict[str, set[str]] = defaultdict(set)
        self.declarations: dict[tuple[GoPackage, str, str], list[str]] = defaultdict(list)
        self.roots: dict[str, str | None] = {}
        for path, facts in sorted(files.items()):
            if not facts["package"]:
                continue
            directory = posixpath.dirname(path)
            package = GoPackage(directory, facts["package"])
            if not path.endswith("_test.go"):
                self.production[package].append(path)
                self.package_names[directory].add(package.name)
            for symbol in facts["declarations"]:
                if symbol["kind"] in {"function", "method"}:
                    key = (package, symbol["receiver"], symbol["name"])
                    self.declarations[key].append(path)

    def _module_root(self, directory: str) -> str | None:
        if directory not in self.roots:
            parent = directory
            while parent not in self.modules and parent:
                parent = posixpath.dirname(parent)
            self.roots[directory] = parent if parent in self.modules else None
        return self.roots[directory]

    def _package(self, directory: str, specifier: str) -> GoPackage | None:
        root = self._module_root(directory)
        if root is None:
            # 无模块的旧工程只沿明确相对路径解析，避免把 fmt 等标准库猜成同名目录。
            if not specifier.startswith(("./", "../")):
                return None
            target = posixpath.normpath(posixpath.join(directory, specifier))
        else:
            module = self.modules[root]
            if not module or not (specifier == module or specifier.startswith(module + "/")):
                return None
            tail = specifier.removeprefix(module).lstrip("/")
            if any(part in {".", ".."} for part in PurePosixPath(tail).parts):
                return None
            target = posixpath.join(root, tail).rstrip("/")
        target = "" if target == "." else target
        if self._module_root(target) != root:
            return None
        names = self.package_names.get(target, set())
        return GoPackage(target, next(iter(names))) if len(names) == 1 and "main" not in names else None

    def dependencies(self, path: str) -> list[str]:
        directory = posixpath.dirname(path)
        targets = []
        for source in self.files[path]["imports"]:
            if package := self._package(directory, source["path"]):
                candidates = self.production[package]
                # 包有多个文件时，调用关系仍可定位具体声明，无需任选整个包的代表文件。
                if len(candidates) == 1 and candidates[0] != path:
                    targets.append(candidates[0])
        return list(dict.fromkeys(targets))

    def resolved_calls(self, path: str) -> list[GoResolvedCall]:
        facts = self.files[path]
        if not facts["package"]:
            return []
        directory = posixpath.dirname(path)
        package = GoPackage(directory, facts["package"])
        imports: dict[str, list[tuple[GoPackage, str]]] = defaultdict(list)
        for source in facts["imports"]:
            if (candidate := self._package(directory, source["path"])) and source["alias"] not in {".", "_"}:
                imports[source["alias"] or candidate.name].append((candidate, source["path"]))
        resolved = []
        for call in facts["calls"]:
            qualifier, receiver, symbol = call["qualifier"], call["receiver_type"], call["symbol"]
            target_package, specifier = package, ""
            test_visible = path.endswith("_test.go")
            if qualifier and not receiver:
                matches = imports.get(qualifier, [])
                if len(matches) != 1 or not symbol[0].isupper():
                    continue
                target_package, specifier = matches[0]
                test_visible = False
            paths = [
                target for target in self.declarations.get((target_package, receiver, symbol), ())
                if test_visible or not target.endswith("_test.go")
            ]
            if len(paths) == 1 and paths[0] != path:
                resolved.append(GoResolvedCall(
                    module=specifier, symbol=symbol, callee=f"{qualifier}.{symbol}" if qualifier else symbol,
                    line=call["line"], target=paths[0], confidence="EXTRACTED",
                ))
        return resolved
