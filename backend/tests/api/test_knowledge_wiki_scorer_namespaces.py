"""同一目录清单限定 Wiki 读取与评分标准，保留各自发布身份。"""

from uuid import UUID, uuid4

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.namespace_policy import resolve_namespaces
from weora.modules.knowledge.session_scoring import adoption_template
from weora.modules.knowledge.store import KnowledgeStore, Scope

pytestmark = pytest.mark.anyio


async def test_declared_wiki_namespace_controls_paths_search_and_status(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "代码 Wiki 项目"))["id"])}
    other = {"project_id": str((await project(runs, "有 Wiki 目录的项目"))["id"])}
    auth = runs.headers()

    async def index(name: str, key: str) -> str:
        created = await runs.client.post(f"{root}/codebases/local", params=scope, headers=auth, json={
            "name": name, "source_key": key * 64, "candidates": 1, "incremental": True,
            "files": [{"path": "src/check.py", "text": "def verification(): return 1"}],
        })
        assert created.status_code == 200, created.text
        return str(created.json()["codebase_id"])

    private = await index("InvoiceModel", "a")
    shared = await index("Runbook", "b")
    wiki_path = "teamwiki/evidence/code/InvoiceModel/overview"
    created = await runs.client.post(root, params=scope, headers=auth, json={
        "operation_id": str(uuid4()), "kind": "doc", "title": "开票文档",
        "source": {"type": "manual", "resource_path": "docs/invoice.md"},
        "body": f"参见 [代码概览]({wiki_path})。",
    })
    note = created.json()["id"]
    reviewed = await runs.client.post(f"{root}/{note}/review", params=scope, headers=auth,
        json={"revision": 1, "decision": "accept"})
    assert reviewed.status_code == 200, reviewed.text
    evidence = reviewed.json()["relations"][0]["target"]

    settings = f"{root}/settings/resource-namespaces"
    manifest = {
        "base_revision": 0,
        "roles": {"billing": {"wiki": ["InvoiceMODEL"]}},
        "projects": {other["project_id"]: {"wiki": ["invoicemodel"]}},
    }
    saved = await runs.client.put(settings, headers=auth, json=manifest)
    assert saved.status_code == 200, saved.text
    assert (await runs.client.get(f"{root}/by-path", params=scope | {"path": wiki_path})).status_code == 404
    assert (await runs.client.get(f"{root}/{evidence}", params=scope)).status_code == 404
    assert (await runs.client.get(f"{root}/codebases/local/{private}", params=scope)).status_code == 404
    assert (await runs.client.get(f"{root}/codebases/local/{private}/design", params=scope)).status_code == 404
    assert (await runs.client.get(f"{root}/{note}", params=scope)).json()["relations"] == []
    listed = await runs.client.get(f"{root}/codebases", params=scope)
    assert listed.status_code == 200, listed.text
    assert [row["codebase_id"] for row in listed.json()["items"]] == [shared]
    found = await runs.client.get(root, params=scope | {"query": "InvoiceModel"})
    assert found.status_code == 200, found.text
    assert evidence not in {row["id"] for row in found.json()["items"]}

    enabled = await runs.client.put(settings, headers=auth, json=manifest | {
        "base_revision": 1, "members": {runs.user_id: "billing"},
    })
    assert enabled.status_code == 200, enabled.text
    path = await runs.client.get(f"{root}/by-path", params=scope | {"path": wiki_path})
    assert path.status_code == 200 and path.json()["id"] == evidence
    assert (await runs.client.get(f"{root}/{note}", params=scope)).json()["relations"][0]["target"] == evidence
    assert (await runs.client.get(f"{root}/by-path", params=other | {"path": wiki_path})).status_code == 404


async def test_scorer_directory_resolution_conflict_and_template_fallback(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    project_id = str((await project(runs, "风险评审项目"))["id"])
    scope = {"project_id": project_id}
    auth = runs.headers()
    catalog = f"{root}/session-scorers"
    settings = f"{root}/settings/resource-namespaces"
    schema = {"passing_score": 1, "labels": [
        {"value": "pass", "score": 1, "description": "通过"}, {"value": "fail", "score": 0, "description": "未通过"},
    ]}

    async def publish(name: str, prompt: str, namespace: str = "", **extra: object) -> dict:
        payload = schema | {"prompt": prompt, "sessions": True, **extra}
        if namespace:
            payload["namespace"] = namespace
        result = await runs.client.put(f"{catalog}/{name}", headers=auth, json=payload)
        assert result.status_code == 200, result.text
        return result.json()

    baseline = await publish("quality", "Shared quality rubric.")
    delivery = await publish("quality", "Delivery rubric.", "delivery")
    risk = await publish("quality", "Risk rubric.", "risk")
    assert len({entry["id"] for entry in (baseline, delivery, risk)}) == 3
    config = {
        "base_revision": 0, "roles": {
            "delivery": {"scorers": ["delivery"]}, "risk": {"scorers": ["risk"]},
        },
        "projects": {project_id: {"scorers": ["risk"]}},
    }
    assert (await runs.client.put(settings, headers=auth, json=config)).status_code == 200

    async def normal_plan(params: dict[str, str] | None = None):
        return await runs.client.post(f"{catalog}/plan", params=params, headers=auth, json={"scorers": ["quality"]})

    assert (await normal_plan()).json()["items"][0]["id"] == baseline["id"]
    assert (await normal_plan(scope)).json()["items"][0]["id"] == risk["id"]
    config |= {"base_revision": 1, "members": {runs.user_id: "delivery"}}
    assert (await runs.client.put(settings, headers=auth, json=config)).status_code == 200
    visible = (await normal_plan()).json()["items"][0]
    assert visible["id"] == delivery["id"] and visible["namespace"] == "delivery"
    denied = await normal_plan(scope)
    assert denied.status_code == 404 and denied.json()["detail"]["code"] == "KNOWLEDGE_SCORER_NOT_FOUND"
    listed = await runs.client.get(catalog, params=scope)
    assert listed.status_code == 200 and listed.json()["items"] == []

    standard = {"labels": [
        {"value": "adopted", "score": 1, "description": "采纳"},
        {"value": "ignored", "score": 0, "description": "忽略"},
    ]}
    root_template = await publish("teamai-adoption", "Check {{ doc.id }}.", **standard)
    override = await publish("teamai-adoption", "Delivery {{ doc.id }}.", "delivery", **standard)

    async def active_template() -> UUID:
        workspace, user = UUID(runs.workspace_id), UUID(runs.user_id)
        async with runs.sessions() as db:
            folders = await resolve_namespaces(db, workspace, user, None)
            store = KnowledgeStore(db, Scope(workspace, None, user, "owner", namespaces=folders))
            row = await adoption_template(store)
            assert row is not None
            return row.id

    assert await active_template() == UUID(override["id"])
    cleared = await runs.client.delete(f"{catalog}/teamai-adoption", headers=auth, params={
        "base_revision": "1", "namespace": "delivery",
    })
    assert cleared.status_code == 200 and cleared.json()["archived"] and cleared.json()["revision"] == 2
    assert cleared.json()["namespace"] == "delivery"
    assert await active_template() == UUID(root_template["id"])
