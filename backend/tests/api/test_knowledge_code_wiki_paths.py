"""使用项目索引、贡献和审核入口验证代码 wiki 引用的发布生命周期。"""

from uuid import uuid4

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs

pytestmark = pytest.mark.anyio


async def test_code_wiki_paths_follow_the_active_index(runs: RunsHarness) -> None:
    scope = {"project_id": str((await project(runs, "开票代码"))["id"])}
    other = {"project_id": str((await project(runs, "其他项目"))["id"])}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    location = "teamwiki/evidence/code/invoice"
    created = await runs.client.post(root, params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "kind": "doc", "title": "InvoiceWikiPaths",
        "source": {"type": "manual", "resource_path": "docs/invoice.md"},
        "body": f"[目录](../{location}/index) [概览]({location}/overview.md)",
        "related": {"MAPS_TO": f"{location}/modules/src", "REFERENCES": f"{location}/docs/architecture.md"},
    })
    assert created.status_code == 201, created.text
    note = created.json()["id"]
    approved = await runs.client.post(
        f"{root}/{note}/review", params=scope, headers=runs.headers(), json={"decision": "accept", "revision": 1},
    )
    assert approved.status_code == 200 and approved.json()["relations"] == [], approved.text

    async def sync(source: str, key: str = "a", name: str = "invoice") -> str:
        result = await runs.client.post(
            f"{root}/codebases/local", params=scope, headers=runs.headers(), json={
                "name": name, "source_key": key * 64, "candidates": 2, "incremental": True, "files": [
                    {"path": "src/invoice.py", "text": source},
                    {"path": "src/pricing.py", "text": "def total(): return 1"},
                ],
            },
        )
        assert result.status_code == 200, result.text
        return str(result.json()["codebase_id"])

    complete = "\n".join(f"def check_{number}(): return {number}" for number in range(6))
    codebase = await sync(complete)
    design = f"{root}/codebases/local/{codebase}/design"
    plan = (await runs.client.get(design, params=scope)).json()
    published = await runs.client.post(design, params=scope, headers=runs.headers(), json={
        "basis": plan["basis"], "name": "architecture.md", "body": "# 开票架构",
    })
    assert published.status_code == 200, published.text
    architecture = published.json()["items"][0]["id"]

    recall = await runs.client.post(
        f"{root}/recall", params=scope, headers=runs.headers(), json={"query": "InvoiceWikiPaths", "session": "wiki"},
    )
    assert recall.status_code == 200, recall.text
    hit = recall.json()["items"][0]
    assert hit["id"] == note
    assert [row["kind"] for row in hit["relations"]] == ["MAPS_TO", "REFERENCES", "REFERENCES", "REFERENCES"]
    module, architecture_link, catalog, overview = [row["target"] for row in hit["relations"]]
    assert architecture_link == architecture
    result = (await runs.client.get(f"{root}/{module}", params=scope)).json()
    assert result["source"]["wiki_page"] == "modules/src.md" and "check_5" in result["body"]
    assert any(row["target"] == note and row["direction"] == "in" for row in result["relations"])
    assert (await runs.client.get(f"{root}/{module}", params=other)).status_code == 404
    named = await runs.client.post(f"{root}/by-path/open", params=scope, headers=runs.headers(), json={
        "path": f"{location}/modules/src", "session": "wiki",
    })
    assert named.status_code == 200 and named.json()["id"] == module, named.text
    stats = await runs.client.post(
        f"{root}/recall/stats", params=scope, headers=runs.headers(), json={"session": "wiki"},
    )
    assert stats.json()["items"][0]["via_relation"] == 1
    hidden = await runs.client.get(f"{root}/by-path", params=other | {"path": f"{location}/modules/src"})
    assert hidden.status_code == 404
    index = (await runs.client.get(f"{root}/{catalog}", params=scope)).json()
    assert "modules/src.md" in index["body"] and "overview.md" in index["body"]
    assert (await runs.client.get(f"{root}/{overview}", params=scope)).json()["source"]["ref"] == "invoice"

    query = "src 模块 check_0 check_1 check_2 check_3 check_4 check_5"
    compact = await runs.client.get(root, params=scope | {"depth": "context", "query": query})
    assert compact.status_code == 200, compact.text
    assert module in {row["id"] for row in compact.json()["items"]}
    assert all(
        not row["source"].get("path") and row["source"].get("wiki_page") != "component.md"
        for row in compact.json()["items"]
    )
    restricted = await runs.client.get(root, params=other | {"depth": "context", "query": query})
    assert all(row["id"] != module for row in restricted.json()["items"])
    deep = await runs.client.get(root, params=scope | {"depth": "lookup", "query": query})
    assert deep.status_code == 200, deep.text
    deep_hits = deep.json()["items"]
    hit = next(row for row in deep_hits if row["id"] == module)
    assert any(row["path"] == "src/invoice.py" for row in hit["related_files"])

    # 快照显示名称可以变化，团队 wiki 路径继续使用目录身份。低于模块门槛后撤销旧页面。
    await sync("def single(): return 1", name="invoice renamed")
    detail = (await runs.client.get(f"{root}/{note}", params=scope)).json()
    assert [row["target"] for row in detail["relations"]] == [catalog, overview]
    assert (await runs.client.get(f"{root}/{module}", params=scope)).status_code == 404
    assert (await runs.client.get(f"{root}/{architecture}", params=scope)).status_code == 404
    updated = await runs.client.get(root, params=scope | {"depth": "context", "query": query})
    assert all(row["id"] != module for row in updated.json()["items"])

    await sync(complete, key="b")
    duplicate = (await runs.client.get(f"{root}/{note}", params=scope)).json()
    # 同一项目两个相同名字的快照不能互相冒充；只有在其中一个快照存在的模块可解析。
    assert [row["kind"] for row in duplicate["relations"]] == ["MAPS_TO"]
    ambiguous = await runs.client.get(f"{root}/by-path", params=scope | {"path": f"{location}/overview"})
    assert ambiguous.status_code == 409
