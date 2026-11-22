"""验证固定快照中的路径别名、转发链及发布时的版本变化。"""

from uuid import UUID, uuid5

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.code_preview import relation_delta
from weora_code_graph.cross_repository import CodeOrigin, cross_repository_edges
from weora_code_graph.extractor import dependencies, extract, priority, resolved_calls, resolved_implementations
from weora_code_graph.javascript_index import JavaScriptIndex

pytestmark = pytest.mark.anyio


def test_aliases_use_nearest_config_inheritance_and_ordered_fallbacks() -> None:
    sources = {
        "tsconfig.json": """{
            // 路径配置允许 JSONC。
            "compilerOptions": { "baseUrl": ".", "paths": {
                "@lib/*": ["gone/*", "src/*"],
                "@lib/special/*": ["feature/*"],
                "@lib/special/port": ["exact.ts"],
                "@outside/*": ["../outside/*"],
            }, },
        }""",
        "work/tsconfig.json": '{"extends": ["../config/tsconfig.base", "./tsconfig.project.json"]}',
        "config/tsconfig.base.json": '{"compilerOptions": {"paths": {"@pkg/*": ["lib/*"]}}}',
        "work/tsconfig.project.json": '{"compilerOptions": {"strict": true}}',
        "isolated/tsconfig.json": '{"extends":"./tsconfig.loop"}',
        "isolated/tsconfig.loop.json": '{"extends":"./tsconfig"}',
        "app/page.ts": "import { emit } from '@lib/worker'; emit();",
        "work/page.ts": "import { emit } from '@pkg/worker'; emit();",
        "isolated/page.ts": "import { emit } from '@lib/worker'; emit();",
        "src/worker.ts": "export function emit() {}",
        "src/duplicate.ts": "export function emit() {}",
        "src/duplicate/index.ts": "export function emit() {}",
        "config/lib/worker.ts": "export function emit() {}",
        "feature/extra.ts": "export function emit() {}",
        "exact.ts": "export function emit() {}",
    }
    facts = {path: extract(path, "fixed", body) for path, body in sources.items()}
    context = JavaScriptIndex(facts, set(facts))
    assert priority("tsconfig.json") < priority("work/tsconfig.json") < priority("app/page.ts")
    assert context.lookup("app/page.ts", "@lib/special/port").target == "exact.ts"
    assert context.lookup("app/page.ts", "@lib/special/extra").target == "feature/extra.ts"
    ambiguous = context.lookup("app/page.ts", "@lib/duplicate")
    assert ambiguous.claimed and ambiguous.target is None
    outside = context.lookup("app/page.ts", "@outside/worker")
    assert outside.claimed and outside.target is None
    assert context.lookup("isolated/page.ts", "@lib/worker").target is None
    skipped = facts | {"app/tsconfig.json": extract("app/tsconfig.json", "skipped", None)}
    unresolved = JavaScriptIndex(skipped, set(facts)).lookup("app/page.ts", "@lib/worker")
    assert unresolved.claimed and unresolved.target is None
    assert dependencies("app/page.ts", facts["app/page.ts"], set(facts), javascript=context) == ["src/worker.ts"]
    assert [row["target"] for row in resolved_calls("work/page.ts", facts, set(facts))] == ["config/lib/worker.ts"]
    unrelated = {"worker.ts": extract("worker.ts", "other", "export function emit() {}")}
    assert cross_repository_edges([
        CodeOrigin("app", "app", "rev", facts), CodeOrigin("other", "other", "rev", unrelated),
    ]) == []


def test_forwarded_calls_interfaces_and_namespaces_follow_unique_origins() -> None:
    source = "src/use.ts"
    sources = {
        "tsconfig.json": '{"compilerOptions":{"paths":{"@bundle":["src/barrel.ts"]}}}',
        source: """
import { send as emit, tools, Factory, repeated, collision, hidden } from "@bundle";
import type { Port, contracts } from "@bundle";
emit(); tools.update(); Factory.make(); repeated(); collision(); hidden();
class Service implements Port, contracts.Config {}
""",
        "src/barrel.ts": """
export { send } from "./relay";
export * as tools from "./worker";
export default class Local {}
import Engine from "./factory";
export { Engine as Factory };
export type { PublicPort as Port } from "./contracts";
export type * as contracts from "./contracts";
export type { hidden } from "./worker";
export * from "./stars-a";
export * from "./stars-b";
""",
        "src/relay.ts": "export { dispatch as send } from './worker'; export * from './barrel';",
        "src/stars-a.ts": "export * from './common'; export * from './stars-b'; export * from './conflicting';",
        "src/stars-b.ts": "export * from './common';",
        "src/common.ts": (
            "export function repeated() {}; export function collision() {}; export default function x() {}"
        ),
        "src/conflicting.ts": "export function collision() {}",
        "src/worker.ts": "export function dispatch() {}; export function update() {}; export function hidden() {}",
        "src/factory.ts": "export default class Engine { static make() {} }",
        "src/contracts.ts": "interface Contract {}; export { type Contract as PublicPort }; export interface Config {}",
    }
    before = {path: extract(path, "fixed", body) for path, body in sources.items()}
    assert [
        (call["callee"], call["symbol"], call["target"], call["confidence"])
        for call in resolved_calls(source, before, set(before))
    ] == [
        ("emit", "dispatch", "src/worker.ts", "EXTRACTED"),
        ("tools.update", "update", "src/worker.ts", "EXTRACTED"),
        ("Factory.make", "default.make", "src/factory.ts", "EXTRACTED"),
        ("repeated", "repeated", "src/common.ts", "EXTRACTED"),
    ]
    assert [(row["interface"], row["target"]) for row in resolved_implementations(source, before, set(before))] == [
        ("Port", "src/contracts.ts"), ("contracts.Config", "src/contracts.ts"),
    ]
    after = before | {"src/relay.ts": extract("src/relay.ts", "next", 'export { send } from "./relay";')}
    assert not any(call["callee"] == "emit" for call in resolved_calls(source, after, set(after)))
    context = JavaScriptIndex(after, set(after))
    assert "src/barrel.ts" in dependencies(source, after[source], set(after), javascript=context)
    without_target = {path: facts for path, facts in before.items() if path != "src/worker.ts"}
    changes = relation_delta(before, without_target)["relations"]
    assert isinstance(changes, dict)
    assert {"kind": "REFERENCES", "source": source, "target": "src/worker.ts"} in changes["removed"]


async def test_alias_config_changes_reuse_unchanged_code_and_replace_published_edges(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "TypeScript 模块关系"))["id"])}
    source = "app/task.ts"
    files = {
        source: 'import { send } from "@internal";\nfunction runInvoiceBatch() { send(); }\n',
        "a/barrel.ts": 'export { first as send } from "./impl";',
        "a/impl.ts": "export function first() {}",
        "b/barrel.ts": 'export { second as send } from "./impl";',
        "b/impl.ts": "export function second() {}",
    }

    async def index(path: str) -> tuple[UUID, int]:
        snapshot = files | {"tsconfig.json": '{"compilerOptions":{"paths":{"@internal":["' + path + '"]}}}'}
        response = await runs.client.post(
            f"{root}/codebases/local", params=scope, headers=runs.headers(), json={
                "source_key": "a" * 64, "name": "发票批处理", "candidates": len(snapshot), "incremental": True,
                "files": [{"path": filename, "text": body} for filename, body in snapshot.items()],
            },
        )
        assert response.status_code == 200, response.text
        return UUID(response.json()["codebase_id"]), int(response.json()["reused"])

    codebase, _ = await index("a/barrel.ts")
    entry = str(uuid5(codebase, source))

    async def relations() -> set[tuple[str, str]]:
        response = await runs.client.get(f"{root}/{entry}", params=scope)
        assert response.status_code == 200, response.text
        return {(row["kind"], row["target"]) for row in response.json()["relations"]}

    a = str(uuid5(codebase, "a/impl.ts"))
    b = str(uuid5(codebase, "b/impl.ts"))
    assert await relations() >= {("REFERENCES", a), ("DEPENDS_ON", str(uuid5(codebase, "a/barrel.ts")))}
    unchanged, reused = await index("b/barrel.ts")
    assert unchanged == codebase and reused == len(files)
    assert await relations() >= {("REFERENCES", b), ("DEPENDS_ON", str(uuid5(codebase, "b/barrel.ts")))}
    assert ("REFERENCES", a) not in await relations()
    recall = await runs.client.get(root, params=scope | {"query": "runInvoiceBatch", "recall_mode": True})
    card = next(item for item in recall.json()["items"] if item["id"] == entry)
    assert any(link["target"] == b for link in card["relations"])
