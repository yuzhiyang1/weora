"""CLI 召回深度对应的项目范围、依赖和反馈契约。"""

from math import log2
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeRecallRun, KnowledgeRecallRunHit, KnowledgeRevision
from weora.modules.knowledge.recall_code import lookup_graph
from weora.modules.knowledge.search import rank_published
from weora.modules.knowledge.store import Scope, overridden, published

pytestmark = pytest.mark.anyio


async def test_route_context_and_lookup_respect_published_project_graph(runs: RunsHarness) -> None:
    active = await project(runs, "可见结算项目")
    other = await project(runs, "隔离项目")
    scope = {"project_id": str(active["id"])}
    url = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"

    async def index(name: str, key: str, files: list[dict[str, str]], selected: dict[str, str]) -> str:
        response = await runs.client.post(
            f"{url}/codebases/local", params=selected, headers=runs.headers(), json={
                "source_key": key * 64, "name": name, "candidates": len(files), "files": files,
            },
        )
        assert response.status_code == 200, response.text
        return str(response.json()["codebase_id"])

    files = [
        {"path": "redline_entry.py", "text": (
            "from quartz_stage import step\ndef run():\n    return step()\n"
            "def cursor_advance(): return 1\ndef finalize_checkpoint(): return 2\n"
        )},
        {"path": "quartz_stage.py", "text": "from ledger_kernel import value\ndef step():\n    return value()\n"},
        {"path": "ledger_kernel.py", "text": "def value():\n    return 7\n"},
        {"path": "unused.py", "text": "def never_in_chain():\n    return 9\n"},
    ]
    code = await index("可见结算服务", "a", files, scope)
    await index("hiddenbillingservice", "b", files, {"project_id": str(other["id"])})
    design = f"{url}/codebases/local/{code}/design"
    plan = (await runs.client.get(design, params=scope)).json()
    for name, body in (
        ("_domains.json", '{"domain":"结算","description":"计算账单","keywords":["billing"]}'),
        ("architecture.md", "# 账单服务架构\n\nsettlementarchitecturepath"),
    ):
        answer = await runs.client.post(
            design, params=scope, headers=runs.headers(), json={"basis": plan["basis"], "name": name, "body": body},
        )
        assert answer.status_code == 200, answer.text

    recalled = await runs.client.post(f"{url}/recall", params=scope, headers=runs.headers(), json={
        "query": "nonexistentroute", "session": "route-only", "depth": "route", "recall_mode": True,
    })
    assert recalled.status_code == 200, recalled.text
    route = recalled.json()
    assert route["items"] == [] and "run_id" not in route
    [repository] = route["code_route"]
    assert repository["name"] == "可见结算服务"
    assert repository["domain"] == "结算" and repository["keywords"] == ["billing"]
    endpoint = f"{url}/{repository['entry_id']}"
    assert (await runs.client.get(endpoint, params={"project_id": str(other["id"])})).status_code == 404
    shared = await runs.client.get(url, params={"query": "nonexistentroute", "depth": "route"})
    assert shared.json()["code_route"] == []

    checked = await runs.client.get(
        url, params=scope | {"query": "nonexistentroute", "depth": "route", "check": "true"},
    )
    assert checked.json() == {"relevant": True}
    explained = await runs.client.get(url, params=scope | {
        "query": "redline_entry.py cursor_advance finalize_checkpoint",
        "depth": "lookup", "check": True, "check_details": True,
    })
    file_evidence = explained.json()
    assert file_evidence["relevant"] and file_evidence["threshold"] == 4, file_evidence
    assert file_evidence["title"] == "redline_entry.py" and file_evidence["sources"] == [{"path": "redline_entry.py"}]
    assert "body" not in file_evidence and "excerpt" not in file_evidence
    context = await runs.client.get(url, params=scope | {"query": "settlementarchitecturepath", "depth": "context"})
    assert [row["source"]["ref"] for row in context.json()["items"]] == ["architecture.md"]
    compact = await runs.client.get(url, params=scope | {"query": "redline_entry", "depth": "context"})
    assert all(not row["source"]["path"] for row in compact.json()["items"])

    async with runs.sessions() as db:
        assert await db.scalar(
            select(KnowledgeRecallRun.id).where(KnowledgeRecallRun.workspace_id == UUID(runs.workspace_id)),
        ) is None
    found = await runs.client.post(f"{url}/recall", params=scope, headers=runs.headers(), json={
        "query": "redline_entry", "session": "lookup-fixture", "depth": "lookup", "recall_mode": True,
    })
    assert found.status_code == 200, found.text
    lookup = found.json()
    assert "run_id" in lookup and len(lookup["items"]) <= 3
    primary = next((row for row in lookup["items"] if row["source"]["path"] == "redline_entry.py"), None)
    assert primary is not None, lookup
    assert "## 调用引用" in primary["excerpt"]
    sibling = primary["related_files"][0]
    assert sibling["path"] == "quartz_stage.py"
    assert {row["path"] for row in lookup["candidate_change_files"]} == {"quartz_stage.py"}
    change = lookup["change_entry_points"]
    assert [row["path"] for row in change["files"]] == ["redline_entry.py"]
    assert {(edge["from"]["path"], edge["to"]["path"], edge["kind"]) for edge in change["edges"]} == {
        ("redline_entry.py", "quartz_stage.py", "DEPENDS_ON"),
        ("redline_entry.py", "quartz_stage.py", "REFERENCES"),
    }
    accepted = await runs.client.post(
        f"{url}/{sibling['id']}/open", params=scope, headers=runs.headers(), json={"session": "lookup-fixture"},
    )
    assert accepted.status_code == 200, accepted.text

    # 只用命中文件作为起点，验证第二跳来自依赖，目录包含关系不扩大至兄弟文件。
    async with runs.sessions() as db:
        scoped = Scope(UUID(runs.workspace_id), UUID(str(active["id"])), UUID(runs.user_id), "owner")
        visible = published(scoped)
        matches = await rank_published(
            db, visible, scoped.workspace, "redline_entry.py", recall=True,
            hidden=overridden(scoped), with_coverage=True,
        )
        anchor = next(hit for hit in matches if str(hit.candidate.entry) == primary["id"])
        # 精确模块查询应与普通查询使用相同的可见代码语料。
        corpus = visible.with_only_columns(KnowledgeRevision.search_length).where(KnowledgeEntry.kind == "code")
        stats = corpus.subquery()
        population, length = (await db.execute(select(func.count(), func.sum(stats.c.search_length)))).one()
        assert (anchor.candidate.population, anchor.candidate.corpus_length) == (population, length)
        assert anchor.candidate.named_module
        graph, links = await lookup_graph(db, visible, [anchor])
        neighbors = [hit for hit in graph if not hit.candidate.matches]
        assert [hit.code_weight for hit in neighbors] == [0.8, 0.4]
        assert [hit.score for hit in neighbors] == [2 * log2(1.8), 2 * log2(1.4)]
        assert all(not hit.relevant for hit in neighbors)
        assert links[anchor.candidate.entry] == [sibling]


async def test_context_follows_published_source_declarations_to_related_modules(runs: RunsHarness) -> None:
    active = await project(runs, "代码上下文关系")
    other = await project(runs, "不相干的项目")
    scope = {"project_id": str(active["id"])}
    url = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    term = "checkpointbeacon"

    async def index(dependency: bool) -> None:
        imports = "from calculation.core import rate\n" if dependency else ""
        answer = "rate()" if dependency else "0"
        files = [
            {"path": "switchboard.py", "text": f"{imports}def {term}():\n    return {answer}\n"},
            {"path": "calculation/core.py", "text": (
                "from currency.provider import quote\ndef rate(): return quote()\n"
                + "\n".join(f"def fee_{number}(): return 1" for number in range(5))
            )},
            {"path": "currency/provider.py", "text": (
                "def quote(): return 7\n" + "\n".join(f"def currency_{number}(): return 1" for number in range(5))
            )},
        ]
        result = await runs.client.post(f"{url}/codebases/local", params=scope, headers=runs.headers(), json={
            "source_key": "c" * 64, "name": "结算模块", "candidates": len(files), "files": files,
            "incremental": True,
        })
        assert result.status_code == 200, result.text

    async def context() -> tuple[set[str], bool]:
        response = await runs.client.get(url, params=scope | {"query": term, "depth": "context"})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert all(not row["source"]["path"] for row in items)
        return {row["source"]["ref"] for row in items}, any(row["relevant"] is True for row in items)

    await index(True)
    assert await context() == ({"结算模块", "modules/calculation.md", "modules/currency.md"}, True)
    checked = await runs.client.get(url, params=scope | {"query": term, "depth": "context", "check": "true"})
    assert checked.json() == {"relevant": True}
    unrelated = await runs.client.get(url, params={
        "project_id": str(other["id"]), "query": term, "depth": "context", "check": "true",
    })
    assert unrelated.json() == {"relevant": False}

    await index(False)
    assert await context() == ({"结算模块"}, True)


async def test_context_precheck_uses_only_published_module_sources(runs: RunsHarness) -> None:
    scope = {"project_id": str((await project(runs, "模块预检出处"))["id"])}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    consumer = {
        "path": "savings/charge_processor.py",
        "text": "from savings.record_map import polish_quasar\n"
                "def charge_invoice():\n    return polish_quasar()\ndef verify_invoice(): return 3\n",
    }
    dependency = {
        "path": "savings/record_map.py",
        "text": "\n".join(f"def {symbol}(): return 1" for symbol in (
            "polish_quasar", "align_transmission", "select_signalling_key", "connect_field_slot",
            "solar_quanta", "increment_polarity", "recalculate_balancer",
        )),
    }

    async def precheck_after_index(files: list[dict[str, str]]) -> dict[str, object]:
        indexed = await runs.client.post(f"{root}/codebases/local", params=scope, headers=runs.headers(), json={
            "source_key": "e" * 64, "name": "service", "candidates": len(files), "files": files, "incremental": True,
        })
        assert indexed.status_code == 200, indexed.text
        checked = await runs.client.get(root, params=scope | {
            "query": "savings charge_invoice verify_invoice", "depth": "context",
            "recall_mode": True, "check": True, "check_details": True,
        })
        assert checked.status_code == 200, checked.text
        result: dict[str, object] = checked.json()
        assert result["relevant"] and "excerpt" not in result, result
        return result

    module = await precheck_after_index([consumer, dependency])
    assert module["title"] == "service · savings 模块"
    assert module["sources"] == [{"path": consumer["path"]}, {"path": dependency["path"]}]
    # 索引更新后仓库概览也可能成为首位，已经撤下的源码不得再出现在出处中。
    updated = await precheck_after_index([consumer])
    assert updated["sources"] in ([], [{"path": consumer["path"]}])


async def test_change_paths_bound_candidate_files_and_forward_edges(runs: RunsHarness) -> None:
    scope = {"project_id": str((await project(runs, "候选文件上限"))["id"])}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    leaves = [f"dependency_{number:02d}" for number in range(12)]
    imports = "\n".join(f"import {name}" for name in leaves)
    files = [
        {
            "path": "startup.py", "text": (
                imports + "\ndef initialize_cluster_gate(): return 1\n"
                "def validate_startup_barrier(): return 2\ndef normalize_job_arguments(): return 3\n"
            ),
        },
        *({"path": f"{name}.py", "text": f"def step_{number:02d}(): return 1"} for number, name in enumerate(leaves)),
    ]
    indexed = await runs.client.post(f"{root}/codebases/local", params=scope, headers=runs.headers(), json={
        "source_key": "8" * 64, "name": "startup", "candidates": len(files), "files": files,
    })
    assert indexed.status_code == 200, indexed.text
    response = await runs.client.get(root, params=scope | {
        "query": "startup.py initialize_cluster_gate validate_startup_barrier normalize_job_arguments",
        "depth": "lookup", "recall_mode": True, "limit": 1,
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert [row["path"] for row in result["change_entry_points"]["files"]] == ["startup.py"]
    assert len(result["candidate_change_files"]) == len(result["change_entry_points"]["edges"]) == 10
    assert result["omitted_candidate_change_files"] == result["change_entry_points"]["omitted_edges"] == 2


async def test_shortlist_uses_raw_code_weight_before_merged_order(runs: RunsHarness) -> None:
    active = await project(runs, "召回短名单")
    scope = {"project_id": str(active["id"])}
    url = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    vocabulary = [
        "argent", "cobalt", "copper", "indium", "iridium", "krypton", "neon", "nickel", "niobium", "osmium",
        "platinum", "radium", "rhodium", "rubidium", "silicon", "tantalum", "tellurium", "terbium", "titanium",
        "tungsten", "vanadium", "yttrium", "zinc", "zirconium",
    ]
    query = " ".join(["named_focus", *vocabulary])

    def file(path: str, terms: list[str]) -> dict[str, str]:
        return {"path": path, "text": "\n".join(f"def {term}():\n    return 1" for term in terms)}

    files = [
        file("tall.py", vocabulary),
        file("next.py", vocabulary[:22]),
        file("third.py", vocabulary[:20]),
        file("named_focus.py", vocabulary[:17]),
    ]
    # 用同等长度的无关文件拉开原始分，让前四条的展示分均达到上限。
    filler = [f"fallbackword{index}" for index in range(20)]
    files.extend(file(f"background_{index}.py", filler) for index in range(74))
    for key, entries in (("a", [files[0], *files[2:40]]), ("b", [files[1], *files[40:]])):
        indexed = await runs.client.post(f"{url}/codebases/local", params=scope, headers=runs.headers(), json={
            "source_key": key * 64, "name": f"原始分验收仓库 {key}", "candidates": len(entries), "files": entries,
        })
        assert indexed.status_code == 200, indexed.text
    note = await runs.client.post(url, params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": " ".join(vocabulary[:12]), "body": "团队维护的排查建议。",
    })
    assert note.status_code == 201, note.text
    note_id = note.json()["id"]

    broad = await runs.client.get(url, params=scope | {"query": query})
    assert broad.status_code == 200, broad.text
    normal = {row["source"].get("path"): row for row in broad.json()["items"] if row["kind"] == "code"}
    expected = {"tall.py", "next.py", "third.py"}
    assert all(normal[path]["score"] == 10 for path in expected | {"named_focus.py"})
    assert "named_focus.py" in tuple(normal)[:3]

    recalled = await runs.client.post(f"{url}/recall", params=scope, headers=runs.headers(), json={
        "query": query, "depth": "lookup", "recall_mode": True, "session": "shortlist-proof", "limit": 5,
    })
    assert recalled.status_code == 200, recalled.text
    result = recalled.json()
    codes = {row["source"]["path"] for row in result["items"] if row["kind"] == "code"}
    assert codes == expected, result
    assert note_id in {row["id"] for row in result["items"][:3]}
    async with runs.sessions() as db:
        entry_ids = await db.scalars(select(KnowledgeRecallRunHit.entry_id).where(
            KnowledgeRecallRunHit.run_id == UUID(result["run_id"]),
        ))
        assert {str(value) for value in entry_ids} == {row["id"] for row in result["items"]}
