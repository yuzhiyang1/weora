"""缺口只能引用当前项目已发布的扫描版本。"""

from uuid import UUID

import pytest
from sqlalchemy import select, update

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeRecallRun

pytestmark = pytest.mark.anyio


async def test_published_gaps_are_distinct_from_recall_matches_and_refresh_with_index(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "缺口项目"))["id"])}
    other = {"project_id": str((await project(runs, "隔离项目"))["id"])}
    gaps = root + "/codebases/gaps"
    names = ("nebularouter", "vectorgalaxy", "warptransit", "latticefirmware", "quasarbus", "moonrelay")

    async def index(imports: bool) -> dict[str, object]:
        source = "\n".join(f"import {name}" for name in names) + "\n" if imports else ""
        result = await runs.client.post(f"{root}/codebases/local", params=scope, headers=runs.headers(), json={
            "source_key": "a" * 64, "name": "transport", "candidates": 2, "incremental": True, "files": [
                {"path": "src/core.py", "text": source + "def transfer(): return 1\n"},
                {"path": "src/incomplete.py", "text": "def broken(:" if imports else "def complete(): return 0\n"},
            ],
        })
        assert result.status_code == 200, result.text
        value: dict[str, object] = result.json()
        return value

    await index(True)
    response = await runs.client.get(gaps, params=scope)
    known = response.json()["items"]
    assert response.status_code == 200, response.text
    assert {item["kind"] for item in known} == {"EXTERNAL_IMPORT", "EXTERNAL_DEP_UNDOCUMENTED", "PARSE_SKIP"}
    page = "teamwiki/evidence/code/transport/gaps/detected.md"
    assert all(item["path"] == page for item in known)
    opened = await runs.client.get(f"{root}/by-path", params=scope | {"path": page})
    assert opened.status_code == 200 and "EXTERNAL_DEP_UNDOCUMENTED" in opened.json()["body"]
    assert (await runs.client.get(f"{root}/by-path", params=other | {"path": page})).status_code == 404
    assert (await runs.client.get(gaps, params=other)).json() == {"items": []}

    # 模拟升级前缺少缺口页的索引，相同输入仍应补齐页面并复用提取事实。
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeEntry).where(
            KnowledgeEntry.id == UUID(known[0]["entry_id"]),
        ).values(status="archived"))
    reused = await index(True)
    assert reused["reused"] == 2 and reused["extracted"] == 0
    assert (await runs.client.get(gaps, params=scope)).json()["items"] == known

    params = scope | {"query": "nebularouter", "check": True, "check_details": True,
                      "recall_mode": True, "depth": "context"}
    result = (await runs.client.get(root, params=params)).json()
    assert result["relevant"] is False and "title" not in result
    assert [row["kind"] for row in result["knowledge_gaps"]] == ["EXTERNAL_IMPORT", "EXTERNAL_DEP_UNDOCUMENTED"]
    miss = (await runs.client.get(root, params=params | {"query": "no-known-gap-topic"})).json()
    assert "knowledge_gaps" not in miss
    regular = await runs.client.get(root, params=params | {"check": False, "depth": "lookup"})
    assert regular.json()["items"] == [] and regular.json()["knowledge_gaps"]
    async with runs.sessions() as db:
        assert await db.scalar(select(KnowledgeRecallRun.id).where(
            KnowledgeRecallRun.workspace_id == UUID(runs.workspace_id),
        )) is None

    await index(False)
    assert (await runs.client.get(gaps, params=scope)).json() == {"items": []}
    fresh = (await runs.client.get(f"{root}/by-path", params=scope | {"path": page})).json()
    assert fresh["id"] == opened.json()["id"] and fresh["revision"] > opened.json()["revision"]
    assert "未发现预定义的知识缺口" in fresh["body"]


async def test_gap_lookup_uses_source_lines_outside_summary_and_current_revision(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "源码证据"))["id"])}
    other = {"project_id": str((await project(runs, "独立索引"))["id"])}
    gaps = f"{root}/codebases/gaps"
    head = "\n".join(f"import anchor{index:02}" for index in range(30))

    async def index(first_line: int) -> None:
        import_source = (
            "\n" * (first_line - 1) + "import zuluarchive.node\nfrom zuluarchive.node import setup\n"
            if first_line else "def transfer(): return 1\n"
        )
        response = await runs.client.post(f"{root}/codebases/local", params=scope, headers=runs.headers(), json={
            "source_key": "b" * 64, "name": "archive", "candidates": 3, "incremental": True, "files": [
                {"path": "src/core.py", "text": head},
                {"path": "src/port.py", "text": import_source},
                {"path": "src/gateway.ts", "text": "// route\n\nimport { relay } from './missing-relay';\n"},
            ],
        })
        assert response.status_code == 200, response.text

    async def lookup(query: str, params: dict[str, str] = scope) -> list[dict[str, object]]:
        response = await runs.client.get(gaps, params=params | {"query": query})
        assert response.status_code == 200, response.text
        items: list[dict[str, object]] = response.json()["items"]
        return items

    def evidence(first_line: int) -> list[dict[str, object]]:
        return [
            {"name": "zuluarchive.node", "path": "src/port.py", "line": line}
            for line in (first_line, first_line + 1)
        ]

    await index(7)
    matches = await lookup("(`zuluarchive.node`)，")
    assert [match["kind"] for match in matches] == ["EXTERNAL_IMPORT", "EXTERNAL_DEP_UNDOCUMENTED"]
    assert all(match["evidence"] == evidence(7) and "zuluarchive" not in str(match["examples"]) for match in matches)
    assert await lookup("zuluarchive.node", other) == []
    for query in ("zuluarchive", "PORT.PY", r"src\port.py"):
        assert await lookup(query) == matches
    [unresolved] = await lookup("./missing-relay")
    assert unresolved["kind"] == "UNRESOLVED_IMPORT"
    assert unresolved["evidence"] == [{"name": "./missing-relay", "path": "src/gateway.ts", "line": 3}]

    page = matches[0]["path"]
    initial = (await runs.client.get(f"{root}/by-path", params=scope | {"path": page})).json()
    await index(11)
    # 只移动源码行也应产生新版本，旧版本的行号不会进入当前召回。
    assert all(match["evidence"] == evidence(11) for match in await lookup("zuluarchive.node"))
    moved = (await runs.client.get(f"{root}/by-path", params=scope | {"path": page})).json()
    assert moved["id"] == initial["id"] and moved["revision"] > initial["revision"]
    await index(0)
    assert await lookup("zuluarchive.node") == []
    assert await lookup("external import")
