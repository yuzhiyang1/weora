"""覆盖团队路径的贡献容错、审核门禁和发布快照。"""

from uuid import uuid4

import pytest
from pydantic import JsonValue

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs

pytestmark = pytest.mark.anyio


async def test_team_paths_follow_review_and_keep_their_logical_target(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    foreign = {"project_id": str((await project(runs, "其他项目"))["id"])}

    async def create(
        kind: str, resource: str | None, body: str = "发布说明", *, related: JsonValue = None,
        params: dict[str, str] = scope,
    ) -> dict[str, JsonValue]:
        source = {"type": "manual"} | ({"resource_path": resource} if resource else {})
        response = await runs.client.post(root, params=params, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": f"CheckoutPaths {kind}", "body": body, "kind": kind,
            "source": source, "related": related,
        })
        assert response.status_code == 201, response.text
        result: dict[str, JsonValue] = response.json()
        return result

    async def review(entry: JsonValue, *, expected: int = 200, params: dict[str, str] = scope) -> None:
        response = await runs.client.post(
            f"{root}/{entry}/review", params=params, headers=runs.headers(),
            json={"decision": "accept", "revision": 1},
        )
        assert response.status_code == expected, response.text

    async def detail(entry: JsonValue, params: dict[str, str] = scope) -> dict[str, JsonValue]:
        response = await runs.client.get(f"{root}/{entry}", params=params)
        assert response.status_code == 200, response.text
        result: dict[str, JsonValue] = response.json()
        return result

    other = await create("rule", "rules/refund.md", params=foreign)
    await review(other["id"], params=foreign)
    note = await create(
        "learning", "learnings/refund-flow.md", "退款经验 [规则](rules/refund.md)", related={
            "Supersedes": "rules/refund",
            "CONFLICTS_WITH": "rules/refund",
            "REFERENCES": ["docs/runbook", "learnings/refund-flow", 42],
        },
    )
    assert note["status"] == "active" and note["relations"] == []
    findings = note["relation_findings"]
    assert isinstance(findings, list) and all(isinstance(row, dict) and row["level"] == "warn" for row in findings)
    assert {row["code"] for row in findings if isinstance(row, dict)} == {
        "incompatible_relation", "self_reference", "invalid_path", "target_unavailable",
    }

    failure = await create("doc", "docs/invalid.md", related={"DEPENDS_ON": "docs/runbook"})
    preview = await runs.client.get(f"{root}/{failure['id']}/review", params=scope | {"revision": "1"})
    assert preview.status_code == 200 and preview.json()["revision_status"] == "pending", preview.text
    assert preview.json()["relation_findings"][0]["level"] == "fail"
    await review(failure["id"], expected=422)
    assert (await runs.client.get(f"{root}/{failure['id']}", params=scope)).status_code == 404
    hidden = await runs.client.get(f"{root}/{failure['id']}/review", params=foreign | {"revision": "1"})
    assert hidden.status_code == 404

    # 路径声明以团队根为基准，无资源路径的文档也能声明出向关系。
    source = await create("doc", None, related={"REFERENCES": ["docs/runbook", "docs/runbook.md"]})
    await review(source["id"])
    target = await create("rule", "rules/refund.md")
    await review(target["id"])
    handbook = await create("doc", "docs/runbook.md")
    await review(handbook["id"])
    assert (await detail(other["id"], foreign))["relations"] == []
    assert (await detail(note["id"]))["relations"] == [
        {"kind": "CONFLICTS_WITH", "target": target["id"], "title": "CheckoutPaths rule", "direction": "out"},
        {"kind": "REFERENCES", "target": handbook["id"], "title": "CheckoutPaths doc", "direction": "out"},
    ]
    assert (await detail(source["id"]))["relations"] == [
        {"kind": "REFERENCES", "target": handbook["id"], "title": "CheckoutPaths doc", "direction": "out"},
    ]
    assert (await detail(target["id"]))["relations"] == [
        {"kind": "CONFLICTS_WITH", "target": note["id"], "title": "CheckoutPaths learning", "direction": "in"},
    ]
