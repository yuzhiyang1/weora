"""验证跨仓库关系在项目检索、打开与健康报告中使用同一份可见证据。"""

from uuid import UUID, uuid5

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora_code_graph.cross_repository import CodeOrigin, cross_repository_edges
from weora_code_graph.extractor import extract

pytestmark = pytest.mark.anyio


def test_cross_repository_keeps_local_resolution_and_repository_identity() -> None:
    caller = {
        "src/app.ts": extract(
            "src/app.ts", "app", """
                import { Connect } from "./shared_service";
                const payment = import("@clients/payment_client");
                const decoy = "import Ghost from '@fake/phantom_client'";
            """,
        ),
        "src/shared_service.ts": extract("src/shared_service.ts", "module-ts", "export class Connect {}"),
        "src/shared_service.js": extract("src/shared_service.js", "module-js", "export class Connect {}"),
    }
    available = {
        "internal/shared.ts": extract("internal/shared.ts", "target-a", "export class SharedService {}"),
        "service/core.ts": extract("service/core.ts", "target-b", "export class PaymentClient {}"),
        "examples/phantom.ts": extract("examples/phantom.ts", "target-c", "export class PhantomClient {}"),
    }
    left = CodeOrigin("source", "canonical/source", "v1", caller)
    edges = cross_repository_edges([left, CodeOrigin("target", "canonical/target", "v2", available)])
    assert [(edge.source.path, edge.target.path, edge.evidence) for edge in edges] == [
        ("src/app.ts", "service/core.ts", "@clients/payment_client"),
    ]
    assert cross_repository_edges([left, CodeOrigin("different-checkout", left.namespace, "v2", available)]) == []


async def test_cross_repository_relations_follow_published_scope_and_unambiguous_objects(runs: RunsHarness) -> None:
    primary = str((await project(runs, "账单知识"))["id"])
    other = str((await project(runs, "隔离项目"))["id"])
    scope = {"project_id": primary}
    knowledge = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"

    async def index(key: str, name: str, files: dict[str, str], project_id: str = primary) -> UUID:
        response = await runs.client.post(
            f"{knowledge}/codebases/local", params={"project_id": project_id}, headers=runs.headers(),
            json={
                "source_key": key * 64, "name": name, "incremental": True, "candidates": len(files),
                "files": [{"path": path, "text": text} for path, text in files.items()],
            },
        )
        assert response.status_code == 200, response.text
        return UUID(response.json()["codebase_id"])

    consumer = await index("a", "账单服务", {
        "src/billing.ts": (
            "import { CorePort } from '@billing/payment_adapter';\n"
            "export class BillingInvoice {}\n"
            "export function invoiceCrossRepo() {}\n"
            "// import { Comment } from '@some/phantom_module';\n"
        ),
    })
    gateway = await index("b", "支付接口", {
        "api/core.ts": "export interface CorePort {}\nexport class PaymentAdapter {}\n",
    })
    data = await index("c", "表定义", {
        "ddl/charges.sql": "/* CREATE TABLE phantom_module(); */\nCREATE TABLE acct.billing_invoice(id int);\n",
    })
    await index("d", "其他项目同名模块", {
        "other/payment_adapter.ts": "export interface PaymentAdapter {}\n",
    }, other)
    await index("e", "未引用组件", {
        "src/ghost.ts": "export class PhantomModule {}\n",
    })
    caller = str(uuid5(consumer, "src/billing.ts"))
    target = str(uuid5(gateway, "api/core.ts"))
    config = str(uuid5(data, "ddl/charges.sql"))

    async def detail(identifier: str) -> dict[str, object]:
        response = await runs.client.get(f"{knowledge}/{identifier}", params=scope)
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    async def edges(identifier: str) -> list[dict[str, str]]:
        response = await detail(identifier)
        links = response["relations"]
        assert isinstance(links, list)
        return [link for link in links if link.get("derived") == "cross_repository"]

    outgoing = await edges(caller)
    assert {(link["target"], link["method"]) for link in outgoing} == {(target, "import"), (config, "configuration")}
    imported = next(link for link in outgoing if link["method"] == "import")
    assert imported["direction"] == "out" and imported["evidence"] == "@billing/payment_adapter"
    assert imported["source_revision"] == (await detail(caller))["source"]["snapshot"]  # type: ignore[index]
    assert imported["target_revision"] == (await detail(target))["source"]["snapshot"]  # type: ignore[index]
    assert {(link["target"], link["direction"]) for link in await edges(target)} == {(caller, "in")}
    recall = (await runs.client.get(knowledge, params=scope | {
        "query": "invoiceCrossRepo", "recall_mode": "true",
    })).json()
    hit = next(row for row in recall["items"] if row["id"] == caller)
    assert any(link["target"] == target and link["derived"] == "cross_repository" for link in hit["relations"])
    edit = await runs.client.get(knowledge, params=scope | {
        "query": "invoiceCrossRepo", "depth": "lookup", "limit": "1",
    })
    plan = edit.json()
    assert edit.status_code == 200 and len(plan["items"]) == 1, edit.text
    assert [item["id"] for item in plan["change_entry_points"]["files"]] == [caller]
    assert {item["id"] for item in plan["candidate_change_files"]} == {target, config}
    assert {(edge["from"]["id"], edge["to"]["id"]) for edge in plan["change_entry_points"]["edges"]} == {
        (caller, target), (caller, config),
    }
    assert (await runs.client.get(f"{knowledge}/{target}", params={"project_id": other})).status_code == 404
    report = (await runs.client.get(f"{knowledge}/health", params=scope)).json()
    assert report["graph"]["cross_repository_relations"] == 2

    duplicate = {"secondary/client.ts": "export class PaymentAdapter {}\n"}
    await index("f", "歧义支付实现", duplicate)
    assert {link["target"] for link in await edges(caller)} == {config}
    await index("f", "歧义支付实现", {})
    assert {link["target"] for link in await edges(caller)} == {target, config}
    await index("b", "支付接口", {"api/core.ts": "export interface CorePort {}\n"})
    assert {link["target"] for link in await edges(caller)} == {config}
    assert await edges(target) == []
