"""验证 iWiki 原页术语生成的关系受评审、范围与目标版本约束。"""

from uuid import UUID, uuid4, uuid5

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora_code_graph.iwiki_links import CodeEvidence, key_terms
from weora_code_graph.reconcile import Page

pytestmark = pytest.mark.anyio


def test_iwiki_match_uses_labels_then_exact_body_terms_and_skips_ambiguity() -> None:
    terms = key_terms("查阅 /api/v1/order、 /api/v1/orders、 `LedgerClient` 和 RetryOrder。另有 create_invoice。")
    assert terms == ["/api/v1/order", "/api/v1/orders", "LedgerClient", "RetryOrder", "create_invoice"]
    pages = [
        Page("code:1", "ledger_client.py", "客户接口。", "", "1"),
        Page("code:2", "api-guides.md", "仅公开 GET /api/v1/orders。", "", "1"),
        Page("code:3", "src/retry.py", "- class `RetryOrder`（第 4 行）", "", "1"),
        Page("code:4", "others/retry.py", "- class `RetryOrder`（第 1 行）", "", "1"),
        Page("code:5", "core.ts", "说明 create_invoice 的返回值。", "", "1"),
    ]
    assert [(link.page.id, link.term, link.confidence) for link in CodeEvidence(pages).links(terms)] == [
        ("code:1", "LedgerClient", "0.8"), ("code:2", "/api/v1/orders", "0.6"),
        ("code:5", "create_invoice", "0.6"),
    ]


async def test_iwiki_page_links_publish_and_stale_target_stays_hidden_on_replay(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "iWiki 图谱"))["id"])}
    other = {"project_id": str((await project(runs, "其他图谱"))["id"])}
    path = "service/ledger.py"

    async def index(params: dict[str, str], target: str, symbol: str) -> str:
        response = await runs.client.post(
            f"{root}/codebases/local", params=params, headers=runs.headers(), json={
                "source_key": "a" * 64, "name": "账单代码", "candidates": 1, "incremental": True,
                "files": [{"path": target, "text": f"class {symbol}: pass\n"}],
            },
        )
        assert response.status_code == 200, response.text
        return str(uuid5(UUID(response.json()["codebase_id"]), target))

    async def detail(entry: str) -> list[dict[str, str]]:
        response = await runs.client.get(f"{root}/{entry}", params=scope)
        assert response.status_code == 200, response.text
        return response.json()["relations"]  # type: ignore[no-any-return]

    async def measured() -> dict[str, int]:
        response = await runs.client.get(f"{root}/health", params=scope)
        assert response.status_code == 200, response.text
        return response.json()["graph"]  # type: ignore[no-any-return]

    key = str(uuid4())
    item = {
        "operation_id": key, "kind": "doc", "title": "账单导入指南", "body": "说明服务的人工操作。",
        "source": {"type": "wiki", "ref": "https://iwiki.woa.com/p/42"},
    }
    payload = {"items": [item], "publish_reviewed": True, "iwiki_terms": {key: ["LedgerClient"]}}
    invalid = await runs.client.post(f"{root}/imports", params=scope, headers=runs.headers(), json={
        **payload, "items": [item | {"source": {"type": "wiki", "ref": "https://unknown.example/p/42"}}],
    })
    assert invalid.status_code == 422
    target = await index(scope, path, "LedgerClient")
    await index(other, path, "LedgerClient")
    baseline = await measured()
    async with member(runs, "iWiki 导入者") as (reader, _):
        response = await reader.post(f"{root}/imports", params=scope, json=payload, headers=headers(reader))
        assert response.status_code == 201, response.text
        row = response.json()["items"][0]
        entry = row["id"]
        assert row["revision_status"] == "pending"
        assert row["relations"] == []
        assert not any(link["target"] == entry for link in await detail(target))
        published = await runs.client.post(
            f"{root}/{entry}/review", params=scope, headers=runs.headers(), json={"decision": "accept", "revision": 1},
        )
        assert published.status_code == 200, published.text
        outgoing = [link for link in await detail(entry) if link["derived"] == "iwiki"]
        assert outgoing == [{
            "kind": "MAPS_TO", "target": target, "title": path, "direction": "out", "derived": "iwiki",
            "source_revision": "1", "target_revision": "1", "evidence": "LedgerClient", "confidence": "0.8",
        }]
        assert any(link.get("derived") == "iwiki" and link["target"] == entry for link in await detail(target))
        assert (await measured())["relations"] == baseline["relations"] + 1
        recalled = await runs.client.get(root, params=scope | {"query": "账单导入指南", "recall_mode": "true"})
        assert any(
            row["id"] == entry and any(link["target"] == target for link in row["relations"])
            for row in recalled.json()["items"]
        )
        assert await index(scope, path, "DifferentService") == target
        assert await detail(entry) == []
        assert not any(link["target"] == entry for link in await detail(target))
        stale = await measured()
        assert (stale["relations"], stale["unresolved"]) == (baseline["relations"], baseline["unresolved"])
        await index(scope, "service/replacement.py", "LedgerClient")
        replay = await reader.post(f"{root}/imports", params=scope, json=payload, headers=headers(reader))
        assert replay.status_code == 201, replay.text
        assert replay.json()["items"][0]["id"] == entry
        assert replay.json()["items"][0]["relations"] == []
