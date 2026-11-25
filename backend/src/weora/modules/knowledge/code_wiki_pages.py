"""从同一版代码事实构建 TeamAI 路径下可引用的代码页面。"""

from collections import defaultdict
from dataclasses import dataclass
from itertools import islice
from uuid import UUID, uuid5

from weora.modules.knowledge.code_wiki_paths_v1 import safe_page_segment
from weora_code_graph.coverage import GAP_PAGE
from weora_code_graph.extractor import FileFacts, Symbol

_CONFIG_LANGUAGES = {"TOML", "INI", "CONF", "JSON", "YAML"}
_TYPES = {"interface", "protocol", "trait"}
_DATA = {"table", "struct", "record"}
_LIMIT = 500


@dataclass(frozen=True, slots=True)
class WikiPage:
    title: str
    body: str
    files: tuple[str, ...]


def wiki_page_id(codebase: UUID, name: str) -> UUID:
    return uuid5(codebase, f"teamwiki:{name}")


def _category(facts: FileFacts, symbol: Symbol) -> str:
    kind, name = symbol["kind"], symbol["name"]
    if facts["language"] in _CONFIG_LANGUAGES or kind == "config":
        return "config"
    if kind in _TYPES:
        return "interface"
    if kind in _DATA:
        return "data"
    if name.endswith(("Error", "Exception")):
        return "error"
    return "component"


def wiki_pages(
    manifest: dict[str, FileFacts], dependencies: dict[str, list[str]],
) -> dict[str, WikiPage]:
    """按事实类别、模块及静态依赖分组，跳过没有证据的报告。"""
    categories: dict[str, list[tuple[str, Symbol]]] = defaultdict(list)
    modules: dict[str, list[tuple[str, Symbol]]] = defaultdict(list)
    pages: dict[str, WikiPage] = {}

    for path in sorted(dependencies):
        entry = manifest[path]
        module = path.split("/", 1)[0] if "/" in path else "_root"
        for symbol in entry["symbols"]:
            categories[_category(entry, symbol)].append((path, symbol))
            modules[module].append((path, symbol))

    for category, facts in sorted(categories.items()):
        pages[f"{category}.md"] = _facts_page(category, facts)
    for module, facts in sorted(modules.items()):
        if len(facts) >= 5:
            pages[f"modules/{safe_page_segment(module)}.md"] = _facts_page(f"{module} 模块", facts)
    if categories["interface"]:
        pages["interfaces.md"] = _facts_page("接口清单", categories["interface"])

    edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for source, targets in sorted(dependencies.items()):
        edges[source.split("/", 1)[0]].extend((source, target) for target in targets)
    for segment, links in edges.items():
        if links:
            links = links[:_LIMIT]
            pages[f"relation-{safe_page_segment(segment)}.md"] = WikiPage(
                f"{segment} 依赖关系",
                "\n".join(f"- {source} → {target}" for source, target in links),
                tuple(dict.fromkeys(source for source, _ in links)),
            )
    if chains := _dependency_paths(dependencies):
        pages["dependency-paths.md"] = WikiPage(
            "静态依赖路径", "\n".join("- " + " → ".join(chain) for chain in chains),
            tuple(dict.fromkeys(path for chain in chains for path in chain)),
        )
    # 标准入口的内容与分类页同一事务发布，可用于理解当前证据覆盖。
    pages["index.md"] = WikiPage(
        "代码证据目录", "\n".join(["# 代码证据目录", "", *(
            f"- [{name}](./{name})" for name in ["overview.md", *sorted(pages), GAP_PAGE]
        )]), (),
    )
    return pages


def _facts_page(title: str, facts: list[tuple[str, Symbol]]) -> WikiPage:
    excerpt = facts[:_LIMIT]
    return WikiPage(
        title, "\n".join([
            f"# {title}", "", f"共 {len(facts)} 项代码事实。", "",
            *(f"- {symbol['kind']} {symbol['name']} ← {path}:{symbol['line']}" for path, symbol in excerpt),
        ]),
        tuple(dict.fromkeys(path for path, _ in excerpt)),
    )


def _dependency_paths(graph: dict[str, list[str]]) -> list[tuple[str, ...]]:
    def walk(chain: tuple[str, ...]) -> list[tuple[str, ...]]:
        steps = [target for target in graph.get(chain[-1], []) if target not in chain][:4] if len(chain) < 5 else []
        return [path for target in steps for path in walk((*chain, target))][:20] if steps else [chain]

    incoming = {target for targets in graph.values() for target in targets}
    entries = [path for path, targets in graph.items() if targets and path not in incoming]
    if not entries:
        entries = [path for path, targets in graph.items() if targets]
    chains = (chain for path in entries for chain in walk((path,)))
    return list(islice((chain for chain in chains if len(chain) > 1), 20))
