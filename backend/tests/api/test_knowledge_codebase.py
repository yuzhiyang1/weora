"""使用真实 Git 对象验证代码知识的发布、增量和读权限。"""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from sqlalchemy import select, update

from tests.api.test_projects import project, projects_path, repository, repository_input
from tests.api.test_repository_sync import FIXTURE_URL, LocalFixtureGit
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.domain import codebase_id
from weora.modules.knowledge.models import KnowledgeEvent
from weora.modules.knowledge.search import Candidate, SearchHit, diversify_code
from weora.modules.knowledge.search_tokens_v2 import QueryWords
from weora.modules.projects.git_reader import GitRepositoryReader
from weora.modules.projects.persistence import RepositorySyncModel
from weora.modules.projects.repository_sync import RepositorySync

pytestmark = [pytest.mark.anyio, pytest.mark.skipif(not shutil.which("git"), reason="需要实际 Git 命令")]


async def test_rule_paths_derive_project_edges_until_a_glob_becomes_ambiguous(runs: RunsHarness) -> None:
    project_id = str((await project(runs))["id"])
    other_id = str((await project(runs, "其他项目"))["id"])
    scope = {"project_id": project_id}
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"

    async def sync(key: str, project: str, *files: str) -> None:
        result = await runs.client.post(
            f"{path}/codebases/local", params={"project_id": project}, headers=runs.headers(),
            json={
                "source_key": key * 64, "name": key * 8, "candidates": len(files),
                "files": [{"path": name, "text": "export const active = true;"} for name in files],
            },
        )
        assert result.status_code == 200, result.text

    async def review(entry: str, params: dict[str, str], revision: int = 1) -> None:
        accepted = await runs.client.post(f"{path}/{entry}/review", params=params, headers=runs.headers(),
                                         json={"decision": "accept", "revision": revision})
        assert accepted.status_code == 200, accepted.text

    async def get(entry: str) -> httpx.Response:
        return await runs.client.get(f"{path}/{entry}", params=scope)

    await sync("a", project_id, "src/pay/receipts.ts")
    await sync("b", project_id, "src/shop/till.ts")
    await sync("c", other_id, "src/pay/other.ts")
    published = await runs.client.get(path, params=scope)
    pages = {row["source"]["path"]: row["id"] for row in published.json()["items"] if row["source"].get("path")}
    rule_body = "---\npaths: src/{pay,shop}/**\n---\n账单处理规则。"
    rule_input = {"kind": "rule", "title": "Billing policy", "body": rule_body}
    created = await runs.client.post(path, params=scope, headers=runs.headers(),
                                     json=rule_input | {"operation_id": str(uuid4())})
    assert created.status_code == 201, created.text
    rule = created.json()["id"]
    assert created.json()["rule_paths"] == ["src/{pay,shop}/**"] and created.json()["relations"] == []
    await review(rule, scope)
    recalled = await runs.client.get(path, params=scope | {"query": "Billing policy", "recall_mode": "true"})
    rows = recalled.json()["items"]
    assert [row["id"] for row in rows] == [rule]
    assert {link["target"] for link in rows[0]["relations"]} == set(pages.values())

    shared = await runs.client.post(path, headers=runs.headers(), json=rule_input | {"operation_id": str(uuid4())})
    await review(shared.json()["id"], {})
    assert (await get(shared.json()["id"])).json()["relations"] == []
    reverse = (await get(pages["src/pay/receipts.ts"])).json()["relations"]
    assert {link["target"] for link in reverse if link["kind"] == "MAPS_TO"} == {rule}
    report = await runs.client.get(f"{path}/health", params=scope)
    assert report.json()["graph"]["relations"] == 2

    # 多了一个匹配同一分支的可见仓库，只撤下 pay 分支。项目只搜到规则时也按全池判断。
    await sync("b", project_id, "src/shop/till.ts", "src/pay/decoy.ts")
    assert {link["target"] for link in (await get(rule)).json()["relations"]} == {pages["src/shop/till.ts"]}
    assert all(link["kind"] != "MAPS_TO" for link in (await get(pages["src/pay/receipts.ts"])).json()["relations"])
    revised = await runs.client.post(f"{path}/{rule}/revisions", params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "kind": "rule", "title": "Billing policy", "body": "明确关联收银代码。",
        "base_revision": 1, "relations": [{"kind": "REFERENCES", "target": pages["src/shop/till.ts"]}],
    })
    assert revised.status_code == 201, revised.text
    assert revised.json()["rule_paths"] == ["src/{pay,shop}/**"]
    await review(rule, scope, 2)
    links = (await get(rule)).json()["relations"]
    assert [(link["kind"], link["target"]) for link in links] == [("REFERENCES", pages["src/shop/till.ts"])]
    report = await runs.client.get(f"{path}/health", params=scope)
    assert report.json()["graph"]["relations"] == 1


async def test_code_recall_recognizes_the_whole_named_repository(runs: RunsHarness) -> None:
    scope = {"project_id": str((await project(runs))["id"])}
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    for name, key in (("zeta-worker", "a"), ("zeta-worker-api", "b")):
        indexed = await runs.client.post(f"{path}/codebases/local", params=scope, headers=runs.headers(), json={
            "source_key": key * 64, "name": name, "candidates": 1,
            "files": [{"path": "dispatch.py", "text": "def zeta_worker(): return 1"}],
        })
        assert indexed.status_code == 200, indexed.text
    response = await runs.client.get(path, params=scope | {"query": "zeta-worker", "recall_mode": "true"})
    assert response.status_code == 200, response.text
    overviews = [row for row in response.json()["items"] if row["source"]["path"] == ""]
    assert [row["title"] for row in overviews] == ["zeta-worker · 代码概览", "zeta-worker-api · 代码概览"]
    assert {row["relevant"] for row in overviews} == {False}


def test_code_diversity_preserves_named_module_and_only_demotes() -> None:
    project_id, primary, secondary, tertiary = uuid4(), uuid4(), uuid4(), uuid4()

    def hit(source: UUID | None, *, named: bool = False, relevant: bool = True) -> SearchHit:
        candidate = Candidate(
            uuid4(), 1, "code" if source else "learning", source, project_id, "neutral",
            1, 1, 1, 1, datetime.now(UTC), hidden=False, named_module=named,
        )
        return SearchHit(candidate, 4, 4, relevant, QueryWords(("module",), ()), threshold=4.0)

    first, second, third = hit(primary), hit(primary), hit(primary)
    note, followup, unrelated = hit(None), hit(None), hit(None, relevant=False)
    assert diversify_code([first, second, third, note, followup]) == [first, second, note, followup, third]
    assert diversify_code([note, first, second, third, unrelated]) == [note, first, second, third, unrelated]
    assert diversify_code([first, second, third, unrelated]) == [first, second, third, unrelated]

    other, ordinary, named = hit(secondary), hit(tertiary), hit(tertiary, named=True)
    assert diversify_code([first, other, ordinary, note]) == [first, other, note, ordinary]
    assert diversify_code([first, other, named, note]) == [first, other, named, note]


async def test_local_checkout_reuses_verified_repository_and_preserves_distinct_evidence(
    runs: RunsHarness, tmp_path: Path,
) -> None:
    upstream = tmp_path / "checkout-origin"
    upstream.mkdir()
    runner = LocalFixtureGit(upstream)
    reader = GitRepositoryReader(tmp_path / "checkout-cache", runner=runner)
    env = reader._base_env(upstream)
    await runner.run(upstream, env, "init", "--initial-branch=main")
    source = "def verified_checkout():\n    return 1\n"
    (upstream / "billing.py").write_bytes(source.encode())
    await runner.run(upstream, env, "add", "-A")
    await runner.run(upstream, env, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-m", "checkout fixture")
    commit = (await runner.run(upstream, env, "rev-parse", "HEAD")).decode().strip()
    service = runs.app.state.project_service
    service.reader = reader
    sync = RepositorySync(
        runs.sessions, reader, service.repository_source, timeout_seconds=30,
        interval_seconds=300, max_age_seconds=900,
    )
    service.repository_sync = sync
    p, other = await project(runs, "登记项目"), await project(runs, "另一个项目")
    repo = await repository(runs, p["id"], "账单仓库", git_url=FIXTURE_URL)
    project_id, repo_id = str(p["id"]), str(repo["id"])
    scope = {"project_id": project_id}
    repo_path = f"{projects_path(runs)}/{project_id}/repositories/{repo_id}"
    knowledge = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    indexes = f"{knowledge}/codebases"
    checked = await runs.client.post(f"{repo_path}/check", headers=runs.headers(), json={"version": repo["version"]})
    assert checked.json()["status"] == "connected", checked.text
    assert await sync.run_one()
    network = runner.network_calls.copy()

    provenance = {
        "origin": "ssh://git@93.184.216.34:22/test/source.git",
        "commit": commit, "subdir": "", "dirty": False,
    }

    async def submit(
        key: str, text: str, checkout: dict[str, object] | None, *, project: str = project_id,
    ) -> dict[str, object]:
        response = await runs.client.post(
            f"{indexes}/local", params={"project_id": project}, headers=runs.headers(),
            json={
                "source_key": key * 64, "name": "billing", "incremental": True, "candidates": 1,
                "files": [{"path": "billing.py", "text": text}], "checkout": checkout,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    before = await submit("a", source, None)
    reused = await submit("a", source, provenance)
    assert reused["repository_id"] == repo_id and reused["revision"] == commit and reused["extracted"] == 1
    assert isinstance(binding := reused["checkout"], dict) and binding["status"] == "reused"
    assert (await submit("b", source, provenance))["codebase_id"] == reused["codebase_id"]
    listing = (await runs.client.get(indexes, params=scope)).json()["items"]
    assert [row["repository_id"] for row in listing] == [repo_id]
    for identity in (before["codebase_id"], reused["codebase_id"]):
        status = (await runs.client.get(f"{indexes}/local/{identity}", params=scope | {"lint": "true"})).json()
        assert status["repository_id"] == repo_id and status["problems"] == []

    changed = source.replace("verified_checkout", "device_override")
    tampered = await submit("a", changed, provenance)
    assert tampered["codebase_id"] == before["codebase_id"] and tampered["revision"] != commit
    assert isinstance(local_link := tampered["checkout"], dict) and local_link["status"] == "linked"
    repeated = await submit("a", changed, provenance)
    assert repeated["revision"] == tampered["revision"] and repeated["reused"] == 1
    modified = await submit("a", changed, provenance | {"dirty": True})
    assert modified["revision"] != repeated["revision"]
    assert isinstance(modified_link := modified["checkout"], dict) and modified_link["tree_state"] == "modified"
    subdir = await submit("c", source, provenance | {"subdir": "src"})
    assert subdir["codebase_id"] not in {before["codebase_id"], reused["codebase_id"]}
    assert isinstance(subdir_link := subdir["checkout"], dict) and subdir_link["subdir"] == "src"
    isolated = await submit("a", source, provenance, project=str(other["id"]))
    assert isinstance(isolated_link := isolated["checkout"], dict)
    assert isolated_link["status"] == "unregistered" and "repository_id" not in isolated_link
    detail = (await runs.client.get(knowledge, params=scope | {"query": "device_override"})).json()["items"]
    assert detail[0]["source"]["checkout"] == modified_link
    assert "origin" not in detail[0]["source"]["checkout"]
    assert (await runs.client.get(f"{indexes}/{repo_id}", params=scope)).json()["revision"] == commit
    plan = await runs.client.get(f"{indexes}/{repo_id}/design", params=scope)
    assert plan.status_code == 200, plan.text
    basis = plan.json()["basis"]
    for identity in (before["codebase_id"], reused["codebase_id"]):
        other_plan = await runs.client.get(f"{indexes}/local/{identity}/design", params=scope)
        if identity == reused["codebase_id"]:
            assert other_plan.json()["basis"] == basis
        else:
            assert other_plan.json()["basis"] != basis  # 本地副本已有独立改动。
    architecture = await runs.client.post(
        f"{indexes}/{repo_id}/design", params=scope, headers=runs.headers(),
        json={"basis": basis, "name": "architecture.md", "body": "# verifiedrepositorydesign"},
    )
    assert architecture.status_code == 200, architecture.text
    architecture_id = architecture.json()["items"][0]["id"]
    assert (await runs.client.get(f"{knowledge}/{architecture_id}", params=scope)).json()["source"]["commit"] == commit
    await submit("c", "def checkout_subdir_only(): pass", provenance | {"subdir": "src"})
    consumer = await submit("d", "from graph_ports import verified_checkout", None)
    ident = uuid5(UUID(str(consumer["codebase_id"])), "billing.py")
    cross = await runs.client.get(f"{knowledge}/{ident}", params=scope)
    assert any(item.get("derived") == "cross_repository" and item["target_revision"] == commit
               for item in cross.json()["relations"])
    assert runner.network_calls == network

    disabled = await runs.client.put(
        repo_path, json=repository_input(repo, status="disabled"), headers=runs.headers(),
    )
    assert disabled.status_code == 200, disabled.text
    local_status = (await runs.client.get(f"{indexes}/local/{tampered['codebase_id']}", params=scope)).json()
    assert local_status["checkout"] == {
        "status": "connection_changed", "commit": commit, "subdir": "", "tree_state": "modified",
    }
    recalled = (await runs.client.get(knowledge, params=scope | {"query": "device_override"})).json()["items"]
    assert recalled[0]["source"]["checkout"] == local_status["checkout"]
    assert (await runs.client.get(f"{indexes}/local/{reused['codebase_id']}", params=scope)).status_code == 404
    assert (await runs.client.get(f"{knowledge}/{architecture_id}", params=scope)).status_code == 404
    after = await runs.client.get(f"{knowledge}/{ident}", params=scope)
    assert all(item.get("derived") != "cross_repository" for item in after.json()["relations"])


async def test_codebase_incremental_publishes_graph_and_preserves_authorized_evidence(
    runs: RunsHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_url = "https://github.com/test/source.git"
    monkeypatch.setattr("tests.api.test_repository_sync.FIXTURE_URL", fixture_url)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    runner = LocalFixtureGit(upstream)
    reader = GitRepositoryReader(tmp_path / "cache", runner=runner)
    env = reader._base_env(upstream)
    await runner.run(upstream, env, "init", "--initial-branch=main")
    service = runs.app.state.project_service
    service.reader = reader
    sync = RepositorySync(
        runs.sessions,
        reader,
        service.repository_source,
        timeout_seconds=30,
        interval_seconds=300,
        max_age_seconds=900,
    )
    service.repository_sync = sync
    p, other = await project(runs, "账单服务"), await project(runs, "其他项目")
    repo = await repository(runs, p["id"], "代码账单", git_url=fixture_url)
    project_id, repo_id = str(p["id"]), str(repo["id"])
    repo_path = f"{projects_path(runs)}/{project_id}/repositories/{repo_id}"
    knowledge = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    code = f"{knowledge}/codebases/{repo_id}"
    scope = {"project_id": project_id}

    async def commit() -> str:
        await runner.run(upstream, env, "add", "-A")
        await runner.run(
            upstream,
            env,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        )
        return (await runner.run(upstream, env, "rev-parse", "HEAD")).decode().strip()

    async def index(incremental: bool = True) -> dict[str, object]:
        result = await runs.client.post(code, params=scope, headers=runs.headers(), json={"incremental": incremental})
        assert result.status_code == 200, result.text
        return result.json()  # type: ignore[no-any-return]

    async def find(query: str) -> list[dict[str, object]]:
        result = await runs.client.get(knowledge, params=scope | {"query": query})
        assert result.status_code == 200, result.text
        return result.json()["items"]  # type: ignore[no-any-return]

    (upstream / "app.py").write_text(
        "from pricing import calculate\n\ndef process_order():\n    return calculate()\n",
        encoding="utf-8",
    )
    (upstream / "pricing.py").write_text("def calculate():\n    return 1\n", encoding="utf-8")
    (upstream / "obsolete.ts").write_text("export function obsoleteField() {}\n", encoding="utf-8")
    (upstream / "contract.ts").write_text("export interface BillingPort {}\n", encoding="utf-8")
    (upstream / "adapter.ts").write_text(
        "import type { BillingPort as Port } from './contract'\n"
        "interface LocalPort {}\n"
        "export class BillingAdapter implements Port, LocalPort {}\n",
        encoding="utf-8",
    )
    (upstream / ".env.py").write_text("def privateFixture():\n    pass\n", encoding="utf-8")
    first = await commit()
    checked = await runs.client.post(repo_path + "/check", headers=runs.headers(), json={"version": repo["version"]})
    assert checked.json()["status"] == "connected", checked.text
    assert await sync.run_one()
    transport_before = runner.network_calls.copy()
    assert (await runs.client.get(code, params={"project_id": str(other["id"])})).status_code == 404
    reviews = [f"https://github.com/test/source/pull/{number}" for number in (4, 5)]
    unindexed = await runs.client.post(
        f"{knowledge}/codebases/review-plan",
        params=scope,
        json={"urls": reviews},
        headers=runs.headers(),
    )
    assert unindexed.json() == {"targets": [], "skipped": [{"url": url, "code": "INDEX_MISSING"} for url in reviews]}
    initial = await index(incremental=False)
    assert initial["revision"] == first and initial["extracted"] == 5
    assert (await index())["extracted"] == 0
    assert runner.network_calls == transport_before

    connection = initial["connection_version"]
    assert isinstance(connection, int)
    overview_id = uuid5(codebase_id(UUID(repo_id), connection), "")
    overview_path = f"{knowledge}/{overview_id}"
    endpoint = f"{code}/design"
    base = (await runs.client.get(overview_path, params=scope)).json()
    draft = (await runs.client.get(endpoint, params=scope)).json()
    assert "app.py" in draft["repository_narrative_context"]["indexed_files"]
    offer = {
        "basis": draft["basis"], "name": "_repository-overview.md",
        "body": "# 计价导读\n\nnarrativeproofmarker · 核心路径由费率决定。",
    }
    invalid = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json=offer | {"body": "无标题"})
    assert invalid.status_code == 422
    published = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json=offer)
    assert published.status_code == 200, published.text
    assert published.json()["items"][0]["id"] == str(overview_id)
    merged = (await runs.client.get(overview_path, params=scope)).json()
    assert merged["body"].startswith(base["body"].rstrip())
    assert merged["body"].count("## AI 架构导读") == 1
    assert merged["relations"] == base["relations"]
    assert merged["revision"] == base["revision"] + 1
    assert any(row["id"] == str(overview_id) for row in await find("narrativeproofmarker"))
    saved = (await runs.client.get(endpoint, params=scope)).json()
    assert saved["repository_narrative_context"] == {}
    assert saved["documents"][offer["name"]]["id"] == str(overview_id)
    repeated = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json=offer)
    assert repeated.json()["reused"] is True
    await index(incremental=False)
    reindexed = (await runs.client.get(overview_path, params=scope)).json()
    assert (reindexed["revision"], reindexed["body"]) == (merged["revision"], merged["body"])
    graph = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json={"basis": draft["basis"]})
    assert graph.status_code == 200, graph.text
    graph_catalog = f"{knowledge}/codebases/graph-documents/g2"
    [call_chain] = (await runs.client.get(graph_catalog, params=scope)).json()["items"]
    assert call_chain["repository_id"] == repo_id
    assert call_chain["path"] == "teamwiki/evidence/code/github__test__source/docs/graph-g2-dataflow.md"
    opened = await runs.client.get(f"{knowledge}/by-path", params=scope | {"path": call_chain["path"]})
    assert opened.status_code == 200 and "calculate" in opened.json()["body"], opened.text
    assert opened.json()["id"] == call_chain["entry_id"]
    assert runner.network_calls == transport_before

    app = next(row for row in await find("process_order") if row["title"] == "app.py")
    pricing = next(row for row in await find("calculate") if row["title"] == "pricing.py")
    obsolete = next(row for row in await find("obsoleteField") if row["title"] == "obsolete.ts")
    edges, source = app["relations"], app["source"]
    assert isinstance(edges, list) and isinstance(source, dict)
    assert {"kind": "DEPENDS_ON", "direction": "out", "target": pricing["id"], "title": "pricing.py"} in edges
    assert {"kind": "REFERENCES", "direction": "out", "target": pricing["id"], "title": "pricing.py"} in edges
    caller_detail = (await runs.client.get(f"{knowledge}/{app['id']}", params=scope)).json()
    assert "`calculate()` → `pricing.py`（第 4 行）" in caller_detail["body"]
    pricing_detail = (await runs.client.get(f"{knowledge}/{pricing['id']}", params=scope)).json()
    assert {"kind": "REFERENCES", "direction": "in", "target": app["id"], "title": "app.py"} in (
        pricing_detail["relations"]
    )
    assert source["freshness"] == "current"
    adapter = next(row for row in await find("BillingAdapter") if row["title"] == "adapter.ts")
    contract = next(row for row in await find("BillingPort") if row["title"] == "contract.ts")
    info = (await runs.client.get(f"{knowledge}/{adapter['id']}", params=scope)).json()
    implementation_edges = info["relations"]
    assert {"kind": "IMPLEMENTS", "direction": "out", "target": contract["id"], "title": "contract.ts"} in (
        implementation_edges
    )
    assert {"kind": "IMPLEMENTS", "direction": "out", "target": adapter["id"], "title": "adapter.ts"} in (
        implementation_edges
    )
    assert "`BillingAdapter implements Port` → `contract.ts`（第 3 行）" in info["body"]
    incoming = (await runs.client.get(f"{knowledge}/{contract['id']}", params=scope)).json()["relations"]
    assert {"kind": "IMPLEMENTS", "direction": "in", "target": adapter["id"], "title": "adapter.ts"} in incoming
    assert await find("privateFixture") == []
    assert (await runs.client.get(knowledge, params={"query": "process_order"})).json()["items"] == []
    assert (await runs.client.get(f"{knowledge}/{app['id']}")).status_code == 404

    (upstream / "pricing.py").write_text("def calculate():\n    return 3\n", encoding="utf-8")
    (upstream / "obsolete.ts").unlink()
    second = await commit()
    async with runs.sessions() as db, db.begin():
        await db.execute(
            update(RepositorySyncModel)
            .where(RepositorySyncModel.repository_id == UUID(repo_id))
            .values(
                next_sync_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    assert await sync.run_one()
    for item, expected in ((app, "current"), (pricing, "stale"), (obsolete, "missing")):
        detail = await runs.client.get(f"{knowledge}/{item['id']}", params=scope)
        assert detail.json()["source"]["freshness"] == expected, detail.text
    stale = await runs.client.get(code, params=scope)
    assert stale.json()["freshness"] == "stale" and stale.json()["source_revision"] == second

    fresh = await index()
    assert (fresh["revision"], fresh["reused"], fresh["extracted"], fresh["removed"]) == (second, 3, 1, 1)
    assert (await runs.client.get(graph_catalog, params=scope)).json()["items"] == []
    assert await find("narrativeproofmarker") == []
    obsolete_offer = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json=offer)
    assert obsolete_offer.status_code == 409
    without_narrative = (await runs.client.get(overview_path, params=scope)).json()
    assert without_narrative["revision"] == merged["revision"] + 1
    assert "## AI 架构导读" not in without_narrative["body"]
    app_after = (await runs.client.get(f"{knowledge}/{app['id']}", params=scope)).json()
    assert app_after["revision"] == app["revision"] and app_after["source"]["commit"] == first
    pricing_after = (await runs.client.get(f"{knowledge}/{pricing['id']}", params=scope)).json()
    assert pricing_after["revision"] == 2 and pricing_after["source"]["commit"] == second
    assert (await runs.client.get(f"{knowledge}/{obsolete['id']}", params=scope)).status_code == 404
    lint = (await runs.client.get(code, params=scope | {"lint": "true"})).json()
    assert lint["problems"] == [] and lint["freshness"] == "current"
    list_all = await runs.client.get(f"{knowledge}/codebases", params=scope | {"lint": "true"})
    assert list_all.status_code == 200 and list_all.json()["items"] == [lint]
    source_plan = await runs.client.post(
        f"{knowledge}/codebases/repo-plan",
        params=scope,
        headers=runs.headers(),
        json={"sources": [fixture_url.removesuffix(".git"), repo_id]},
    )
    assert source_plan.status_code == 200
    assert [row["repository_id"] for row in source_plan.json()["items"]] == [repo_id, repo_id]
    operation = {"operation_id": str(uuid4()), "expected_connection_version": repo["connection_version"]}
    rejected = await runs.client.post(
        code,
        params=scope,
        headers=runs.headers(),
        json=operation | {"expected_connection_version": 99},
    )
    assert rejected.status_code == 409
    once = await runs.client.post(code, params=scope, json=operation, headers=runs.headers())
    replay = await runs.client.post(code, params=scope, json=operation, headers=runs.headers())
    assert replay.status_code == once.status_code == 200 and replay.json() == once.json()
    reused_key = await runs.client.post(code, params=scope, headers=runs.headers(), json=operation | {"max_files": 1})
    assert reused_key.status_code == 409
    async with runs.sessions() as db:
        removed = await db.scalar(select(KnowledgeEvent.reason).where(
            KnowledgeEvent.entry_id == UUID(obsolete["id"]), KnowledgeEvent.action == "code_removed",
        ))
        assert removed == second

    unrelated = "https://github.com/test/other/pull/6"
    planned = await runs.client.post(
        f"{knowledge}/codebases/review-plan",
        params=scope,
        json={"urls": [*reviews, unrelated]},
        headers=runs.headers(),
    )
    assert planned.status_code == 200 and planned.json() == {
        "targets": [{"repository_id": repo_id, "urls": reviews}],
        "skipped": [{"url": unrelated, "code": "REPOSITORY_UNREGISTERED"}],
    }
    request = {"run_id": str(uuid4()), "urls": reviews}
    mismatch = await runs.client.post(
        f"{code}/refresh-reviews",
        params=scope,
        json=request | {"urls": [unrelated]},
        headers=runs.headers(),
    )
    assert mismatch.status_code == 409
    (upstream / "pricing.py").write_text("def calculate_next():\n    return 8\n", encoding="utf-8")
    third = await commit()
    fetches = runner.network_calls.count("fetch")
    refreshed = await runs.client.post(f"{code}/refresh-reviews", params=scope, json=request, headers=runs.headers())
    assert refreshed.status_code == 200 and refreshed.json()["revision"] == third, refreshed.text
    assert refreshed.json()["extracted"] == 1 and runner.network_calls.count("fetch") == fetches + 1
    caller_latest = (await runs.client.get(f"{knowledge}/{app['id']}", params=scope)).json()
    assert caller_latest["revision"] == caller_detail["revision"] + 1
    assert caller_latest["source"]["blob"] == caller_detail["source"]["blob"]
    assert caller_latest["source"]["commit"] == third
    assert "## 调用引用" not in caller_latest["body"]
    assert {"kind": "DEPENDS_ON", "direction": "out", "target": pricing["id"], "title": "pricing.py"} in (
        caller_latest["relations"]
    )
    replayed = await runs.client.post(f"{code}/refresh-reviews", params=scope, json=request, headers=runs.headers())
    assert replayed.json()["status"] == "replayed" and runner.network_calls.count("fetch") == fetches + 1
    repeated = await runs.client.post(
        f"{code}/refresh-reviews",
        params=scope,
        json=request | {"run_id": str(uuid4())},
        headers=runs.headers(),
    )
    assert repeated.json()["status"] == "unchanged"
    before_old_replay = runner.network_calls.copy()
    assert before_old_replay.count("fetch") == fetches + 1
    old_replay = await runs.client.post(f"{code}/refresh-reviews", params=scope, json=request, headers=runs.headers())
    assert old_replay.json()["status"] == "replayed" and runner.network_calls == before_old_replay
    sources = (await runs.client.get(code, params=scope)).json()["review_sources"]
    assert [(row["url"], row["revision"]) for row in sources] == [(url, third) for url in reviews]

    # TeamAI 使用登记地址的仓库 slug，显示名称与命名空间独立。
    declaration = await runs.client.post(knowledge, params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "仓库概览引用", "body": "查看原仓库概览。",
        "related": {"MAPS_TO": "teamwiki/evidence/code/github__test__source/overview"},
    })
    assert declaration.status_code == 201, declaration.text
    note_path = f"{knowledge}/{declaration.json()['id']}"
    assert declaration.json()["relations"][0]["target"] == without_narrative["id"]

    edit = await runs.client.put(
        repo_path,
        headers=runs.headers(),
        json=repository_input(repo, git_url="https://93.184.216.34/test/replaced.git"),
    )
    assert edit.status_code == 200, edit.text
    assert (await runs.client.get(f"{knowledge}/{app['id']}", params=scope)).status_code == 404
    withheld = (await runs.client.get(note_path, params=scope)).json()
    assert withheld["relations"] == [] and withheld["relation_findings"][0]["code"] == "target_unavailable"
