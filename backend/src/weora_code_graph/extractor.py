"""确定性地从固定 Git 文本提取声明、行号和仓库内依赖。"""

import ast
import posixpath
import re
from bisect import bisect_right
from collections.abc import Sequence
from itertools import islice
from pathlib import PurePosixPath
from typing import NotRequired, TypedDict

from weora_code_graph.config_facts import CONFIG_LANGUAGES, configuration_names
from weora_code_graph.go_facts import GoFacts, go_facts, module_directive
from weora_code_graph.go_index import GoCodeIndex
from weora_code_graph.javascript_calls import JavaScriptCall, JavaScriptForward, javascript_facts
from weora_code_graph.javascript_config import JavaScriptConfig, is_javascript_config, javascript_config
from weora_code_graph.javascript_index import JavaScriptIndex
from weora_code_graph.python_calls import PythonCall, call_facts
from weora_code_graph.swift_calls import SwiftCalls, swift_calls
from weora_code_graph.swift_facts import swift_facts, swift_protocol_locations
from weora_code_graph.swift_index import SwiftCodeIndex
from weora_code_graph.typescript_facts import Implementation, ImportBinding, Interface

MAX_INDEX_FILES = 1000
DEFAULT_INDEX_FILES = 200
EXTRACTOR_VERSION = 11
_LANGUAGE = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".vue": "Vue",
    ".java": "Java",
    ".kt": "Kotlin",
    ".scala": "Scala",
    ".go": "Go",
    ".rs": "Rust",
    ".swift": "Swift",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".hpp": "C++",
    ".sql": "SQL",
    ".toml": "TOML",
    ".ini": "INI",
    ".conf": "CONF",
}
_IGNORED_DIRS = {"node_modules", "vendor", "dist", "build", "target", "coverage", "__pycache__", "generated"}
_KEY_FILE = re.compile(r"(?:^|[._-])(main|index|app|routes?|controller|service|handler|config)(?:[._-]|$)", re.I)
_DECLARATION = re.compile(
    r"^\s*(?:(?:export|default|declare|public|private|static|final|abstract|open|pub|async)\s+)*"
    r"(class|interface|type|struct|enum|trait|record|object|function|fun|func|fn|def)\s+"
    r"(?:\([^)\n]+\)\s+)?([a-zA-Z_$][\w$]{0,119})\b",
    re.MULTILINE,
)
_RELATIVE_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*(?:\(\s*)?|\brequire\s*\(\s*)['"](\.{1,2}/[a-zA-Z0-9_./-]{1,380})['"]"""
)


_SQL_IGNORED = re.compile(
    r"--[^\r\n]*|/\*[\s\S]*?(?:\*/|\Z)|(?P<dollar>\$(?:[a-zA-Z_]\w*)?\$)[\s\S]*?(?P=dollar)|'(?:''|[^'])*'"
)
_SQL_IDENTIFIER = r'(?:[A-Za-z_]\w*|["`][A-Za-z_]\w*["`])'
_SQL_TABLE = re.compile(
    rf"\b(?:create\s+(?:temporary\s+|temp\s+)?table\s+(?:if\s+not\s+exists\s+)?|alter\s+table\s+)"
    rf"(?:{_SQL_IDENTIFIER}\s*\.\s*)?({_SQL_IDENTIFIER})",
    re.I,
)


class Symbol(TypedDict):
    kind: str
    name: str
    line: int


class ImportSite(TypedDict):
    specifier: str
    line: int


class FileFacts(TypedDict):
    oid: str
    language: str
    parser: str
    symbols: list[Symbol]
    imports: list[str]
    external_imports: NotRequired[list[str]]
    import_sites: NotRequired[list[ImportSite]]
    implementations: NotRequired[list[Implementation]]
    bindings: NotRequired[list[ImportBinding]]
    interfaces: NotRequired[list[Interface]]
    python_calls: NotRequired[list[PythonCall]]
    python_callables: NotRequired[list[str]]
    javascript_calls: NotRequired[list[JavaScriptCall]]
    javascript_exports: NotRequired[list[str]]
    javascript_export_names: NotRequired[list[str]]
    javascript_types: NotRequired[list[str]]
    javascript_forwards: NotRequired[list[JavaScriptForward]]
    javascript_config: NotRequired[JavaScriptConfig]
    go: NotRequired[GoFacts]
    go_module: NotRequired[str]
    swift: NotRequired[SwiftCalls]


class ResolvedImplementation(Implementation):
    target: str


class ResolvedCall(PythonCall):
    target: str
    confidence: NotRequired[str]


def language_for(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if len(path) > 480 or any(part.startswith(".") or part.casefold() in _IGNORED_DIRS for part in parts):
        return None
    if parts and parts[-1] == "go.mod":
        return "GoMod"
    if parts and is_javascript_config(parts[-1]):
        return "JSConfig"
    return _LANGUAGE.get(PurePosixPath(path).suffix.lower())


def priority(path: str) -> tuple[int, int, str]:
    name = PurePosixPath(path).name
    if name == "go.mod" or is_javascript_config(name):
        return 0, path.count("/"), path
    return (1 if _KEY_FILE.search(name) else 2), 0, path


def extract(path: str, oid: str, text: str | None) -> FileFacts:
    language = language_for(path)
    assert language is not None
    if text is None:
        return {"oid": oid, "language": language, "parser": "skipped", "symbols": [], "imports": []}
    if language == "JSConfig":
        return {
            "oid": oid, "language": language, "parser": "heuristic", "imports": [], "symbols": [],
            "javascript_config": javascript_config(text),
        }
    if language == "GoMod":
        module, line = module_directive(text)
        return {
            "oid": oid, "language": language, "parser": "heuristic", "imports": [], "go_module": module,
            "symbols": [{"kind": "module", "name": module, "line": line}] if module else [],
        }
    if language == "Go":
        go = go_facts(text)
        return {
            "oid": oid, "language": language, "parser": "ast", "go": go,
            "imports": list(dict.fromkeys(item["path"] for item in go["imports"])),
            "symbols": [
                {"kind": item["kind"], "name": item["name"], "line": item["line"]}
                for item in sorted(go["declarations"], key=lambda item: item["line"])[:200]
            ],
        }
    if language == "Swift":
        declarations, implementations, interfaces = swift_facts(text)
        return {
            "oid": oid, "language": language, "parser": "heuristic", "imports": [],
            "symbols": [{"kind": kind, "name": name, "line": line} for kind, name, line in declarations],
            "implementations": implementations, "interfaces": interfaces, "swift": swift_calls(text),
        }
    if language in CONFIG_LANGUAGES:
        return {
            "oid": oid, "language": language, "parser": "heuristic", "imports": [],
            "symbols": [
                {"name": name, "kind": "config", "line": line}
                for name, line in configuration_names(text, toml=language == "TOML")
            ],
        }
    if language == "Python":
        try:
            parsed = ast.parse(text, filename=path)
        except (SyntaxError, RecursionError):
            pass  # 语法不完整时保留确定性声明，并标明使用了启发式提取。
        else:
            return _python_facts(path, oid, text, parsed)
    if language in {"JavaScript", "TypeScript"}:
        jsx = language == "JavaScript" or PurePosixPath(path).suffix.lower() == ".tsx"
        javascript = javascript_facts(text, jsx=jsx, typescript=language == "TypeScript")
        surface = javascript.exports
        result: FileFacts = {
            "oid": oid, "language": language, "parser": "ast",
            "symbols": [{"kind": kind, "name": name, "line": line} for kind, name, line in javascript.symbols],
            "imports": [spec for spec in javascript.specifiers if spec.startswith(("./", "../"))],
            "external_imports": javascript.specifiers, "javascript_calls": javascript.calls,
            "import_sites": [{"specifier": spec, "line": line} for spec, line in javascript.import_sites],
            "javascript_exports": sorted(name for name in surface.callables if len(name) <= 240)[:500],
            "javascript_export_names": sorted(name for name in surface.names if len(name) <= 120)[:500],
            "javascript_types": sorted(name for name in surface.types if len(name) <= 120)[:500],
            "javascript_forwards": surface.forwards,
        }
        if language == "TypeScript":
            result["implementations"] = javascript.implementations
            result["bindings"] = javascript.bindings
            result["interfaces"] = surface.interfaces[:400]
        return result
    symbols: list[Symbol] = []
    for line, content in enumerate(text.splitlines(), 1):
        match = _DECLARATION.match(content)
        if match is not None:
            symbols.append({"name": match[2], "kind": match[1], "line": line})
            if len(symbols) == 200:
                break
    imported = list(dict.fromkeys(_RELATIVE_IMPORT.findall(text)))[:120]
    facts: FileFacts = {
        "oid": oid,
        "language": language,
        "parser": "heuristic",
        "symbols": symbols,
        "imports": imported,
    }
    if language == "SQL":
        # 用等长空白遮住字面量和注释，保留能定位源文件的行号。
        clean = _SQL_IGNORED.sub(lambda match: re.sub(r"[^\r\n]", " ", match[0]), text)
        locations = [match.start() for match in re.finditer("\n", clean)]
        facts["symbols"] = [
            {"name": item[1].strip('\"`')[:120], "kind": "table", "line": bisect_right(locations, item.start()) + 1}
            for item in islice(_SQL_TABLE.finditer(clean), 200)
        ]
    return facts


def _python_facts(path: str, oid: str, text: str, tree: ast.Module) -> FileFacts:
    symbols: list[Symbol] = []
    imports: list[str] = []
    sites: list[ImportSite] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            symbols.append({"name": node.name[:120], "kind": kind, "line": node.lineno})
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
            sites.extend({"specifier": alias.name, "line": node.lineno} for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            if node.module:
                imports.append(prefix)
            separator = "." if node.module else ""
            qualified = [f"{prefix}{separator}{alias.name}" for alias in node.names if alias.name != "*"]
            imports.extend(qualified)
            sites.extend(
                {"specifier": specifier, "line": node.lineno}
                for specifier in ([prefix] if node.module else qualified)
            )
    symbols.sort(key=lambda symbol: symbol["line"])
    return {
        "oid": oid,
        "language": "Python",
        "parser": "ast",
        "symbols": symbols[:200],
        "imports": [name for name in dict.fromkeys(imports) if len(name) <= 480][:120],
        "import_sites": [site for site in sorted(sites, key=lambda site: site["line"])
                         if len(site["specifier"]) <= 480][:300],
        "python_calls": call_facts(path, text, tree),
        "python_callables": [
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and len(node.name) <= 480
        ][:200],
    }


def _dependency_candidates(path: str, language: str, spec: str) -> set[str]:
    directory = posixpath.dirname(path)
    if language == "Python":
        if spec.startswith("."):
            level = len(spec) - len(spec.lstrip("."))
            parent = posixpath.join(directory, *(".." for _ in range(level - 1)))
            base = posixpath.join(parent, spec[level:].replace(".", "/"))
            roots = [posixpath.normpath(base)]
        else:
            base = spec.replace(".", "/")
            roots = [base, f"src/{base}"]
        candidates = {f"{root}.py" for root in roots if root != "."}
        candidates.update(posixpath.join("" if root == "." else root, "__init__.py") for root in roots)
    elif spec.startswith(("./", "../")):
        base = posixpath.normpath(posixpath.join(directory, spec))
        candidates = {base} | {base + suffix for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue")}
        candidates |= {f"{base}/index{suffix}" for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")}
        stem, extension = posixpath.splitext(base)
        if extension in {".js", ".jsx"}:
            candidates.update((stem + ".ts", stem + ".tsx"))
    else:
        return set()
    return candidates


def _dependency(path: str, language: str, spec: str, available: set[str]) -> str | None:
    if language in {"JavaScript", "TypeScript"} and spec.startswith(("./", "../")):
        exact = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
        if exact in available:
            return exact if exact != path else None
    matches = (_dependency_candidates(path, language, spec) & available) - {path}
    return next(iter(matches)) if len(matches) == 1 else None


def unresolved_imports(
    path: str, facts: FileFacts, available: set[str], *, javascript: JavaScriptIndex | None = None,
) -> list[str]:
    # 仓库内有任意匹配（含多个候选）时，保留本地优先级，避免转向其他仓库猜测。
    if facts["language"] == "Python" and facts["parser"] == "ast":
        offered = facts["imports"]
    else:
        # 老版本的 JS/TS 正则易把注释当导入。重新索引后再使用词法提取得到的引用。
        offered = facts.get("external_imports", [])
    if javascript is not None and javascript.is_script(path):
        return [spec for spec in dict.fromkeys(offered) if not javascript.lookup(path, spec).claimed]
    return [
        spec for spec in dict.fromkeys(offered)
        if not (_dependency_candidates(path, facts["language"], spec) & available)
    ]


def go_code_index(manifest: dict[str, FileFacts], available: set[str]) -> GoCodeIndex:
    return GoCodeIndex(
        {path: manifest[path]["go"] for path in available if "go" in manifest[path]},
        {
            posixpath.dirname(path): manifest[path]["go_module"]
            for path in available if "go_module" in manifest[path]
        },
    )


def swift_code_index(manifest: dict[str, FileFacts], available: set[str]) -> SwiftCodeIndex:
    return SwiftCodeIndex({path: manifest[path]["swift"] for path in available if "swift" in manifest[path]})


def dependencies(
    path: str, facts: FileFacts, available: set[str], *,
    go: GoCodeIndex | None = None, javascript: JavaScriptIndex | None = None,
) -> list[str]:
    """只连接可以解析到当前图谱的文件；导入名有歧义时不猜测目标。"""
    if facts["language"] == "Go":
        return go.dependencies(path) if go is not None and path in go.files else []
    if javascript is not None and javascript.is_script(path):
        return list(dict.fromkeys(
            target for spec in facts.get("external_imports", [])
            if (target := javascript.lookup(path, spec).target) and target != path
        ))
    return list(
        dict.fromkeys(
            target for spec in facts["imports"] if (target := _dependency(path, facts["language"], spec, available))
        )
    )


def relation_targets(
    imports: Sequence[str], calls: Sequence[ResolvedCall], implementations: Sequence[ResolvedImplementation],
) -> dict[str, list[str]]:
    """索引和预览使用相同的边定义，同文件中的多次引用合并为一条边。"""
    return {
        "IMPLEMENTS": list(dict.fromkeys(item["target"] for item in implementations)),
        "REFERENCES": list(dict.fromkeys(item["target"] for item in calls)),
        "DEPENDS_ON": list(dict.fromkeys(imports)),
    }


def resolved_calls(
    path: str, manifest: dict[str, FileFacts], available: set[str], *,
    go: GoCodeIndex | None = None, swift: SwiftCodeIndex | None = None, javascript: JavaScriptIndex | None = None,
) -> list[ResolvedCall]:
    """按静态导入识别跨文件调用，同时记录 JS/TS 能确认到的证据层级。"""
    facts = manifest[path]
    if "go" in facts:
        context = go if go is not None else go_code_index(manifest, available)
        return [
            ResolvedCall(
                module=item["module"], symbol=item["symbol"], callee=item["callee"],
                line=item["line"], target=item["target"], confidence=item["confidence"],
            )
            for item in context.resolved_calls(path)
        ] if path in context.files else []
    if "swift" in facts:
        swift_context = swift if swift is not None else swift_code_index(manifest, available)
        return [
            ResolvedCall(
                module=item["module"], symbol=item["symbol"], callee=item["callee"],
                line=item["line"], target=item["target"], confidence=item["confidence"],
            )
            for item in swift_context.resolved_calls(path)
        ] if path in swift_context.files else []
    resolved: list[ResolvedCall] = []
    for call in facts.get("python_calls", []):
        target = _dependency(path, "Python", call["module"], available)
        if target and call["symbol"] in manifest[target].get("python_callables", []):
            resolved.append({
                "module": call["module"], "symbol": call["symbol"], "callee": call["callee"],
                "line": call["line"], "target": target,
            })
    invocations = facts.get("javascript_calls", [])
    js = (javascript if javascript is not None else JavaScriptIndex(manifest, available)) if invocations else None
    for invocation in invocations:
        assert js is not None
        imported = js.lookup(path, invocation["module"]).target
        if imported is None or imported == path or not js.is_script(imported):
            continue
        actual, forwarded = js.export(imported, invocation["symbol"])
        if actual is None and forwarded:
            continue
        target, symbol = actual or (imported, invocation["symbol"])
        if target == path:
            continue
        declarations = manifest[target].get("javascript_exports", [])
        confidence = "EXTRACTED" if symbol in declarations else "INFERRED"
        resolved.append({
            "module": invocation["module"], "symbol": symbol, "callee": invocation["callee"],
            "line": invocation["line"], "target": target, "confidence": confidence,
        })
    return resolved


def resolved_implementations(
    path: str,
    manifest: dict[str, FileFacts],
    available: set[str], *, javascript: JavaScriptIndex | None = None,
) -> list[ResolvedImplementation]:
    """连接同文件、明确导入或同一 SwiftPM 模块中可见的接口。"""
    facts = manifest[path]
    if facts["language"] == "Swift":
        conformances = facts.get("implementations", [])
        if not conformances:
            return []
        declarations = [
            (other, interface)
            for other in sorted(available) if manifest[other]["language"] == "Swift"
            for interface in manifest[other].get("interfaces", [])
        ]
        targets = swift_protocol_locations(path, declarations, {item["interface"] for item in conformances})
        return [
            {**item, "target": target}
            for item in conformances if (target := targets.get(item["interface"]))
        ]
    implementations = facts.get("implementations", [])
    if not implementations:
        return []
    js = javascript if javascript is not None else JavaScriptIndex(manifest, available)
    local = {item["name"] for item in facts.get("interfaces", [])}
    bindings: dict[str, list[ImportBinding]] = {}
    for binding in facts.get("bindings", []):
        bindings.setdefault(binding["local"], []).append(binding)
    resolved: list[ResolvedImplementation] = []
    for implementation in implementations:
        name = implementation["interface"]
        # 索引比较仍能读取旧清单，却不让全文件的同名声明覆盖新清单的词法解析。
        site: Implementation = {"name": implementation["name"], "interface": name, "line": implementation["line"]}
        if implementation.get("local", name in local):
            resolved.append({**site, "target": path})
            continue
        alias, _, member = name.partition(".")
        candidates = bindings.get(alias, [])
        if len(candidates) != 1:
            continue
        binding = candidates[0]
        bound = binding["exported"]
        if not member and bound == "*":
            continue
        target = js.lookup(path, binding["specifier"]).target
        exported = member if bound == "*" else f"{bound}.{member}" if member else bound
        if target and (actual := js.export(target, exported, interface=True)[0]):
            resolved.append({**site, "target": actual[0]})
    return resolved
