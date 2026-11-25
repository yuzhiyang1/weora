"""验证代码索引只连接能够核实的 TypeScript 接口。"""

from uuid import UUID, uuid5

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.code_preview import relation_delta
from weora.modules.knowledge.models import KnowledgeCodebase
from weora_code_graph.extractor import EXTRACTOR_VERSION, extract, resolved_implementations


def test_resolves_aliases_and_skips_ambiguous_or_false_declarations() -> None:
    files = {
        "src/service.ts": """// class Commented implements Port {}
const sample = "it's class Quoted implements Port {}";
const template = `class Template implements Port {}`;
const pattern = /class Pattern implements Port {}/; const count = 10 / 2;
import type { Direct as Port, NotAnInterface, Hidden, Imaginary, Nested } from './ports';
import type * as Contracts from './ports';
import type Primary from './ports';
import type { Alternative } from './ambiguous';
interface Local<T> {}
class Service<T> extends Base<T> implements
    Port, Contracts.Direct, Primary, Local<{ cb: (x: T) => void }>,
    NotAnInterface, Hidden, Imaginary, Nested, Unknown, Alternative {}
""",
        "src/ports.ts": """export interface Direct {}
export default interface PrimaryPort {}
export class NotAnInterface {}
interface Hidden {}
/* export interface Imaginary {} */
export namespace Scope { export interface Nested {} }
""",
        "src/ambiguous.ts": "export interface Alternative {}",
        "src/ambiguous/index.ts": "export interface Alternative {}",
    }
    manifest = {path: extract(path, "oid", text) for path, text in files.items()}
    assert resolved_implementations("src/service.ts", manifest, set(files)) == [
        {"name": "Service", "interface": "Port", "line": 10, "target": "src/ports.ts"},
        {"name": "Service", "interface": "Contracts.Direct", "line": 10, "target": "src/ports.ts"},
        {"name": "Service", "interface": "Primary", "line": 10, "target": "src/ports.ts"},
        {"name": "Service", "interface": "Local", "line": 10, "target": "src/service.ts"},
    ]

def test_typescript_syntax_distinguishes_local_interfaces_type_parameters_and_jsx_text() -> None:
    source = "src/runner.tsx"
    files = {
        "tsconfig.json": '{"compilerOptions":{"paths":{"@api":["src/index.ts"]}}}',
        source: """import type { Port, API } from "@api";
const demo = <pre>class Phantom implements Port {/**/} require("./fake")</pre>;
@component()
export default abstract class Runner<T> extends Parent<T> implements Port, API.Deep.Port<{ extras: T }> {}
const Derived = class Named implements Port {};
function make<Port>() { class Hidden implements Port {} }
function choose() { class LocalRunner implements Inner {} interface Inner {} }
function hide() { type Port = number; class LocalAlias implements Port {} }
namespace Details { interface Port {} export class Nested implements Port {} }
import("./dynamic", {with: {type: "json"}});
require("./worker");
import compat = require("./compat");
""",
        "src/index.ts": 'export type { Contract as Port } from "./contract"; export type * as API from "./api";',
        "src/api.ts": 'export type * as Deep from "./index";',
        "src/contract.ts": "export interface Contract {}",
        "src/fake.ts": "export interface Phantom {}",
        "src/dynamic.ts": "export function boot() {}",
        "src/worker.ts": "export function run() {}",
        "src/compat.ts": "export function compat() {}",
    }
    manifest = {path: extract(path, "fixed", text) for path, text in files.items()}
    assert manifest[source]["parser"] == "ast"
    assert manifest[source]["external_imports"] == ["@api", "./dynamic", "./worker", "./compat"]
    assert [
        (row["name"], row["interface"], row["line"], row["target"])
        for row in resolved_implementations(source, manifest, set(files))
    ] == [
        ("Runner", "Port", 4, "src/contract.ts"),
        ("Runner", "API.Deep.Port", 4, "src/contract.ts"),
        ("Named", "Port", 5, "src/contract.ts"),
        ("LocalRunner", "Inner", 7, source),
        ("Nested", "Port", 9, source),
    ]
    assert all(row["name"] != "Phantom" for row in manifest[source]["symbols"])
    redirected = manifest | {"src/index.ts": extract("src/index.ts", "next", 'export type * from "./api";')}
    changes = relation_delta(manifest, redirected)["relations"]
    assert isinstance(changes, dict)
    assert {"kind": "IMPLEMENTS", "source": source, "target": "src/contract.ts"} in changes["removed"]
    assert {"kind": "DEPENDS_ON", "source": source, "target": "src/index.ts"} not in changes["removed"]


@pytest.mark.anyio
async def test_typescript_ast_reextracts_old_manifest_and_replaces_published_relationships(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "TypeScript 接口解析"))["id"])}
    other = {"project_id": str((await project(runs, "独立项目"))["id"])}
    source, barrel, contract = "src/invoice.tsx", "src/api.ts", "src/contract.ts"
    files = {
        "tsconfig.json": '{"compilerOptions":{"paths":{"@api":["src/api.ts"]}}}',
        source: """import type { Port } from '@api';
const sample = <pre>class Phantom implements Port {/**/}</pre>;
export class InvoiceVerification implements Port {}
""",
        barrel: 'export type { Contract as Port } from "./contract";',
        contract: "export interface Contract {}",
    }

    async def publish(snapshot: dict[str, str]) -> tuple[UUID, int]:
        response = await runs.client.post(
            root + "/codebases/local", params=scope, headers=runs.headers(), json={
                "source_key": "a" * 64, "name": "发票接口", "candidates": len(snapshot), "incremental": True,
                "files": [{"path": path, "text": text} for path, text in snapshot.items()],
            },
        )
        assert response.status_code == 200, response.text
        return UUID(response.json()["codebase_id"]), response.json()["reused"]

    codebase, _ = await publish(files)
    page, endpoint, forwarding = (str(uuid5(codebase, path)) for path in (source, contract, barrel))

    async def relations() -> tuple[set[tuple[str, str]], str]:
        response = await runs.client.get(f"{root}/{page}", params=scope)
        assert response.status_code == 200, response.text
        return {(row["kind"], row["target"]) for row in response.json()["relations"]}, response.json()["body"]

    edges, body = await relations()
    assert edges >= {("IMPLEMENTS", endpoint), ("DEPENDS_ON", forwarding)}
    assert "InvoiceVerification" in body and "Phantom" not in body
    assert (await runs.client.get(f"{root}/{page}", params=other)).status_code == 404

    # 模拟旧提取器记录的同一文件哈希和 JSX 中的误识别，验证采集版本阻止复用。
    async with runs.sessions.begin() as db:
        prior = await db.get(KnowledgeCodebase, codebase)
        assert prior is not None
        prior.extraction_version = EXTRACTOR_VERSION - 1
        prior.manifest = prior.manifest | {source: prior.manifest[source] | {"implementations": [
            {"name": "Phantom", "interface": "Port", "line": 2},
        ]}}
    assert await publish(files) == (codebase, 0)
    async with runs.sessions() as db:
        refreshed = await db.get(KnowledgeCodebase, codebase)
        assert refreshed is not None and refreshed.extraction_version == EXTRACTOR_VERSION
        assert [row["name"] for row in refreshed.manifest[source]["implementations"]] == ["InvoiceVerification"]

    assert await publish(files | {barrel: 'export type { Contract as Renamed } from "./contract";'}) == (
        codebase, len(files) - 1,
    )
    edges, _ = await relations()
    assert ("IMPLEMENTS", endpoint) not in edges and ("DEPENDS_ON", forwarding) in edges
    fresh = await runs.client.get(root, params=scope | {"query": "InvoiceVerification", "recall_mode": "true"})
    card = next(item for item in fresh.json()["items"] if item["id"] == page)
    assert all(link["target"] != endpoint for link in card.get("relations", []))
