"""通过正常贡献、评审、读取入口验证正文链接随发布版本生效。"""

from uuid import uuid4

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs

pytestmark = pytest.mark.anyio


async def test_markdown_references_follow_visible_publications(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "其他项目"))["id"])}

    async def contribute(
        title: str, path: str, body: str, params: dict[str, str], *, kind: str = "doc",
        relations: list[dict[str, str]] | None = None,
    ) -> str:
        response = await runs.client.post(root, params=params, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": title, "body": body, "kind": kind,
            "source": {"type": "file", "ref": path}, "relations": relations or [],
        })
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    async def approve(entry: str, params: dict[str, str], revision: int = 1) -> dict[str, object]:
        response = await runs.client.post(
            f"{root}/{entry}/review", params=params, headers=runs.headers(),
            json={"decision": "accept", "revision": revision},
        )
        assert response.status_code == 200, response.text
        payload: dict[str, object] = response.json()
        return payload

    async def links(entry: str, params: dict[str, str]) -> list[tuple[str, str, str]]:
        result = await runs.client.get(f"{root}/{entry}", params=params)
        assert result.status_code == 200, result.text
        return [(row["direction"], row["kind"], row["target"]) for row in result.json()["relations"]]

    reference_path = "docs/payments/receipt guide.md"
    shared = await contribute("共享收据指南", reference_path, "共享支付流程。", {})
    await approve(shared, {})
    shared_origin = await contribute("共享支付手册", "docs/handbook.md", "[收据](<payments/receipt guide.md>)", {})
    await approve(shared_origin, {})
    foreign = await contribute("其他项目收据", reference_path, "其他项目内容。", other)
    await approve(foreign, other)

    origin_path = "docs/payments/lease.md"
    source = await contribute(
        "PaymentLease 配置",
        origin_path,
        "[指南](receipt%20guide#approve)\n[重复](<docs/payments/receipt guide.md?view=true>)\n"
        "\n[内部规则][validate]\n\n[validate]: ../../rules/validate.md\n",
        scope,
    )
    target = await contribute("项目收据指南", reference_path.removesuffix(".md"), "本项目支付流程。", scope)
    rule = await contribute("校验规则", "rules/validate.md", "检验收据字段。", scope, kind="rule")
    assert (await approve(source, scope))["relations"] == []
    assert await links(shared, scope) == [("in", "REFERENCES", shared_origin)]

    await approve(target, scope)
    await approve(rule, scope)
    assert await links(source, scope) == [("out", "REFERENCES", target), ("out", "REFERENCES", rule)]
    assert await links(target, scope) == [("in", "REFERENCES", source)]
    assert await links(foreign, other) == []
    # 项目同路径发布会隐藏共享目标，工作空间自己的关系仍然存在。
    assert await links(shared_origin, scope) == []
    assert await links(shared_origin, {}) == [("out", "REFERENCES", shared)]

    result = await runs.client.post(
        f"{root}/recall/preview", params=scope, headers=runs.headers(), json={"query": "PaymentLease"},
    )
    assert result.status_code == 200, result.text
    hit = result.json()["items"][0]
    assert hit["id"] == source and [link["target"] for link in hit["relations"]] == [target, rule]

    session = {"session": "markdown-relation-snapshot"}
    recorded = await runs.client.post(
        f"{root}/recall", params=scope, headers=runs.headers(), json=session | {"query": "PaymentLease"},
    )
    assert recorded.status_code == 200 and recorded.json()["items"][0]["id"] == source, recorded.text

    second_rule = await contribute("新版校验规则", "rules/validate-v2.md", "新增校验字段。", scope, kind="rule")
    await approve(second_rule, scope)
    version = await runs.client.post(
        f"{root}/{source}/revisions", params=scope, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "kind": "doc", "base_revision": 1,
            "title": "PaymentLease 配置", "body": "[新版](receipt%20guide.md) [新版规则](../../rules/validate-v2)",
            "source": {"type": "file", "resource_path": origin_path.removesuffix(".md")},
            "relations": [{"kind": "SUPERSEDES", "target": target}],
        },
    )
    assert version.status_code == 201 and version.json()["revision_status"] == "pending", version.text
    assert await links(source, scope) == [("out", "REFERENCES", target), ("out", "REFERENCES", rule)]
    assert await links(second_rule, scope) == []
    await approve(source, scope, 2)
    assert await links(rule, scope) == []
    assert await links(source, scope) == [("out", "SUPERSEDES", target), ("out", "REFERENCES", second_rule)]
    assert await links(second_rule, scope) == [("in", "REFERENCES", source)]
    assert await links(target, scope) == [("in", "SUPERSEDES", source)]

    duplicate = await contribute("重复的规则路径", "rules/validate-v2", "另一条同路径规则。", scope, kind="rule")
    await approve(duplicate, scope)
    # 扩展名别名撞到两个已发布条目时，两个目标都不附加这条推导关系。
    assert await links(source, scope) == [("out", "SUPERSEDES", target)]
    assert await links(second_rule, scope) == [] and await links(duplicate, scope) == []

    # 旧召回记录保持它当时的关系快照，打开原链接仍计入该次召回。
    for _ in range(2):
        opened = await runs.client.post(f"{root}/{rule}/open", params=scope, headers=runs.headers(), json=session)
        assert opened.status_code == 200, opened.text
    stats = await runs.client.post(f"{root}/recall/stats", params=scope, headers=runs.headers(), json=session)
    assert stats.status_code == 200 and stats.json()["items"][0]["via_relation"] == 1, stats.text
