"""验证 JS/TS 调用边绑定到可见模块，且反映目标出口变化。"""

from uuid import UUID, uuid5

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora_code_graph.extractor import extract, resolved_calls

pytestmark = pytest.mark.anyio


def test_javascript_calls_resolve_runtime_imports_and_ignore_shadowed_names() -> None:
    caller = "src/caller.tsx"
    files = {
        caller: """
import DefaultTool, { fetchData as load, type Validator } from "./tools.js";
import * as API from "./tools.js";
import type TypeOnly from "./tools.js";
import { ambiguous } from "./ambiguous";
load<string>();
API.fetchData();
new DefaultTool();
DefaultTool.make();
API.missing();
Validator();
TypeOnly();
ambiguous();
function withParams({load}, API: Service) { load(); API.fetchData(); }
const delayed = load => load();
{ load(); const load = () => false; }
function hoisted() { load(); if (true) { var load; } }
for (const load of callbacks) load();
try {} catch (load) { load(); }
const html = <div>API.fetchData()</div>;
const jsx = <div>{API.fetchData()}</div>;
const template = \x60API.fetchData() \x24{load()}\x60;
const literal = "load()";
const regex = /load()/;
outer.API.fetchData();
""",
        "src/tools.ts": """
function sourceFetch() {}
export {sourceFetch as fetchData};
export default class Tool { static make() {} }
""",
        "src/ambiguous.ts": "export function ambiguous() {}",
        "src/ambiguous/index.ts": "export function ambiguous() {}",
    }
    manifest = {path: extract(path, "object", source) for path, source in files.items()}
    calls = resolved_calls(caller, manifest, set(files))
    evidence = [
        (item["callee"], item["symbol"], item["target"], item.get("confidence"), item["line"])
        for item in calls
    ]
    assert evidence == [
        ("load", "fetchData", "src/tools.ts", "EXTRACTED", 6),
        ("API.fetchData", "fetchData", "src/tools.ts", "EXTRACTED", 7),
        ("DefaultTool", "default", "src/tools.ts", "EXTRACTED", 8),
        ("DefaultTool.make", "default.make", "src/tools.ts", "EXTRACTED", 9),
        ("API.missing", "missing", "src/tools.ts", "INFERRED", 10),
        ("API.fetchData", "fetchData", "src/tools.ts", "EXTRACTED", 21),
        ("load", "fetchData", "src/tools.ts", "EXTRACTED", 22),
    ]


async def test_javascript_calls_publish_pinned_references(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "脚本调用项目"))["id"])}
    isolated = {"project_id": str((await project(runs, "隔离脚本项目"))["id"])}
    caller, target = "app/planner.js", "service/ledger.ts"

    async def publish(files: dict[str, str]) -> tuple[UUID, int]:
        result = await runs.client.post(
            f"{root}/codebases/local", params=scope, headers=runs.headers(), json={
                "source_key": "a" * 64, "name": "调用索引", "candidates": len(files), "incremental": True,
                "files": [{"path": path, "text": content} for path, content in files.items()],
            },
        )
        assert result.status_code == 200, result.text
        return UUID(result.json()["codebase_id"]), int(result.json()["reused"])

    async def detail(page: str) -> dict[str, object]:
        result = await runs.client.get(f"{root}/{page}", params=scope)
        assert result.status_code == 200, result.text
        return result.json()  # type: ignore[no-any-return]

    files = {
        caller: "import { refreshLedger as plan } from '../service/ledger.js';\n"
                "function planLedgerRoute() { plan(); }\n",
        target: "export function refreshLedger() {}",
    }
    source, _ = await publish(files)
    entry, peer = str(uuid5(source, caller)), str(uuid5(source, target))
    original = await detail(entry)
    assert "plan()" in str(original["body"]) and "EXTRACTED" in str(original["body"])
    assert any(
        link["kind"] == "REFERENCES" and link["target"] == peer
        for link in original["relations"]  # type: ignore[union-attr]
    )
    readonly = await runs.client.get(root, params=scope | {"query": "planLedgerRoute", "recall_mode": "true"})
    found = next(row for row in readonly.json()["items"] if row["id"] == entry)
    assert {link["kind"] for link in found["relations"] if link["target"] == peer} >= {"REFERENCES"}
    assert (await runs.client.get(f"{root}/{peer}", params=isolated)).status_code == 404

    _, reused = await publish(files | {target: "export function different() {}"})
    assert reused == 1
    assert "INFERRED" in str((await detail(entry))["body"])
    await publish({caller: files[caller]})
    current = await detail(entry)
    assert "plan()" not in str(current["body"])
    assert (await runs.client.get(f"{root}/{peer}", params=scope)).status_code == 404
