"""验证调和预览、评审边界、发布幂等和代码版本替换。"""

import asyncio
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import select

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora.modules.knowledge.models import KnowledgeEvent, KnowledgeProductMapping

pytestmark = pytest.mark.anyio


async def test_reconciliation_publication_and_review(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    endpoint = f"{path}/codebases/reconcile"
    scope = {"project_id": str((await project(runs, "账单调和"))["id"])}
    other = {"project_id": str((await project(runs, "其他调和项目"))["id"])}

    async def index(symbol: str, params: dict[str, str], key: str) -> str:
        response = await runs.client.post(
            f"{path}/codebases/local", params=params, headers=runs.headers(),
            json={
                "source_key": key * 64, "name": "账单代码", "incremental": True, "candidates": 1,
                "files": [{"path": "ledger.py", "text": f"class {symbol}: pass\n"}],
            },
        )
        assert response.status_code == 200, response.text
        return str(uuid5(UUID(response.json()["codebase_id"]), "ledger.py"))

    async def review(entry: str, revision: int) -> None:
        response = await runs.client.post(
            f"{path}/{entry}/review", params=scope, headers=runs.headers(),
            json={"decision": "accept", "revision": revision},
        )
        assert response.status_code == 200, response.text

    async def submit(file: str, *, entry: str = "", target: str = "") -> str:
        response = await runs.client.post(
            f"{path}/{entry}/revisions" if entry else path, params=scope, headers=runs.headers(),
            json={
                "operation_id": str(uuid4()), "title": file, "body": "BillingLedger\n\nGET /missing",
                "kind": "doc" if file.startswith("product/") else "rule",
                "source": {"type": "manual", "resource_path": file},
                "base_revision": 1 if entry else None,
                "relations": [{"kind": "MAPS_TO", "target": target}] if target else [],
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["revision_status"] == "pending"
        return str(response.json()["id"])

    async def relations(entry: str) -> list[dict[str, str]]:
        response = await runs.client.get(f"{path}/{entry}", params=scope)
        assert response.status_code == 200, response.text
        return response.json()["relations"]  # type: ignore[no-any-return]

    code = await index("BillingLedger", scope, "a")
    alternate = await index("BillingLedger", other, "b")
    product_id = await submit("product/billing.md")
    rule = await submit("rules/billing.md")
    await review(rule, 1)
    initial = await runs.client.get(endpoint, params=scope)
    assert initial.status_code == 200 and initial.json()["mappings"] == []
    await review(product_id, 1)

    async with member(runs, "调和只读成员") as (reader, _):
        preview = await reader.get(endpoint, params=scope)
        assert preview.status_code == 200, preview.text
        denied = await reader.post(endpoint, params=scope, headers=headers(reader))
        assert denied.status_code == 403
    report = preview.json()
    assert report["preview"] is True and report["changed"] is False
    assert {(item["source"]["id"], item["target"]["id"]) for item in report["mappings"]} == {(product_id, code)}
    assert [(item["source"]["id"], item["target"]["id"]) for item in report["relation_candidates"]] == [(rule, code)]
    assert [item["kind"] for item in report["gaps"]] == ["API_DOC_NO_IMPL"]
    assert alternate not in preview.text
    assert await relations(product_id) == [] and await relations(rule) == []
    assert (await runs.client.get(endpoint)).status_code == 422

    async def publish() -> bool:
        response = await runs.client.post(endpoint, params=scope, headers=runs.headers())
        assert response.status_code == 200, response.text
        assert response.json()["relation_candidates"] == []
        return bool(response.json()["changed"])

    # SQLite 共用连接只验证功能，PostgreSQL 同时发布验证事务锁。
    async with runs.sessions() as db:
        postgresql = db.get_bind().dialect.name == "postgresql"
    if postgresql:
        assert sorted(await asyncio.gather(publish(), publish())) == [False, True]
    else:
        assert await publish()
        assert not await publish()
    assert [(link["target"], link["direction"]) for link in await relations(product_id)] == [(code, "out")]
    inbound = [link for link in await relations(code) if link.get("derived") == "product_reconciliation"]
    assert [(link["target"], link["direction"]) for link in inbound] == [(product_id, "in")]
    recalled = await runs.client.get(path, params=scope | {"query": "BillingLedger", "recall_mode": "true"})
    assert recalled.status_code == 200
    hit = next(row for row in recalled.json()["items"] if row["id"] == product_id)
    assert any(link["target"] == code and link["derived"] == "product_reconciliation" for link in hit["relations"])
    async with runs.sessions() as db:
        recorded = (await db.scalars(select(KnowledgeEvent).where(
            KnowledgeEvent.entry_id == UUID(product_id), KnowledgeEvent.action == "code_reconciled",
        ))).all()
        assert len(recorded) == 1 and str(recorded[0].actor_id) == runs.user_id

    await submit("rules/billing.md", entry=rule, target=code)
    assert await relations(rule) == []
    await review(rule, 2)
    assert [link["target"] for link in await relations(rule)] == [code]
    assert (await runs.client.get(endpoint, params=scope)).json()["relation_candidates"] == []

    await index("AnotherService", scope, "a")
    assert await relations(product_id) == []
    assert await publish()
    async with runs.sessions() as db:
        assert (await db.scalars(select(KnowledgeProductMapping))).all() == []
