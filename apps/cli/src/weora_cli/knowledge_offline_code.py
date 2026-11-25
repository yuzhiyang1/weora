"""在本机 teamwiki 写入、查询及校验确定性的代码证据。"""

import hashlib
import json
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast
from urllib.parse import quote, urlsplit

from weora_code_graph.coverage import GAP_EVIDENCE, GAP_PAGE, detect_gaps, render_gaps
from weora_code_graph.cross_repository import CodeOrigin, cross_repository_edges
from weora_code_graph.design import (
    ARCHITECTURE_DOCUMENT,
    COMPONENT_PROFILE_MANIFEST,
    DOMAIN_DOCUMENT,
    REPOSITORY_OVERVIEW_DOCUMENT,
    SCENARIOS_DOCUMENT,
    components_for,
    escape_domain_text,
    parse_component_profile,
    read_code_domain,
)
from weora_code_graph.extractor import (
    EXTRACTOR_VERSION,
    FileFacts,
    dependencies,
    extract,
    go_code_index,
    relation_targets,
    resolved_calls,
    resolved_implementations,
    swift_code_index,
)
from weora_code_graph.iwiki_links import CodeEvidence
from weora_code_graph.javascript_index import JavaScriptIndex
from weora_code_graph.reconcile import Page, documented_update, reconcile

from weora_cli.collab_client import CollabError, as_object, lock_file
from weora_cli.commands import JsonValue

_MANIFEST = "source-manifest.json"
_GRAPH = ".indices/graph-index.json"
_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,129}")
_HEX = re.compile(r"[0-9a-f]{64}")
_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "git.woa.com": "tgit", "gitcode.com": "gitcode"}
_RESERVED = {"con", "prn", "aux", "nul", *(f"{name}{n}" for name in ("com", "lpt") for n in range(1, 10))}
_NAVIGATION = {
    "index.md": "# 代码知识库\n\n[仓库目录](evidence/code/index.md)\n",
    "router.md": "# 知识导航\n\n从[索引](index.md)选择代码来源，再按文件定位声明与关系。\n",
    "hot.md": "# 常用入口\n\n[代码知识索引](index.md)\n",
}


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _entry(root: Path, relative: str) -> Path:
    current = root
    # 输出根目录由调用者选定。内部条目必须是普通目录或文件，禁止沿链接读写。
    for part in (".", *Path(relative).parts):
        current /= part
        try:
            mode = current.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(mode.st_mode)
            or getattr(mode, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise CollabError("KNOWLEDGE_OFFLINE_PATH", f"离线知识目录包含重定向条目（{relative}）。")
    return current


def _read(root: Path, path: str) -> bytes | None:
    try:
        return _entry(root, path).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None


def _write(root: Path, path: str, content: bytes) -> None:
    target = _entry(root, path)
    if _read(root, path) == content:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        staged = Path(handle.name)
        try:
            handle.write(content)
            handle.close()
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)


def _object(data: bytes | None) -> dict[str, JsonValue]:
    try:
        value = json.loads(data) if data is not None else None
    except (ValueError, UnicodeError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _artifact(slug: str, revision: str) -> str:
    return f"evidence/code/{slug}/snapshots/{revision}"


def _valid_slug(value: str) -> bool:
    return bool(
        _SLUG.fullmatch(value) and not value.endswith(".")
        and value != "index.md" and value.split(".")[0] not in _RESERVED
    )


def _safe_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("._-")[:95].rstrip(".") or "codebase"
    return slug if _valid_slug(slug) else "repo-" + slug


def repository_identity(address: str) -> tuple[str, str]:
    url = urlsplit(address)
    host = url.hostname or ""
    remote_path = url.path.strip("/").removesuffix(".git")
    canonical = remote_path.lower() if host == "github.com" else remote_path
    port = url.port
    standard = 443 if url.scheme == "https" else 2222 if host == "gitcode.com" else 22
    identity = f"{host}{f':{port}' if port not in {None, standard} else ''}/{canonical}"
    slug = "__".join((_HOSTS.get(host, host), *canonical.split("/")))
    return identity, _safe_slug(slug)


def _source(snapshot: dict[str, JsonValue]) -> tuple[str, dict[str, JsonValue]]:
    key, name = str(snapshot["source_key"]), str(snapshot["name"])
    provenance: dict[str, JsonValue] = {"localRootHash": key}
    checkout = snapshot.get("checkout")
    if not isinstance(checkout, dict):
        return _safe_slug(name), provenance
    address = str(checkout["origin"])
    repo_identity, repo_slug = repository_identity(address)
    subdir = str(checkout["subdir"])
    provenance.update(
        repoUrl=address, repoIdentity=repo_identity, sourceSubdir=subdir,
        headSha=checkout["commit"], dirty=checkout["dirty"],
    )
    for field in ("branch", "verifiedRemote"):
        if field in checkout:
            provenance[field] = checkout[field]
    return (_safe_slug(name) if subdir else repo_slug), provenance


def _baseline(root: Path, slug: str) -> dict[str, JsonValue]:
    return _object(_read(root, f"evidence/code/{slug}/{_MANIFEST}"))


def _choose(root: Path, slug: str, source: dict[str, JsonValue]) -> tuple[str, dict[str, JsonValue]]:
    key = str(source["localRootHash"])
    subdir = source.get("sourceSubdir", "")
    identity = source.get("repoIdentity")
    free = None
    for candidate in (slug, f"{slug}-{key[:8]}", f"{slug[:65]}-{key}"):
        baseline = _baseline(root, candidate)
        owner = baseline or _object(_read(root, f"evidence/code/{candidate}/.source.json"))
        previous = owner.get("repoIdentity")
        if subdir == owner.get("sourceSubdir", "") and (
            (identity and identity == previous) or (previous is None and owner.get("localRootHash") == key)
        ):
            return candidate, baseline
        if free is None and not _entry(root, f"evidence/code/{candidate}").exists():
            free = candidate
    if free is not None:
        return free, {}
    raise CollabError("KNOWLEDGE_OFFLINE_SOURCE_CONFLICT", f"可用目录都被其他代码来源占用（{slug}）。")


def remote_baseline(
    output: Path, address: str, key: str, sha: str, branch: str | None, limit: int,
) -> dict[str, JsonValue] | None:
    """只跳过已由平台外的固定 Git 对象验证、且离线产物完整的相同输入。"""
    root = output.resolve() / "teamwiki"
    identity, slug = repository_identity(address)
    project, manifest = _choose(root, slug, {"repoIdentity": identity, "localRootHash": key})
    expected = manifest.get("artifacts")
    if (
        manifest.get("verifiedRemote") is not True or manifest.get("dirty") is not False
        or manifest.get("headSha") != sha or manifest.get("branch") != branch
        or manifest.get("maxFiles") != limit or manifest.get("version") != EXTRACTOR_VERSION
        or not isinstance(expected, dict)
        or _read(root, f"evidence/code/{project}/index.md") is None
    ):
        return None
    files = manifest.get("files")
    if not isinstance(files, list):
        return None
    required = {"overview.md", "graph-index.json", "facts.json", "_manifest.json", GAP_PAGE, GAP_EVIDENCE}
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(path := entry.get("relativePath"), str):
            return None
        required.add(f"files/{_digest(path.encode())}.md")
    if any(_load_verified(root, project, manifest, name) is None for name in required):
        return None
    return _status(manifest) | {
        "output": str(root), "reused": len(files), "extracted": 0, "removed": 0, "unchanged": True,
    }


def _load_verified(
    root: Path, slug: str, baseline: dict[str, JsonValue], name: str,
) -> bytes | None:
    revision = baseline.get("revision")
    files = baseline.get("artifacts")
    if not isinstance(revision, str) or not _HEX.fullmatch(revision) or not isinstance(files, dict):
        return None
    expected = files.get(name)
    if not isinstance(expected, str):
        return None
    content = _read(root, f"{_artifact(slug, revision)}/{name}")
    return content if content is not None and _digest(content) == expected else None


def _facts(
    snapshot: dict[str, JsonValue], cache: dict[str, JsonValue],
) -> tuple[dict[str, FileFacts], int]:
    facts: dict[str, FileFacts] = {}
    reused = 0
    for value in cast(list[JsonValue], snapshot["files"]):
        item = as_object(value)
        path, text = str(item["path"]), str(item["text"])
        oid = _digest(text.encode())
        saved = cache.get(path)
        if isinstance(saved, dict) and saved.get("oid") == oid:
            facts[path] = cast(FileFacts, saved)
            reused += 1
        else:
            facts[path] = extract(path, oid, text)
    return facts, reused


def _evidence(slug: str, facts: dict[str, FileFacts], candidates: int) -> dict[str, bytes]:
    sources = set(facts)
    go = go_code_index(facts, sources)
    swift = swift_code_index(facts, sources)
    javascript = JavaScriptIndex(facts, sources)
    nodes = [{"id": slug, "kind": "repository", "path": ""}]
    edges = []
    overview = [f"# {slug}", "", "## 源码证据", ""]
    rendered = {}
    connected: dict[str, list[str]] = {}
    implementations: set[tuple[str, str]] = set()
    for path, fact in sorted(facts.items()):
        identifier = f"{slug}/{path}"
        # Markdown 文件名独立于源码路径，保留大小写、空格及多层目录时不会覆盖其他证据。
        destination = f"files/{_digest(path.encode())}.md"
        overview.append(f"- [`{path}`]({destination})")
        nodes.append({"id": identifier, "kind": "file", "path": path})
        edges.append({"source": slug, "target": identifier, "kind": "CONTAINS"})
        declarations = [f"- {item['kind']} `{item['name']}`（第 {item['line']} 行）" for item in fact["symbols"]]
        calls = resolved_calls(path, facts, sources, go=go, swift=swift, javascript=javascript)
        implemented = resolved_implementations(path, facts, sources, javascript=javascript)
        imported_files = dependencies(path, fact, sources, go=go, javascript=javascript)
        relations = relation_targets(imported_files, calls, implemented)
        connected[path] = [target for related in relations.values() for target in related]
        implementations.update((item["target"], item["interface"]) for item in implemented)
        lines = [f"# `{path}`", "", fact["language"], "", "## 声明", "", *declarations, "", "## 关系", ""]
        for kind, targets in relations.items():
            for target in targets:
                edges.append({"source": identifier, "target": f"{slug}/{target}", "kind": kind})
                lines.append(f"- {kind} [`{target}`]({_digest(target.encode())}.md)")
        for call in calls:
            if confidence := call.get("confidence"):
                lines.append(f"- 调用 `{call['callee']}()` → `{call['target']}`（第 {call['line']} 行，{confidence}）")
            else:
                lines.append(f"- 调用 `{call['symbol']}()`（第 {call['line']} 行）")
        lines.extend(f"- 实现 `{item['interface']}`（第 {item['line']} 行）" for item in implemented)
        rendered[destination] = ("\n".join(lines) + "\n").encode()
    findings = detect_gaps(facts, connected, implementations, javascript, candidates)
    return rendered | {
        "overview.md": ("\n".join(overview) + "\n").encode(),
        GAP_PAGE: render_gaps(findings).encode(),
        GAP_EVIDENCE: _json(findings),
        "graph-index.json": _json({"nodes": nodes, "edges": edges}),
        "facts.json": _json(facts),
        "_manifest.json": _json({"project": slug, "components": [item.manifest() for item in components_for(facts)]}),
    }


def _projects(root: Path) -> list[str]:
    try:
        return sorted(
            path.name for path in _entry(root, "evidence/code").iterdir()
            if _valid_slug(path.name) and _entry(root, f"evidence/code/{path.name}").is_dir()
        )
    except FileNotFoundError:
        return []


def _wiki_graph(
    root: Path, projects: list[str], *, preserve_mappings: bool = True, preserve_iwiki: bool = True,
) -> dict[str, list[JsonValue]]:
    graph: dict[str, list[JsonValue]] = {"nodes": [], "edges": []}
    origins: list[CodeOrigin] = []
    for project in projects:
        baseline = _baseline(root, project)
        scope = _object(_load_verified(root, project, baseline, "graph-index.json"))
        for key in graph:
            values = scope.get(key)
            if isinstance(values, list):
                graph[key].extend(values)
        if scope:
            facts = cast(dict[str, FileFacts], _object(_load_verified(root, project, baseline, "facts.json")))
            origins.append(CodeOrigin(
                project, str(baseline.get("repoIdentity") or project), str(baseline["revision"]), facts,
            ))
    for edge in cross_repository_edges(origins):
        graph["edges"].append({
            "source": f"{edge.source.origin}/{edge.source.path}", "target": f"{edge.target.origin}/{edge.target.path}",
            "kind": "DEPENDS_ON", "derived": "cross_repository", "method": edge.method, "evidence": edge.evidence,
            "source_revision": edge.source.revision, "target_revision": edge.target.revision,
        })
    if preserve_mappings:
        _preserve_product_mappings(root, graph, projects)
    if preserve_iwiki:
        _merge_iwiki_mappings(root, graph, projects)
    return graph


def _markdown_pages(root: Path, directory: str, prefix: str) -> Iterator[Page]:
    folder = _entry(root, directory)
    if not folder.is_dir():
        return
    for child in sorted(folder.iterdir()):
        path = child.relative_to(root).as_posix()
        checked = _entry(root, path)
        if checked.is_dir():
            yield from _markdown_pages(root, path, prefix)
        elif checked.suffix == ".md" and checked.is_file():
            page = _markdown_page(root, path, prefix + path, path)
            if page:
                yield page


def _markdown_page(
    root: Path, path: str, identifier: str, ref: str, verified: bytes | None = None,
) -> Page | None:
    if verified is None:
        try:
            with _entry(root, path).open("rb") as handle:
                verified = handle.read(512_001)
        except (FileNotFoundError, NotADirectoryError):
            return None
    if len(verified) > 512_000:
        return None
    try:
        body = verified.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if not body.strip() or "\x00" in body:
        return None
    title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), Path(ref).stem)
    return Page(identifier, title, body, ref, _digest(verified), updated=documented_update(body))


def _offline_code_pages(root: Path, projects: list[str]) -> list[Page]:
    pages = [
        replace(page, ref=f"teamwiki/{page.ref}") for page in _markdown_pages(root, "docs", "wiki:")
    ]
    for project in projects:
        baseline = _baseline(root, project)
        revision = baseline.get("revision")
        if not isinstance(revision, str) or not _HEX.fullmatch(revision):
            continue
        prefix = _artifact(project, revision)
        files = baseline.get("files")
        if isinstance(files, list):
            for row in files:
                if not isinstance(row, dict) or not isinstance(path := row.get("relativePath"), str):
                    continue
                name = f"files/{_digest(path.encode())}.md"
                content = _load_verified(root, project, baseline, name)
                evidence = f"{prefix}/{name}"
                page = _markdown_page(root, evidence, f"{project}/{path}", f"teamwiki/{evidence}", content) \
                    if content else None
                if page:
                    pages.append(page)
        # 深度生成的文档位于对应的快照目录，图谱引用同时保存正文摘要。
        manifest = _object(_load_verified(root, project, baseline, "_manifest.json"))
        components = manifest.get("components")
        names = {ARCHITECTURE_DOCUMENT, SCENARIOS_DOCUMENT}
        if isinstance(components, list):
            names.update(
                f"{slug}.md" for item in components
                if isinstance(item, dict) and isinstance(slug := item.get("slug"), str) and _valid_slug(slug)
            )
        for name in sorted(names):
            path = f"{prefix}/docs/{name}"
            if _entry(root, path).is_file():
                page = _markdown_page(root, path, f"{project}/design/{name}", f"teamwiki/{path}")
                if page:
                    pages.append(page)
    return sorted(pages, key=lambda page: page.id)


def _add_mapping_nodes(graph: dict[str, list[JsonValue]], pages: dict[str, Page], references: set[str]) -> None:
    existing = {
        identifier for row in graph["nodes"]
        if isinstance(row, dict) and isinstance(identifier := row.get("id"), str)
    }
    for identifier in sorted(references - existing):
        page = pages[identifier]
        kind = "code"
        if identifier.startswith("product:"):
            kind = "product"
        elif identifier.startswith("iwiki/p/"):
            kind = "iwiki"
        graph["nodes"].append({"id": identifier, "kind": kind, "path": page.ref})


def _preserve_product_mappings(root: Path, graph: dict[str, list[JsonValue]], projects: list[str]) -> None:
    existing = _object(_read(root, _GRAPH)).get("edges")
    if not isinstance(existing, list):
        return
    mappings = [
        row for row in existing if isinstance(row, dict)
        and row.get("derived") == "product_reconciliation" and row.get("kind") == "MAPS_TO"
    ]
    if not mappings:
        return
    products = {page.id: page for page in _markdown_pages(root, "product", "product:")}
    code = {page.id: page for page in _offline_code_pages(root, projects)}
    references: set[str] = set()
    for row in mappings:
        source, target = row.get("source"), row.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        left, right = products.get(source), code.get(target)
        # 更新源码、设计或产品正文时，只保留仍有相同两端证据的已发布关系。
        if (
            left and right and left.revision == row.get("source_revision")
            and right.revision == row.get("target_revision")
        ):
            graph["edges"].append(row)
            references.update((source, target))
    _add_mapping_nodes(graph, products | code, references)


def _iwiki_page(root: Path, identity: str, filename: str) -> Page | None:
    if not re.fullmatch(r"iwiki/p/[a-zA-Z0-9_-]{1,128}", identity):
        return None
    # 图谱可能由设备上的其他程序写入，只认可导入命令生成的单层 Markdown 文件名。
    if not re.fullmatch(r"[\w-]{1,48}-[a-f0-9]{32}\.md", filename):
        return None
    return _markdown_page(root.parent, filename, identity, filename)


def _merge_iwiki_mappings(
    root: Path, graph: dict[str, list[JsonValue]], projects: list[str],
    updates: list[tuple[Page, list[str]]] | None = None,
) -> int:
    stored = _object(_read(root, _GRAPH)).get("edges")
    replaced = {page.id for page, _ in updates or []}
    existing = [
        item for item in stored if isinstance(item, dict) and item.get("derived") == "iwiki"
        and item.get("kind") == "MAPS_TO" and isinstance(item.get("source"), str) and item["source"] not in replaced
    ] if isinstance(stored, list) else []
    if not existing and not updates:
        return 0
    code = {page.id: page for page in _offline_code_pages(root, projects)}
    sources: dict[str, Page | None] = {}
    references: dict[str, Page] = {}
    for row in existing:
        source, target, filename = row.get("source"), row.get("target"), row.get("source_file")
        if not isinstance(source, str) or not isinstance(target, str) or not isinstance(filename, str):
            continue
        if source not in sources:
            sources[source] = _iwiki_page(root, source, filename)
        left, right = sources[source], code.get(target)
        if (
            left and right and left.ref == filename and left.revision == row.get("source_revision")
            and right.revision == row.get("target_revision")
        ):
            graph["edges"].append(row)
            references.update({source: left, target: right})

    count = 0
    if updates:
        evidence = CodeEvidence(list(code.values()))
        for page, terms in updates:
            for link in evidence.links(terms):
                graph["edges"].append({
                    "source": page.id, "source_file": page.ref, "target": link.page.id,
                    "kind": "MAPS_TO", "derived": "iwiki", "evidence": link.term, "confidence": float(link.confidence),
                    "source_revision": page.revision, "target_revision": link.page.revision,
                })
                references.update({page.id: page, link.page.id: link.page})
                count += 1
    _add_mapping_nodes(graph, references, set(references))
    return count


def record_iwiki_mappings(output: Path, files: list[tuple[str, str, list[str]]]) -> int:
    """只在已有同目录团队图谱时写入映射，复用代码索引写锁和可验证证据。"""
    root = output.resolve() / "teamwiki"
    if not files or not _entry(root, _GRAPH).is_file():
        return 0
    with offline_code_writer(output):
        updates = [
            (page, terms) for ref, filename, terms in files
            if (page := _iwiki_page(root, f"iwiki/p/{ref.rsplit('/', 1)[-1]}", filename))
        ]
        projects = _projects(root)
        graph = _wiki_graph(root, projects, preserve_iwiki=False)
        count = _merge_iwiki_mappings(root, graph, projects, updates)
        _write(root, _GRAPH, _json(graph))
        return count


def _offline_reconcile(root: Path, *, preview: bool) -> dict[str, JsonValue]:
    projects = _projects(root)
    if not _entry(root, "product").is_dir() and not _entry(root, "docs").is_dir() and not projects:
        raise CollabError("KNOWLEDGE_OFFLINE_MISSING", "找不到可调和的离线知识库。")
    products = list(_markdown_pages(root, "product", "product:"))
    code = _offline_code_pages(root, projects)
    resources = [
        page for directory in ("docs", "rules")
        for page in _markdown_pages(root.parent, directory, "resource:")
    ] if preview else []
    report = reconcile(products, code, resources)
    changed = False
    if not preview:
        graph = _wiki_graph(root, projects, preserve_mappings=False, preserve_iwiki=False)
        references: set[str] = set()
        for mapping in report["mappings"]:
            source, target = mapping["source"], mapping["target"]
            references.update((source["id"], target["id"]))
            graph["edges"].append({
                "source": source["id"], "target": target["id"], "kind": "MAPS_TO",
                "derived": "product_reconciliation", "evidence": mapping["term"], "confidence": mapping["confidence"],
                "source_revision": source["revision"], "target_revision": target["revision"],
            })
        _add_mapping_nodes(graph, {page.id: page for page in (*products, *code)}, references)
        _merge_iwiki_mappings(root, graph, projects)
        content = _json(graph)
        changed = _read(root, _GRAPH) != content
        if changed:
            _write(root, _GRAPH, content)
    return cast(dict[str, JsonValue], {**report, "preview": preview, "changed": changed})


def offline_reconcile(output: Path, *, preview: bool) -> dict[str, JsonValue]:
    if not preview:
        with offline_code_writer(output) as writer:
            return _offline_reconcile(writer.root, preview=False)
    try:
        return _offline_reconcile(output.resolve() / "teamwiki", preview=True)
    except (OSError, RuntimeError) as exc:
        raise CollabError("KNOWLEDGE_OFFLINE_READ", "无法读取本地 teamwiki。") from exc


def _publish(
    root: Path, project: str, manifest: dict[str, JsonValue], evidence: dict[str, bytes],
) -> None:
    revision = str(manifest["revision"])
    location = f"evidence/code/{project}"
    # 先写入内容寻址的完整证据，再原子切换这个来源的基线。中断时旧基线仍可读取。
    for name, content in evidence.items():
        _write(root, f"{_artifact(project, revision)}/{name}", content)
    _write(root, f"{location}/{_MANIFEST}", _json(manifest))
    write_project_index(root, project, revision)


def read_offline_domain(root: Path, project: str, revision: str) -> str | None:
    # 画像包含组件名称，允许比普通正文更大的文件；旧快照由其各自的指纹定位。
    if not _HEX.fullmatch(revision):
        return None
    content = _read(root, f"{_artifact(project, revision)}/{DOMAIN_DOCUMENT}")
    if content is None or len(content) > 2_000_000:
        return None
    try:
        body = content.decode("utf-8").strip()
    except UnicodeError:
        return None
    return body if read_code_domain(body) else None


def write_project_index(root: Path, project: str, revision: str) -> None:
    prefix = f"snapshots/{revision}"
    merged = f"{_artifact(project, revision)}/{REPOSITORY_OVERVIEW_DOCUMENT}"
    overview = REPOSITORY_OVERVIEW_DOCUMENT if _entry(root, merged).is_file() else "overview.md"
    lines = [f"# {project}", "", f"- [源码概览]({prefix}/{overview})"]
    if _read(root, f"{_artifact(project, revision)}/{GAP_PAGE}") is not None:
        lines.append(f"- [知识缺口]({prefix}/{GAP_PAGE})")
    if _read(root, f"{_artifact(project, revision)}/{GAP_EVIDENCE}") is not None:
        lines.append(f"- [缺口源码证据]({prefix}/{GAP_EVIDENCE})")
    if read_offline_domain(root, project, revision) is not None:
        lines.append(f"- [仓库业务领域]({prefix}/{DOMAIN_DOCUMENT})")
    if _read(root, f"{_artifact(project, revision)}/docs/README.md") is not None:
        lines.append(f"- [设计与图谱]({prefix}/docs/README.md)")
    _write(root, f"evidence/code/{project}/index.md", ("\n".join(lines) + "\n").encode())


def _component_routing(root: Path, project: str, revision: str) -> tuple[list[str], list[str]]:
    if not _HEX.fullmatch(revision):
        return [], []
    # 路由只读取活动快照的投影，避免旧模块继续给新版本提供提示。
    manifest = _object(_read(root, f"{_artifact(project, revision)}/{COMPONENT_PROFILE_MANIFEST}"))
    rows = manifest.get("components")
    keywords: list[str] = []
    duties: list[str] = []
    if manifest.get("revision") == revision and isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            if isinstance(slug := row.get("slug"), str) and _valid_slug(slug):
                keywords.append(slug)
            if profile := parse_component_profile(row):
                duties.extend(profile.responsibilities or (profile.summary,))
    return keywords, duties


def write_source_index(root: Path, projects: list[str] | None = None) -> None:
    catalog: list[tuple[str, str, str, str]] = []
    uncategorized = []
    for slug in projects if projects is not None else _projects(root):
        revision = str(_baseline(root, slug).get("revision", ""))
        body = read_offline_domain(root, slug, revision)
        link = f"[{slug}]({quote(slug)}/index.md)"
        profile = read_code_domain(body) if body is not None else None
        slugs, duties = _component_routing(root, slug, revision)
        summary = "；".join(duties[:2])
        if profile is not None:
            keywords = list(dict.fromkeys((*profile.keywords, *slugs)))
            catalog.append((profile.domain, link, profile.description or summary, ", ".join(keywords[:6])))
        elif duties:
            catalog.append(("其他", link, summary, ", ".join(slugs[:6])))
        else:
            uncategorized.append(f"- {link}")
    lines = ["# 代码来源", ""]
    if catalog:
        lines.extend(["## 产品域路由", "", "| 域 | 入口 | 核心职责 | 路由关键词 |", "|---|---|---|---|"])
        lines.extend(
            f"| {escape_domain_text(domain)} | {link} | {escape_domain_text(description[:80])} | "
            f"{escape_domain_text(keywords)} |"
            for domain, link, description, keywords in sorted(catalog)
        )
        if uncategorized:
            lines.extend(["", "## 未归类仓库", ""])
    lines.extend(uncategorized)
    _write(root, "evidence/code/index.md", ("\n".join(lines) + "\n").encode())


def _refresh(root: Path, manifest: dict[str, JsonValue]) -> None:
    projects = _projects(root)
    write_source_index(root, projects)
    gaps = ["# 知识缺口", ""]
    for slug in projects:
        current = _baseline(root, slug)
        if _load_verified(root, slug, current, GAP_PAGE) is not None:
            location = _artifact(slug, str(current["revision"]))
            entry = f"- [{slug}](../{location}/{GAP_PAGE})"
            if _load_verified(root, slug, current, GAP_EVIDENCE) is not None:
                entry += f" · [源码证据](../{location}/{GAP_EVIDENCE})"
            gaps.append(entry)
    _write(root, GAP_PAGE, ("\n".join(gaps) + "\n").encode())
    for name, navigation in _NAVIGATION.items():
        if _read(root, name) is None:
            _write(root, name, navigation.encode())
    _write(root, _GRAPH, _json(_wiki_graph(root, projects)))
    _write(root, _MANIFEST, _json(manifest))


class OfflineCodeWriter:
    """一个导入批次独占输出目录，逐仓库提交证据，最后刷新一次公共图谱。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.latest: dict[str, JsonValue] | None = None

    def publish(self, snapshot: dict[str, JsonValue]) -> dict[str, JsonValue]:
        root = self.root
        initial, source = _source(snapshot)
        project, baseline = _choose(root, initial, source)
        if not snapshot["files"] and not baseline:
            raise CollabError("KNOWLEDGE_LOCAL_EMPTY", "本地目录没有可索引的源码文件。")
        # 先登记来源，以便写到一半中断时原目录可以继续发布。
        _write(root, f"evidence/code/{project}/.source.json", _json(source))
        compatible = snapshot["incremental"] and baseline.get("version") == EXTRACTOR_VERSION
        cache = _object(_load_verified(root, project, baseline, "facts.json")) if compatible else {}
        facts, reused = _facts(snapshot, cache)
        evidence = _evidence(project, facts, int(str(snapshot["candidates"])))
        files: list[JsonValue] = [
            {"relativePath": path, "sha256": fact["oid"], "language": fact["language"]}
            for path, fact in sorted(facts.items())
        ]
        provenance = dict(source)
        if provenance.get("repoUrl"):
            provenance.pop("localRootHash")
        manifest: dict[str, JsonValue] = provenance | {
            "version": EXTRACTOR_VERSION, "project": project, "files": files,
            "candidates": snapshot["candidates"], "maxFiles": snapshot["max_files"],
            "artifacts": {name: _digest(content) for name, content in sorted(evidence.items())},
        }
        revision = _digest(_json(manifest))
        checked = baseline.get("lastScan") if baseline.get("revision") == revision else None
        manifest.update(revision=revision, lastScan=checked or datetime.now(UTC).isoformat())
        _publish(root, project, manifest, evidence)
        self.latest = manifest
        previous = baseline.get("files")
        removed = {
            item.get("relativePath") for item in previous if isinstance(item, dict)
            and isinstance(item.get("relativePath"), str)
        } - facts.keys() if isinstance(previous, list) else set()
        return _status(manifest) | {
            "output": str(root), "reused": reused, "extracted": len(facts) - reused, "removed": len(removed),
        }

    def refresh(self) -> None:
        if self.latest is not None:
            _refresh(self.root, self.latest)
            return
        # 中断可能发生在单库提交之后。恢复时用已持久化的清单重建公共图谱。
        current = [_baseline(self.root, slug) for slug in _projects(self.root)]
        latest = max(
            (row for row in current if row.get("lastScan")), key=lambda row: str(row["lastScan"]), default=None,
        )
        if latest is not None:
            _refresh(self.root, latest)


@contextmanager
def offline_code_writer(output: Path) -> Iterator[OfflineCodeWriter]:
    root = output.resolve() / "teamwiki"
    acquired = False
    try:
        with lock_file(_entry(root, "source-manifest.lock")):
            acquired = True
            yield OfflineCodeWriter(root)
    except OSError as exc:
        if not acquired:
            raise CollabError("KNOWLEDGE_OFFLINE_BUSY", "无法获取 teamwiki 写锁，检查权限或等待当前导入结束。") from exc
        raise CollabError("KNOWLEDGE_OFFLINE_WRITE", "无法写入本地 teamwiki，检查目录权限或稍后重试。") from exc


def export_codebase(output: Path, snapshot: dict[str, JsonValue]) -> dict[str, JsonValue]:
    with offline_code_writer(output) as writer:
        result = writer.publish(snapshot)
        writer.refresh()
        return result


def _status(manifest: dict[str, JsonValue]) -> dict[str, JsonValue]:
    files = manifest.get("files")
    count = len(files) if isinstance(files, list) else 0
    candidates = manifest.get("candidates")
    return {
        "project": manifest.get("project"), "revision": manifest.get("revision"),
        "headSha": manifest.get("headSha"), "repoUrl": manifest.get("repoUrl"),
        "sourceSubdir": manifest.get("sourceSubdir"), "lastScan": manifest.get("lastScan"),
        "branch": manifest.get("branch"),
        "dirty": manifest.get("dirty"), "fileCount": count, "candidates": candidates,
        "truncated": isinstance(candidates, int) and candidates > count,
    }


def _lint(root: Path, projects: list[str]) -> dict[str, JsonValue]:
    issues: list[JsonValue] = []

    def issue(severity: str, category: str, location: str) -> None:
        issues.append({"severity": severity, "category": category, "location": location})

    if not projects:
        issue("high", "evidence-missing", "evidence/code")
    if not _object(_read(root, _MANIFEST)):
        issue("high", "manifest-missing", _MANIFEST)
    for name in _NAVIGATION:
        if _read(root, name) is None:
            issue("low", "nav-missing", name)
    for project in projects:
        manifest = _baseline(root, project)
        expected = manifest.get("artifacts")
        sources = manifest.get("files")
        if not isinstance(expected, dict) or not isinstance(sources, list):
            issue("high", "manifest-missing", project)
            continue
        required = {"overview.md", "graph-index.json", "facts.json"}
        if "_manifest.json" in expected:
            required.add("_manifest.json")
        for item in sources:
            if isinstance(item, dict) and isinstance(path := item.get("relativePath"), str):
                required.add(f"files/{_digest(path.encode())}.md")
        context = f"evidence/code/{project}"
        for name in sorted(required):
            if _load_verified(root, project, manifest, name) is None:
                issue("high", "evidence-invalid", f"{context}/{name}")
        if _read(root, f"{context}/index.md") is None:
            issue("low", "missing-index", context)
        if _status(manifest)["truncated"]:
            issue("medium", "file-limit", context)
    whole = _object(_read(root, _GRAPH))
    expected_graph = _wiki_graph(root, _projects(root))
    if whole != expected_graph:
        issue("high", "graph-invalid", _GRAPH)
    counts = {
        level: sum(1 for item in issues if isinstance(item, dict) and item["severity"] == level)
        for level in ("high", "medium", "low")
    }
    return {"issues": issues, "summary": {**counts, "total": len(issues)}}


def offline_codebase(output: Path, project: str | None, *, lint: bool) -> dict[str, JsonValue]:
    root = output.resolve() / "teamwiki"
    if project and not _valid_slug(project):
        raise CollabError("KNOWLEDGE_OFFLINE_PROJECT", "请输入离线导入返回的代码库名称。")
    try:
        if lint:
            return _lint(root, [project] if project else _projects(root))
        latest = _baseline(root, project) if project else _object(_read(root, _MANIFEST))
        if not latest:
            raise CollabError("KNOWLEDGE_OFFLINE_MISSING", "找不到离线代码库，请先使用 import --output。")
        return _status(latest)
    except (OSError, RuntimeError) as exc:
        raise CollabError("KNOWLEDGE_OFFLINE_READ", "无法读取本地 teamwiki。") from exc
