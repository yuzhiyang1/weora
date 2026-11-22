"""在仓库快照内解析 JS/TS 路径别名及静态转发导出。"""

from typing import TYPE_CHECKING

from weora_code_graph.javascript_config import JavaScriptConfig, JavaScriptModules, ModuleLookup

if TYPE_CHECKING:
    from weora_code_graph.extractor import FileFacts

_LANGUAGES = {"JavaScript", "TypeScript"}


class JavaScriptIndex:
    def __init__(self, manifest: dict[str, "FileFacts"], available: set[str]) -> None:
        self.manifest = manifest
        unreadable: JavaScriptConfig = {"valid": False, "extends": []}
        configs = {
            path: facts.get("javascript_config", unreadable)
            for path, facts in manifest.items()
            if facts["language"] == "JSConfig" and (path in available or facts["parser"] == "skipped")
        }
        self.modules = JavaScriptModules(available, configs)
        self._surfaces: dict[tuple[str, str, bool], tuple[tuple[str, str] | None, bool]] = {}

    def lookup(self, path: str, specifier: str) -> ModuleLookup:
        return self.modules.lookup(path, specifier)

    def is_script(self, path: str) -> bool:
        return path in self.manifest and self.manifest[path]["language"] in _LANGUAGES

    def _find(self, start: str, name: str, *, interface: bool) -> tuple[tuple[str, str] | None, bool]:
        pending = [(start, name)]
        visited: set[tuple[str, str]] = set()
        origins: set[tuple[str, str]] = set()
        handled = False
        while pending:
            path, symbol = pending.pop()
            key = path, symbol
            if key in visited:
                continue
            if len(visited) >= 1000:
                return None, True
            visited.add(key)
            if not self.is_script(path):
                return None, True
            facts = self.manifest[path]
            head, dot, tail = symbol.partition(".")
            if interface:
                if any(row["exported"] == symbol for row in facts.get("interfaces", ())):
                    origins.add(key)
                    continue
            elif symbol in facts.get("javascript_exports", ()) or (
                head in facts.get("javascript_export_names", ()) and head not in facts.get("javascript_types", ())
            ):
                origins.add(key)
                continue

            local_types = facts.get("javascript_types", ())
            if head in local_types:
                handled = True
                if interface:
                    continue

            forwards = facts.get("javascript_forwards", [])
            eligible = [row for row in forwards if interface or not row["type_only"]]
            explicit = [row for row in eligible if row["exported"] == head]
            if len(explicit) > 1:
                return None, True
            if head in facts.get("javascript_export_names", ()) and head not in local_types:
                continue
            if not interface and not explicit and any(
                row["type_only"] and row["exported"] in {head, "*"} for row in forwards
            ):
                handled = True
            relays = explicit or ([row for row in eligible if row["exported"] == "*"] if head != "default" else [])
            if not relays:
                continue
            handled = True
            for relay in relays:
                target = self.lookup(path, relay["module"]).target
                if target is None:
                    return None, True
                if relay["exported"] == "*":
                    forwarded = symbol
                elif relay["imported"] == "*":
                    if not tail:
                        continue
                    forwarded = tail
                else:
                    forwarded = relay["imported"] + dot + tail
                pending.append((target, forwarded))
        if len(origins) > 1:
            return None, True
        return next(iter(origins), None), handled

    def export(self, path: str, symbol: str, *, interface: bool = False) -> tuple[tuple[str, str] | None, bool]:
        key = path, symbol, interface
        if key not in self._surfaces:
            self._surfaces[key] = self._find(path, symbol, interface=interface)
        return self._surfaces[key]
