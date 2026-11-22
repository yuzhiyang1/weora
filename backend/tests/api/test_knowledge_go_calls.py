"""验证 Go 模块、包、调用作用域与已发布图谱的关系。"""

from uuid import UUID, uuid5

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.code_preview import relation_delta
from weora_code_graph.extractor import dependencies, extract, go_code_index, priority, resolved_calls

pytestmark = pytest.mark.anyio


def test_go_selectors_resolve_by_module_package_and_lexical_scope() -> None:
    assert priority("go.mod") < priority("a/go.mod") < priority("app/main.go")
    caller = "app/main.go"
    files = {
        "go.mod": "module example.org/acme/v2\n",
        caller: """package app
import (
    p "example.org/acme/v2/billing"
    "example.org/acme/v2/ledger/v3"
    "example.org/acme/v2/nested"
    "fmt"
)
func Run(service *Worker) {
    p.Refresh()
    ledger.Run()
    p.private()
    nested.Run()
    fmt.Println()
    helper()
    service.Handle()
    generic[int](); genericPair[int,string](1)
    { p.Refresh(); p := mock(); p.Refresh() }
    if p := mock(); true { p.Refresh() }
    for _, p := range callbacks { p.Refresh() }
    switch p := anyValue.(type) { case string: p.Refresh() }
    func(p any) { p.Refresh() }(nil)
    x := "p.Refresh()"
    // p.Refresh()
    router.Get("/api/v1/orders", handler)
}
""",
        "app/helpers.go": "package app\nfunc helper() {}\nfunc generic[T any]() {}\n"
                          "func genericPair[A, B any](a A) {}\n",
        "app/worker.go": "package app\ntype Worker struct{}\nfunc (w *Worker) Handle() {}\n",
        "app/helpers_test.go": "package app\nfunc mock() {}\nfunc hidden() {}\n",
        "billing/bill.go": "package invoicing\nfunc Refresh() {}\nfunc private() {}\n",
        "ledger/v3/core.go": "package ledger\nfunc Run() {}\n",
        "nested/go.mod": "module example.org/acme/v2/nested\n",
        "nested/run.go": "package nested\nfunc Run() {}\n",
        "app/fmt/fmt.go": "package fmt\nfunc Println() {}\n",
    }
    before = {path: extract(path, "fixed", text) for path, text in files.items()}
    available = set(before)
    go = go_code_index(before, available)
    assert dependencies(caller, before[caller], available, go=go) == ["billing/bill.go", "ledger/v3/core.go"]
    assert [(item["callee"], item["target"], item["line"]) for item in resolved_calls(caller, before, available)] == [
        ("p.Refresh", "billing/bill.go", 9), ("ledger.Run", "ledger/v3/core.go", 10),
        ("helper", "app/helpers.go", 14), ("service.Handle", "app/worker.go", 15),
        ("generic", "app/helpers.go", 16), ("genericPair", "app/helpers.go", 16),
        ("p.Refresh", "billing/bill.go", 17),
    ]
    assert {"kind": "route", "name": "GET /api/v1/orders", "line": 24} in before[caller]["symbols"]
    after = before | {"billing/duplicate_linux.go": extract(
        "billing/duplicate_linux.go", "other", "package invoicing\nfunc Refresh() {}\n",
    )}
    removed = relation_delta(before, after)["relations"]
    assert isinstance(removed, dict)
    assert {"kind": "REFERENCES", "source": caller, "target": "billing/bill.go"} in removed["removed"]


async def test_go_calls_and_routes_publish_and_follow_incremental_module_changes(runs: RunsHarness) -> None:
    scope = {"project_id": str((await project(runs, "Go 模块证据"))["id"])}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    caller, peer = "api/handler.go", "ledger/v2/repo.go"

    async def index(module: str) -> tuple[UUID, int]:
        files = {
            "go.mod": f"module {module}\n",
            caller: (
                "package api\nimport \"example.org/payments/ledger/v2\"\n"
                "func planInvoice() { ledger.Refresh(); router.Get(\"/api/v1/invoice\", handler) }\n"
            ),
            peer: "package ledger\nfunc Refresh() {}\n",
        }
        response = await runs.client.post(
            f"{root}/codebases/local", params=scope, headers=runs.headers(), json={
                "source_key": "c" * 64, "name": "账单 Go 服务", "incremental": True, "candidates": len(files),
                "files": [{"path": path, "text": content} for path, content in files.items()],
            },
        )
        assert response.status_code == 200, response.text
        return UUID(response.json()["codebase_id"]), int(response.json()["reused"])

    repo, _ = await index("example.org/payments")
    origin, target = str(uuid5(repo, caller)), str(uuid5(repo, peer))
    reply = await runs.client.get(f"{root}/{origin}", params=scope)
    assert reply.status_code == 200, reply.text
    page = reply.json()
    assert ("REFERENCES", target) in {(row["kind"], row["target"]) for row in page["relations"]}
    assert "GET /api/v1/invoice" in page["body"]
    assert "ledger.Refresh()" in page["body"]

    # iWiki 使用已索引的 Go 路由正文进行关联。
    key = str(uuid5(repo, "wiki-test"))
    summary = await runs.client.post(f"{root}/imports", params=scope, headers=runs.headers(), json={
        "items": [{
            "operation_id": key, "kind": "doc", "title": "iWiki 发票接口", "body": "账单约定。",
            "source": {"type": "wiki", "ref": "https://iwiki.woa.com/p/42"},
        }],
        "publish_reviewed": True, "iwiki_terms": {key: ["/api/v1/invoice"]},
    })
    assert summary.status_code == 201, summary.text
    links = summary.json()["items"][0]["relations"]
    assert [(link["target"], link["confidence"]) for link in links] == [(origin, "0.6")]
    wiki = summary.json()["items"][0]["id"]

    _, reused = await index("example.org/renamed")
    assert reused == 2
    current = (await runs.client.get(f"{root}/{origin}", params=scope)).json()
    assert "ledger.Refresh()" not in current["body"]
    assert all(row["target"] != target for row in current["relations"])
    assert (await runs.client.get(f"{root}/{wiki}", params=scope)).json()["relations"] == []
