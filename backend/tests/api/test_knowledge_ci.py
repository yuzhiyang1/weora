"""验证 CI 草稿的可见性、合并幂等和不同否决信号的隔离。"""

import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from tests.api.test_projects import project, projects_path, repository
from tests.api.test_repository_sync import LocalFixtureGit
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora.modules.knowledge.models import KnowledgeCiExtraction
from weora.modules.projects.domain import ProjectError, RepositoryBlob
from weora.modules.projects.git_reader import GitRepositoryReader
from weora.modules.projects.repository_sync import RepositorySync

pytestmark = pytest.mark.anyio


async def test_ci_replays_merge_and_keeps_unmerged_and_rejected_suggestions_out_of_recall(runs: RunsHarness) -> None:
    current = await project(runs)
    other = await project(runs, "其他项目")
    repo = await repository(runs, current["id"], "orders", git_url="https://github.com/Team/Orders.git")
    scope, other_scope = {"project_id": str(current["id"])}, {"project_id": str(other["id"])}
    knowledge = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    path = f"{knowledge}/ci/extractions"
    url = "https://github.com/team/orders/pull/31"
    staged = {
        "url": url, "head_commit": "a" * 40, "title": "库存锁顺序",
        "body": "固定库存行加锁顺序可以避免处理顺序不一致。",
        "tags": ["库存"], "review_author": "ci-author",
    }
    query = {"url": url, "head_commit": "a" * 40}
    receipt = {"merge_commit": "f" * 40, "merged_at": "2026-11-19T03:30:00Z", "rejected_learning": False,
               "rejected_codebase": True}

    assert (await runs.client.get(path, params=query)).status_code == 422
    assert (await runs.client.get(path, params=query | other_scope)).status_code == 409
    planned = await runs.client.get(path, params=query | scope)
    assert planned.json() == {"status": "missing", "repository_id": repo["id"]}
    async with member(runs, "普通项目成员") as (participant, _):
        forbidden = await participant.post(path, params=scope, headers=headers(participant), json=staged)
        assert forbidden.status_code == 403

    proposed = await runs.client.post(path, params=scope, headers=runs.headers(), json=staged)
    assert proposed.status_code == 201, proposed.text
    item = proposed.json()
    assert item["status"] == "proposed" and item["entry_id"] is None
    repeated = await runs.client.post(path, params=scope, headers=runs.headers(), json=staged | {
        "url": "https://github.com/TEAM/Orders/pull/31", "body": "再次提炼不覆盖第一次展示的正文。",
    })
    assert repeated.json() == item
    assert (await runs.client.get(knowledge, params=scope)).json()["items"] == []
    alternative = await runs.client.post(path, params=scope, headers=runs.headers(), json=staged | {
        "head_commit": "b" * 40,
    })
    assert alternative.status_code == 201
    finish = f"{path}/{item['id']}/merge"
    assert (await runs.client.post(finish, params=other_scope, headers=runs.headers(), json=receipt)).status_code == 404
    lease = str(uuid4())
    leases = f"{path}/{item['id']}/comment-lease"
    other_lease = f"{path}/{alternative.json()['id']}/comment-lease"
    for _ in range(2):
        claimed = await runs.client.post(leases, params=scope, headers=runs.headers(), json={"lease_id": lease})
        assert claimed.json() == {"status": "acquired"}, claimed.text
    blocked = await runs.client.post(other_lease, params=scope, headers=runs.headers(), json={"lease_id": str(uuid4())})
    assert blocked.status_code == 409
    assert (await runs.client.post(finish, params=scope, headers=runs.headers(), json=receipt)).status_code == 409
    written = await runs.client.post(
        finish, params=scope, headers=runs.headers(), json=receipt | {"comment_lease_id": lease},
    )
    assert written.status_code == 200, written.text
    final = written.json()
    assert final["status"] == "merged" and final["rejected_codebase"] is True
    assert (await runs.client.post(finish, params=scope, headers=runs.headers(), json=receipt)).json() == final
    changed = await runs.client.post(finish, params=scope, headers=runs.headers(), json=receipt | {
        "rejected_learning": True,
    })
    assert changed.status_code == 409
    stale = await runs.client.post(
        f"{path}/{alternative.json()['id']}/merge", params=scope, headers=runs.headers(), json=receipt,
    )
    assert stale.status_code == 409
    published = (await runs.client.get(f"{knowledge}/{final['entry_id']}", params=scope)).json()
    assert published["body"] == staged["body"]
    assert published["source"]["review_head"] == "a" * 40 and published["source"]["merge_commit"] == "f" * 40

    rejected = await runs.client.post(path, params=scope, headers=runs.headers(), json=staged | {
        "url": url.removesuffix("31") + "32", "title": "不采用的备选方案",
    })
    settled = await runs.client.post(f"{path}/{rejected.json()['id']}/merge", params=scope, headers=runs.headers(),
                                    json=receipt | {"rejected_learning": True, "rejected_codebase": False})
    assert settled.status_code == 200, settled.text
    assert settled.json()["entry_id"] is None and settled.json()["rejected_codebase"] is False
    assert len((await runs.client.get(knowledge, params=scope)).json()["items"]) == 1
    released = await runs.client.post(
        leases, params=scope, headers=runs.headers(), json={"lease_id": lease, "release": True},
    )
    assert released.json() == {"status": "released"}
    async with runs.sessions() as db:
        rows = (await db.scalars(select(KnowledgeCiExtraction))).all()
        assert len(rows) == 3
        assert all(row.stage_request_id for row in rows)
        terminal = [row for row in rows if row.merge_commit is not None]
        assert len(terminal) == 2 and all(row.complete_request_id for row in terminal)


@pytest.mark.skipif(not shutil.which("git"), reason="需要真实 Git 对象")
async def test_ci_graph_preview_compares_fixed_commits_without_publishing(
    runs: RunsHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_url = "https://github.com/test/source.git"
    monkeypatch.setattr("tests.api.test_repository_sync.FIXTURE_URL", fixture_url)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git = LocalFixtureGit(upstream)
    reader = GitRepositoryReader(tmp_path / "cache", runner=git)
    service = runs.app.state.project_service
    service.reader = reader
    service.repository_sync = RepositorySync(
        runs.sessions, reader, service.repository_source, timeout_seconds=30, interval_seconds=300, max_age_seconds=900,
    )
    env = reader._base_env(upstream)
    await git.run(upstream, env, "init", "--initial-branch=main")

    async def commit() -> str:
        await git.run(upstream, env, "add", "-A")
        await git.run(upstream, env, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                      "commit", "-m", "fixture")
        return (await git.run(upstream, env, "rev-parse", "HEAD")).decode().strip()

    (upstream / "service.py").write_text("class Stable: pass\ndef old_name(): pass\n", encoding="utf-8")
    (upstream / "retired.py").write_text("def retired(): pass\n", encoding="utf-8")
    (upstream / "binary.py").write_text("def before_binary(): pass\n", encoding="utf-8")
    (upstream / "consumer.py").write_text("from retired import retired\nretired()\n", encoding="utf-8")
    (upstream / "adapter.ts").write_text(
        "import type { BillingPort } from './port';\nexport class Adapter implements BillingPort {}\n",
        encoding="utf-8",
    )
    (upstream / "binary_reader.py").write_text("from binary import before_binary\nbefore_binary()\n", encoding="utf-8")
    old = await commit()
    current = await project(runs)
    other = await project(runs, "未登记仓库的项目")
    repo = await repository(runs, current["id"], "代码证据", git_url=fixture_url)
    checked = await runs.client.post(
        f"{projects_path(runs)}/{current['id']}/repositories/{repo['id']}/check",
        headers=runs.headers(), json={"version": repo["version"]},
    )
    assert checked.json()["status"] == "connected", checked.text
    assert await service.repository_sync.run_one()
    (upstream / "service.py").write_text("\nclass Stable: pass\ndef new_name(): pass\n", encoding="utf-8")
    (upstream / "retired.py").unlink()
    (upstream / "binary.py").write_bytes(b"\x00binary")
    (upstream / "port.ts").write_text("export interface BillingPort {}\n", encoding="utf-8")
    proposed = await commit()

    knowledge = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    path = f"{knowledge}/ci/graph-preview"
    scope = {"project_id": str(current["id"])}
    data = {
        "url": fixture_url.removesuffix(".git") + "/pull/2",
        "base_commit": old, "head_commit": proposed, "paths": ["service.py", "retired.py", "port.ts", "binary.py"],
    }
    untouched = git.network_calls.copy()
    denied = await runs.client.post(
        path, params={"project_id": str(other["id"])}, headers=runs.headers(), json=data,
    )
    assert denied.status_code == 409 and git.network_calls == untouched
    invalid = await runs.client.post(
        path, params=scope, headers=runs.headers(), json=data | {"paths": ["../service.py"]},
    )
    assert invalid.status_code == 422 and git.network_calls == untouched

    result = await runs.client.post(path, params=scope, headers=runs.headers(), json=data)
    assert result.status_code == 200, result.text
    preview = result.json()
    assert (preview["base_commit"], preview["head_commit"]) == (old, proposed)
    assert preview["skipped"] == ["binary.py"]
    assert {row["name"] for row in preview["nodes"]["added"]} == {"new_name", "port.ts", "BillingPort"}
    assert {row["name"] for row in preview["nodes"]["updated"]} == {"service.py", "Stable"}
    assert {row["name"] for row in preview["nodes"]["removed"]} == {"old_name", "retired.py", "retired"}
    assert preview["counts"] == {"added": 3, "updated": 2, "removed": 3}
    # 调用方和实现类本身没改，导入目标变化仍会新增或移除关系。
    assert preview["relations"] == {
        "added": [
            {"kind": "DEPENDS_ON", "source": "adapter.ts", "target": "port.ts"},
            {"kind": "IMPLEMENTS", "source": "adapter.ts", "target": "port.ts"},
        ],
        "removed": [
            {"kind": "DEPENDS_ON", "source": "consumer.py", "target": "retired.py"},
            {"kind": "REFERENCES", "source": "consumer.py", "target": "retired.py"},
        ],
    }
    assert preview["relation_counts"] == {"added": 2, "removed": 2}
    assert preview["relation_coverage"] == {"limit": 200, "base_candidates": 6, "head_candidates": 6}
    assert preview["relation_skipped"] == 1
    assert not (await runs.client.get(knowledge, params=scope)).json()["items"]
    snapshot = await service.repository_sync.status(UUID(runs.workspace_id), UUID(str(repo["id"])), 1)
    assert snapshot["revision"] == old
    assert git.network_calls.count("fetch") == untouched.count("fetch") + 1

    selected_blobs = reader.index_blobs

    async def limit_catalog(cache: UUID, revision: str, paths: tuple[str, ...] = ()) -> list[RepositoryBlob]:
        if not paths:
            raise ProjectError("REPOSITORY_INDEX_CATALOG_UNAVAILABLE", "目录超出索引预算")
        return await selected_blobs(cache, revision, paths)

    monkeypatch.setattr(reader, "index_blobs", limit_catalog)
    partial = await runs.client.post(path, params=scope, headers=runs.headers(), json=data)
    assert partial.status_code == 200, partial.text
    assert partial.json()["nodes"] == preview["nodes"]
    assert partial.json()["relation_warning"] == "REPOSITORY_INDEX_CATALOG_UNAVAILABLE"
    assert "relations" not in partial.json()
