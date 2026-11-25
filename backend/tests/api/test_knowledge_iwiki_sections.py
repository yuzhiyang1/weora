"""验证项目 iWiki 章节复用知识评审，按范围维护独立版本。"""

import asyncio
from uuid import uuid4

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member

pytestmark = pytest.mark.anyio


async def test_iwiki_sections_version_scope_review_and_retry(runs: RunsHarness) -> None:
    endpoint = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    path = f"{endpoint}/imports/iwiki-sections"
    scope = {"project_id": str((await project(runs, "文档汇总项目"))["id"])}
    other = {"project_id": str((await project(runs, "其他文档项目"))["id"])}
    first = {
        "run_id": str(uuid4()), "source": "iwiki://42",
        "sections": [
            {"section": "business-api", "body": "InvoiceCode 初版协议"},
            {"section": "glossary", "body": "ShareLedger 术语"},
        ],
    }

    async def publish(payload: dict[str, object], params: dict[str, str] = scope) -> list[dict[str, object]]:
        result = await runs.client.post(path, params=params, json=payload, headers=runs.headers())
        assert result.status_code == 201, result.text
        return result.json()["items"]  # type: ignore[no-any-return]

    async def body(entry: object) -> str:
        result = await runs.client.get(f"{endpoint}/{entry}", params=scope)
        assert result.status_code == 200, result.text
        return str(result.json()["body"])

    manual = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "kind": "doc", "title": "手工维护内容", "body": "独立历史正文",
        "source": {"type": "manual", "resource_path": "docs/team-codebase/external-knowledge/business-api.md"},
    })
    assert manual.status_code == 201, manual.text
    manual_id = manual.json()["id"]
    approved = await runs.client.post(f"{endpoint}/{manual_id}/review", params=scope, headers=runs.headers(),
                                     json={"revision": 1, "decision": "accept"})
    assert approved.status_code == 200

    async with runs.sessions() as db:
        concurrent = db.get_bind().dialect.name == "postgresql"
    if concurrent:
        one, replay = await asyncio.gather(publish(first), publish(first))
    else:
        one, replay = await publish(first), await publish(first)
    assert [record["id"] for record in one] == [record["id"] for record in replay]
    api_entry, glossary_entry = one
    assert api_entry["revision"] == glossary_entry["revision"] == 1
    assert api_entry["status"] == "active"
    assert await body(api_entry["id"]) == "InvoiceCode 初版协议"
    other_records = await publish(first | {"run_id": str(uuid4())}, other)
    assert {record["id"] for record in other_records}.isdisjoint({record["id"] for record in one})
    hidden = await runs.client.get(f"{endpoint}/{api_entry['id']}", params=other)
    assert hidden.status_code == 404

    update = first | {
        "run_id": str(uuid4()), "source": "iwiki://59",
        "sections": [{"section": "business-api", "body": "InvoiceCode 修订协议"}],
    }
    async with member(runs, "章节投稿成员") as (client, _):
        submitted = await client.post(path, params=scope, json=update, headers=headers(client))
        assert submitted.status_code == 201, submitted.text
        response = submitted.json()["items"][0]
        assert response["id"] == api_entry["id"]
        assert response["revision"] == 2 and response["revision_status"] == "pending"
        with_different_body = update | {"sections": [{"section": "business-api", "body": "另一个版本"}]}
        conflict = await client.post(path, params=scope, json=with_different_body, headers=headers(client))
        assert conflict.status_code == 409
    assert await body(api_entry["id"]) == "InvoiceCode 初版协议"
    accepted = await runs.client.post(f"{endpoint}/{api_entry['id']}/review", params=scope, headers=runs.headers(),
                                      json={"revision": 2, "decision": "accept"})
    assert accepted.status_code == 200, accepted.text
    assert await body(api_entry["id"]) == "InvoiceCode 修订协议"
    assert await body(glossary_entry["id"]) == "ShareLedger 术语"
    assert await body(manual_id) == "独立历史正文"
    assert (await publish(first))[0]["revision"] == 1
    assert await body(api_entry["id"]) == "InvoiceCode 修订协议"

    drafts = await publish(first | {
        "run_id": str(uuid4()), "require_review": True,
        "sections": [{"section": "glossary", "body": "ShareLedger 待审术语"}],
    })
    assert drafts[0]["revision_status"] == "pending"
    assert await body(glossary_entry["id"]) == "ShareLedger 术语"
    failing_batch = await runs.client.post(path, params=scope, headers=runs.headers(), json=first | {
        "run_id": str(uuid4()),
        "sections": [
            {"section": "business-api", "body": "InvoiceCode 另一版"},
            {"section": "glossary", "body": "此时不能覆盖待审术语"},
        ],
    })
    assert failing_batch.status_code == 409
    assert await body(api_entry["id"]) == "InvoiceCode 修订协议"
