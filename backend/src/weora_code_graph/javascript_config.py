"""读取静态 JS/TS 配置并在已固定的文件清单中定位模块。"""

import json
import posixpath
import re
from dataclasses import dataclass
from typing import NotRequired, TypedDict

_CONFIG_NAME = re.compile(r"(?:tsconfig|jsconfig)(?:\.[A-Za-z0-9_-]+)*\.json")
_STRING = r'"(?:\\[\s\S]|[^"\\])*"'
_COMMENTS = re.compile(rf"{_STRING}|//[^\r\n]*|/\*[\s\S]*?\*/")
_TRAILING = re.compile(rf"{_STRING}|,(?=\s*[}}\]])")
_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue")
_SUBSTITUTIONS = {
    ".js": (".ts", ".tsx"), ".jsx": (".tsx", ".ts"), ".mjs": (".mts",), ".cjs": (".cts",),
}


class JavaScriptConfig(TypedDict):
    valid: bool
    extends: list[str]
    base_url: NotRequired[str]
    paths: NotRequired[dict[str, list[str]]]


def is_javascript_config(name: str) -> bool:
    return _CONFIG_NAME.fullmatch(name) is not None


def _path(value: object, *, wildcard: bool = False) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= 480 and value == value.strip()
        and not value.startswith(("/", "\\"))
        and not any(char in value for char in (":", "\x00", "\n", "\r", "?", "#", "{", "}"))
        and value.count("*") <= int(wildcard)
    )


def javascript_config(text: str) -> JavaScriptConfig:
    clean = _COMMENTS.sub(lambda match: match[0] if match[0].startswith('"') else " ", text.lstrip("\ufeff"))
    clean = _TRAILING.sub(lambda match: match[0] if match[0].startswith('"') else "", clean)
    invalid: JavaScriptConfig = {"valid": False, "extends": []}
    try:
        raw = json.loads(clean)
    except (ValueError, RecursionError):
        return invalid
    if not isinstance(raw, dict):
        return invalid
    compiler = raw.get("compilerOptions", {})
    parents = raw.get("extends", [])
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(compiler, dict) or not isinstance(parents, list) or len(parents) > 16:
        return invalid
    if not all(_path(parent) for parent in parents):
        return invalid
    result: JavaScriptConfig = {"valid": True, "extends": [str(parent) for parent in parents]}
    if "baseUrl" in compiler:
        base = compiler["baseUrl"]
        if not _path(base):
            return invalid
        result["base_url"] = str(base)
    if "paths" in compiler:
        paths = compiler["paths"]
        if not isinstance(paths, dict) or len(paths) > 200:
            return invalid
        entries = {}
        for alias, choices in paths.items():
            if not _path(alias, wildcard=True) or not isinstance(choices, list) or len(choices) > 20:
                return invalid
            if not all(_path(choice, wildcard=True) for choice in choices):
                return invalid
            entries[str(alias)] = [str(choice) for choice in choices]
        result["paths"] = entries
    return result


def _join(directory: str, value: str) -> str | None:
    """路径只在仓库相对清单中求值，不访问外部目录或运行配置。"""
    target = posixpath.normpath(posixpath.join(directory, value.replace("\\", "/")))
    if target.startswith(("/", "../")) or target == "..":
        return None
    return "" if target == "." else target


def _file_candidates(base: str, files: set[str]) -> set[str]:
    if base and base in files:
        return {base}
    stem, extension = posixpath.splitext(base)
    candidates = {base + suffix for suffix in _EXTENSIONS} if base else set()
    candidates.update(posixpath.join(base, f"index{suffix}") for suffix in _EXTENSIONS)
    candidates.update(stem + suffix for suffix in _SUBSTITUTIONS.get(extension, ()))
    return candidates & files


@dataclass(frozen=True, slots=True)
class ModuleLookup:
    target: str | None = None
    claimed: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    base_url: str | None = None
    paths: dict[str, list[str]] | None = None
    paths_directory: str = ""


class JavaScriptModules:
    def __init__(self, files: set[str], configs: dict[str, JavaScriptConfig]) -> None:
        self.files = files
        self.configs = configs
        self._nearest: dict[str, str | None] = {}
        self._configs: dict[str, ResolvedConfig | None] = {}
        self._imports: dict[tuple[str, str], ModuleLookup] = {}

    def _parent_path(self, directory: str, specifier: str) -> str | None:
        if not specifier.startswith(("./", "../")) or (base := _join(directory, specifier)) is None:
            return None
        for path in (base, base + ".json", posixpath.join(base, "tsconfig.json")):
            if path in self.configs:
                return path
        return None

    def _config(self, path: str, seen: frozenset[str] = frozenset()) -> ResolvedConfig | None:
        if path in seen or len(seen) >= 24:
            return None
        if path in self._configs:
            return self._configs[path]
        value = self.configs[path]
        if not value["valid"]:
            return None
        directory = posixpath.dirname(path)
        base: str | None = None
        paths: dict[str, list[str]] | None = None
        paths_directory = directory
        for spec in value["extends"]:
            parent = self._parent_path(directory, spec)
            inherited = self._config(parent, seen | {path}) if parent is not None else None
            if inherited is None:
                return None
            if inherited.base_url is not None:
                base = inherited.base_url
            if inherited.paths is not None:
                paths, paths_directory = inherited.paths, inherited.paths_directory
        if "base_url" in value:
            base = _join(directory, value["base_url"])
            if base is None:
                return None
        if "paths" in value:
            paths, paths_directory = value["paths"], directory
        result = ResolvedConfig(base, paths, paths_directory)
        self._configs[path] = result
        return result

    def _config_path(self, directory: str) -> str | None:
        if directory not in self._nearest:
            current = directory
            while True:
                for name in ("tsconfig.json", "jsconfig.json"):
                    candidate = posixpath.join(current, name)
                    if candidate in self.configs:
                        self._nearest[directory] = candidate
                        return candidate
                if not current:
                    break
                current = posixpath.dirname(current)
            self._nearest[directory] = None
        return self._nearest[directory]

    def _result(self, base: str | None) -> ModuleLookup:
        candidates = _file_candidates(base, self.files) if base is not None else set()
        return ModuleLookup(next(iter(candidates)) if len(candidates) == 1 else None, bool(candidates))

    def _lookup(self, path: str, specifier: str) -> ModuleLookup:
        directory = posixpath.dirname(path)
        if specifier.startswith(("./", "../")):
            return self._result(_join(directory, specifier))
        config_path = self._config_path(directory)
        if config_path is None:
            return ModuleLookup()
        config = self._config(config_path)
        if config is None:
            # 最近的配置存在但无法解释时，不向别的仓库猜测同名导入。
            return ModuleLookup(claimed=True)
        if not config.paths:
            return ModuleLookup()
        match: tuple[int, str, list[str]] | None = None
        if specifier in config.paths:
            match = (len(specifier), "", config.paths[specifier])
        else:
            for pattern, targets in config.paths.items():
                prefix, wildcard, suffix = pattern.partition("*")
                if (
                    not wildcard or not specifier.startswith(prefix) or not specifier.endswith(suffix)
                    or len(specifier) < len(prefix) + len(suffix)
                ):
                    continue
                if match is None or len(prefix) > match[0]:
                    capture = specifier[len(prefix):len(specifier) - len(suffix)]
                    match = (len(prefix), capture, targets)
        if match is None:
            return ModuleLookup()
        _, capture, choices = match
        root = config.base_url if config.base_url is not None else config.paths_directory
        for choice in choices:
            resolved = self._result(_join(root, choice.replace("*", capture)))
            if resolved.claimed:
                return resolved
        return ModuleLookup(claimed=True)

    def lookup(self, path: str, specifier: str) -> ModuleLookup:
        key = (path, specifier)
        if key not in self._imports:
            self._imports[key] = self._lookup(path, specifier)
        return self._imports[key]
