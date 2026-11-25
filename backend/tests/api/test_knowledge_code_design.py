"""代码设计与图谱沿用项目知识发布、检索和审计事务。"""

import asyncio
import json
from uuid import UUID

import pytest
from sqlalchemy import select

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora.modules.knowledge.models import KnowledgeEvent, KnowledgeRevision

pytestmark = pytest.mark.anyio

_SCENARIO = """# 计价流程

```mermaid
sequenceDiagram
    participant Handler
    participant Storage
    Handler->>Storage: rate()
    Storage-->>Handler: rate
```
"""


async def test_design_publish_search_resume_and_invalidate(runs: RunsHarness) -> None:
    current = str((await project(runs, "设计发布项目"))["id"])
    other = str((await project(runs, "隔离项目"))["id"])
    scope = {"project_id": current}
    knowledge = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    files = [
        {
            "path": "handler/billing.py",
            "text": (
                "from storage.rates import rate\ndef total():\n    return rate()\n"
                "def main():\n    return total()\n"
            ),
        },
        {"path": "storage/rates.py", "text": "def rate():\n    return 2\n"},
    ]

    async def sync() -> str:
        response = await runs.client.post(
            f"{knowledge}/codebases/local", params=scope, headers=runs.headers(),
            json={"source_key": "a" * 64, "name": "计价服务", "candidates": len(files), "files": files},
        )
        assert response.status_code == 200, response.text
        return str(response.json()["codebase_id"])

    index = await sync()
    endpoint = f"{knowledge}/codebases/local/{index}/design"
    catalog_root = f"{knowledge}/codebases/graph-documents"

    async def catalog(topic: str, params: dict[str, str] = scope) -> list[dict[str, object]]:
        response = await runs.client.get(f"{catalog_root}/{topic}", params=params)
        assert response.status_code == 200, response.text
        assert response.json()["topic"] == topic
        items: list[dict[str, object]] = response.json()["items"]
        assert all("body" not in item for item in items)
        return items

    assert await catalog("g1") == []
    assert (await runs.client.get(f"{catalog_root}/g1")).status_code == 422
    assert (await runs.client.get(f"{catalog_root}/g3", params=scope)).status_code == 422
    planned = await runs.client.get(endpoint, params=scope)
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    basis = plan["basis"]
    assert len(basis) == 64 and plan["documents"] == {}
    assert plan["project"] == "计价服务"
    assert plan["repository_narrative_context"] is None
    assert [item["slug"] for item in plan["components"]] == ["handler", "storage"]
    assert plan["interactions"] == [["handler", "storage"]]
    assert "handler/billing.py:3" in plan["components"][0]["context"]["calls"]
    assert "handler/billing.py:2" in plan["domain_context"]["declarations"]
    assert "return rate()" not in planned.text
    only = (await runs.client.get(endpoint, params=scope | {"max_modules": "1"})).json()
    assert only["interactions"] == [] and len(only["components"]) == 1
    assert len(only["profile_components"]) == 2
    assert all(row["confidence"] == "EXTRACTED" for row in only["component_manifest"]["components"])

    async def publish(name: str, body: str, *, version: str = basis, status: int = 200) -> dict[str, object]:
        response = await runs.client.post(endpoint, params=scope, headers=runs.headers(),
                                          json={"basis": version, "name": name, "body": body})
        assert response.status_code == status, response.text
        return response.json()  # type: ignore[no-any-return]

    assert (await runs.client.get(endpoint, params={"project_id": other})).status_code == 404
    unauthorized = await runs.client.post(endpoint, params=scope, json={"basis": basis})
    assert unauthorized.status_code == 403
    async with member(runs, "测试项目协作者") as (peer, _):
        assert (await peer.get(endpoint, params=scope)).status_code == 200
        denied = await peer.post(endpoint, params=scope, headers=headers(peer), json={
            "basis": basis, "name": "architecture.md", "body": "# 其他设备快照",
        })
        assert denied.status_code == 403 and denied.json()["detail"]["code"] == "KNOWLEDGE_CODE_DESIGN_FORBIDDEN"
        denied_domain = await peer.post(endpoint, params=scope, headers=headers(peer), json={
            "basis": basis, "name": "_domains.json", "body": json.dumps({"domain": "其他人的领域"}),
        })
        assert denied_domain.status_code == 403
    await publish("unused.md", "# Arbitrary asset", status=422)
    await publish("_repository-overview.md", "# Local narrative", status=422)
    await publish("unknown.profile.json", "{}", status=422)
    await publish("handler.profile.json", json.dumps({"domain": "计费", "layer": "invalid"}), status=422)
    component_profile = {
        "domain": "计费", "layer": "entry", "summary": "处理账单", "responsibilities": ["profilerecallmarker"],
    }
    component_payload = json.dumps(component_profile | {"entrypoints": ["fake.py:1"], "slug": "unknown"})
    profiled = await publish("handler.profile.json", component_payload)
    assert isinstance(profile_items := profiled["items"], list)
    profile_id = profile_items[0]["id"]
    assert json.loads(profile_items[0]["body"]) == component_profile
    duplicate = await publish("handler.profile.json", json.dumps(component_profile | {"domain": "其他领域"}))
    assert duplicate["items"] == profile_items and duplicate["reused"] is True
    profile_page = (await runs.client.get(f"{knowledge}/{profile_id}", params=scope)).json()
    assert "handler/billing.py:4" in profile_page["body"] and "fake.py" not in profile_page["body"]
    profile_hits = (await runs.client.get(knowledge, params=scope | {"query": "profilerecallmarker"})).json()
    assert any(row["id"] == profile_id for row in profile_hits["items"])
    assert (await runs.client.get(f"{knowledge}/{profile_id}", params={"project_id": other})).status_code == 404
    manifest = (await runs.client.get(endpoint, params=scope)).json()["component_manifest"]
    assert [row["confidence"] for row in manifest["components"]] == ["INFERRED", "EXTRACTED"]
    assert manifest["components"][0]["entrypoints"] == ["main (handler/billing.py:4)"]
    assert any(row["from"] == "handler" and row["to"] == "storage" for row in manifest["edges"])
    await publish("_domains.json", json.dumps({"domain": "计价", "keywords": [{}]}), status=422)
    submitted = {
        "domain": "paymentdomainmarker", "description": "计算订单费率",
        "keywords": ["domainsearchtoken", "domainsearchtoken"], "components": ["arbitrary"],
    }
    offer = await publish("_domains.json", json.dumps(submitted))
    assert isinstance(domain_items := offer["items"], list)
    domain_id = domain_items[0]["id"]
    profile = submitted | {"keywords": ["domainsearchtoken"], "components": ["handler", "storage"]}
    assert json.loads(domain_items[0]["body"]) == profile
    reused = await publish("_domains.json", json.dumps({"domain": "不同领域"}))
    assert reused["reused"] is True and reused["items"] == domain_items
    await publish("graph-g5-scenarios.md", "# Invalid sequence", status=422)
    await publish("graph-g5-scenarios.md", _SCENARIO, status=409)

    async with runs.sessions() as db:
        postgres = db.get_bind().dialect.name == "postgresql"

    title = "billingdesignsignature"
    if postgres:
        first, second = await asyncio.gather(
            publish("handler.md", f"# {title}\n\n存储只向处理层返回费率。"),
            publish("handler.md", f"# {title}\n\n候选的处理层设计。"),
        )
        assert first["items"] == second["items"]
        assert bool(first.get("reused")) != bool(second.get("reused"))
    else:
        first = await publish("handler.md", f"# {title}\n\n存储只向处理层返回费率。")
        assert (await publish("handler.md", "# Duplicate"))["items"] == first["items"]
    assets = first["items"]
    assert isinstance(assets, list)
    document_id = assets[0]["id"]
    await publish("storage.md", "# 费率来源")
    await publish("architecture.md", "# 计价技术架构")
    await publish("graph-g5-scenarios.md", _SCENARIO)
    graph = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json={"basis": basis})
    assert graph.status_code == 200, graph.text
    assert len(graph.json()["items"]) == 8
    triple = next(item for item in graph.json()["items"] if item["name"] == "graph-g6-triples.md")
    expected = {
        "g1": "graph-g1-relations.md", "g2": "graph-g2-dataflow.md",
        "g5": "graph-g5-scenarios.md", "g6": "graph-g6-triples.md",
    }
    for topic, filename in expected.items():
        [page] = await catalog(topic, scope | {"codebase_id": index})
        assert page["codebase_id"] == index
        assert page["path"] == f"teamwiki/evidence/code/计价服务/docs/{filename}"
        opened = await runs.client.get(f"{knowledge}/by-path", params=scope | {"path": str(page["path"])})
        assert opened.status_code == 200 and opened.json()["id"] == page["entry_id"], opened.text
        assert opened.json()["source"]["design_asset"] == filename
    assert await catalog("g2", {"project_id": other, "codebase_id": index}) == []
    traced = (await runs.client.get(f"{knowledge}/{triple['id']}", params=scope)).json()
    assert "REFERENCES" in traced["body"] and traced["source"]["design_origin"] == "graph"
    existing = (await runs.client.get(endpoint, params=scope)).json()
    assert existing["documents"]["handler.md"]["id"] == document_id
    assert not existing["components"][0]["context"] and not existing["domain_context"]
    assert json.loads(existing["documents"]["_domains.json"]["body"]) == profile
    domain_hits = (await runs.client.get(knowledge, params=scope | {"query": "domainsearchtoken"})).json()
    assert any(row["id"] == domain_id for row in domain_hits["items"])
    domain_page = (await runs.client.get(f"{knowledge}/{domain_id}", params=scope)).json()
    assert "paymentdomainmarker" in domain_page["body"]
    assert json.loads(domain_page["source"]["domain_profile"]) == profile
    assert domain_page["source"]["domain_assignment"] == "inferred"

    payload = {"basis": basis, "domain_override": "custombillingdomain"}
    explicit = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json=payload)
    assert explicit.status_code == 200, explicit.text
    assigned = explicit.json()["items"][0]
    assert assigned["id"] == domain_id
    assert json.loads(assigned["body"]) == profile | {"domain": "custombillingdomain"}
    retry = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json=payload)
    assert retry.json()["reused"] is True
    after_model_retry = await publish("_domains.json", json.dumps(submitted))
    assert after_model_retry["reused"] is True and after_model_retry["items"] == [assigned]
    explicit_page = (await runs.client.get(f"{knowledge}/{domain_id}", params=scope)).json()
    assert explicit_page["source"]["domain_assignment"] == "explicit"
    assert "custombillingdomain" in explicit_page["body"]
    hits = (await runs.client.get(knowledge, params=scope | {"query": title})).json()
    assert any(row["id"] == document_id and row["source"]["design_basis"] == basis for row in hits["items"])
    assert (await runs.client.get(f"{knowledge}/{document_id}", params={"project_id": other})).status_code == 404
    assert (await runs.client.get(endpoint.removesuffix("/design"), params=scope | {"lint": "true"})).json()[
        "problems"
    ] == []

    # 全量重提取同样的事实不让文档过期，源码变更才撤下旧版本。
    assert await sync() == index
    unchanged = (await runs.client.get(endpoint, params=scope)).json()
    assert unchanged["documents"]["handler.md"]["id"] == document_id and unchanged["basis"] == basis
    files[-1]["text"] = "def rate():\n    return 3\n"
    assert await sync() == index
    updated = (await runs.client.get(endpoint, params=scope)).json()
    assert updated["documents"] == {} and updated["basis"] != basis
    for topic in expected:
        assert await catalog(topic) == []
    await publish("handler.md", "# Stale", status=409)
    await publish("_domains.json", json.dumps(submitted), status=409)
    await publish("handler.profile.json", component_payload, status=409)
    assert all(row["confidence"] == "EXTRACTED" for row in updated["component_manifest"]["components"])
    stale_profile = (await runs.client.get(knowledge, params=scope | {"query": "profilerecallmarker"})).json()
    assert all(row["id"] != profile_id for row in stale_profile["items"])
    stale_hits = (await runs.client.get(knowledge, params=scope | {"query": "domainsearchtoken"})).json()
    assert all(row["id"] != domain_id for row in stale_hits["items"])
    later = await publish("handler.md", "# 新版处理器", version=updated["basis"])
    assert isinstance(later_assets := later["items"], list) and later_assets[0]["id"] == document_id

    async with runs.sessions() as db:
        events = (await db.scalars(select(KnowledgeEvent).where(KnowledgeEvent.entry_id == UUID(document_id)))).all()
        revisions = (await db.scalars(
            select(KnowledgeRevision).where(KnowledgeRevision.entry_id == UUID(document_id))
        )).all()
    assert sorted(event.action for event in events) == [
        "code_design_archived", "code_design_published", "code_design_published",
    ]
    assert all(event.actor_id == UUID(runs.user_id) for event in events)
    assert sorted(revision.revision for revision in revisions) == [1, 2]
    async with runs.sessions() as db:
        domain_revisions = (await db.scalars(
            select(KnowledgeRevision).where(KnowledgeRevision.entry_id == UUID(domain_id))
        )).all()
    assert sorted((revision.revision, revision.source["domain_assignment"]) for revision in domain_revisions) == [
        (1, "inferred"), (2, "explicit"),
    ]
