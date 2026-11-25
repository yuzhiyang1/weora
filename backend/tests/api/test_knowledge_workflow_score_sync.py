"""使用真实 Git 对象验证已登记报表来源的同步与权限。"""

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import update

from tests.api.test_knowledge_workflow_scores import workflow_sample
from tests.api.test_projects import project, projects_path, repository, repository_input
from tests.api.test_repository_sync import FIXTURE_URL, LocalFixtureGit
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora.modules.knowledge.models import WorkflowScoreSyncSource
from weora.modules.knowledge.workflow_score_sync import BRANCH, WorkflowScoreSync
from weora.modules.projects.domain import RepositoryBlob
from weora.modules.projects.git_reader import GitRepositoryReader

pytestmark = [pytest.mark.anyio, pytest.mark.skipif(not shutil.which("git"), reason="需要实际 Git 命令")]


class CountingReader(GitRepositoryReader):
    """复用生产只读 Git 命令，记录实际打开的评分对象。"""

    def __init__(self, root: Path, runner: LocalFixtureGit) -> None:
        super().__init__(root, runner=runner)
        self.read_paths: list[str] = []

    async def index_text(self, cache_id: UUID, blob: RepositoryBlob, *, max_bytes: int = 256 * 1024) -> str | None:
        self.read_paths.append(blob.path)
        return await super().index_text(cache_id, blob, max_bytes=max_bytes)


class ReportsFixture:
    def __init__(self, root: Path) -> None:
        self.upstream = root / "upstream"
        self.upstream.mkdir(parents=True)
        self.runner = LocalFixtureGit(self.upstream)
        self.reader = CountingReader(root / "cache", self.runner)
        self.env = self.reader._base_env(self.upstream)

    async def git(self, *args: str) -> str:
        output = await self.runner.run(
            self.upstream, self.env, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args,
        )
        return output.decode().strip()

    async def commit(self) -> str:
        await self.git("add", "--all")
        await self.git("commit", "-m", "fixture")
        return await self.git("rev-parse", "HEAD")

    def write(self, path: str, *labels: dict[str, object]) -> None:
        target = self.upstream / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(json.dumps(label) for label in labels) + "\n", encoding="utf-8")

    async def register(self, runs: RunsHarness) -> tuple[str, dict[str, object], WorkflowScoreSync]:
        await self.git("init", "--initial-branch=main")
        (self.upstream / "README.md").write_text("test source", encoding="utf-8")
        await self.commit()
        projects = runs.app.state.project_service
        projects.reader = self.reader
        sync = WorkflowScoreSync(runs.sessions, projects, self.reader, timeout=30, interval=300)
        runs.app.state.workflow_score_sync = sync
        project_id = str((await project(runs, "团队报表项目"))["id"])
        repo = await repository(runs, project_id, "上报仓库", git_url=FIXTURE_URL)
        return project_id, repo, sync


async def exercise_score_sync(runs: RunsHarness, tmp_path: Path) -> None:
    fixture = ReportsFixture(tmp_path)
    project_id, repo, sync = await fixture.register(runs)
    another = str((await project(runs, "评分隔离项目"))["id"])
    report = f"/api/v1/workspaces/{runs.workspace_id}/knowledge/dashboard/scorers/workflow"
    path = report + "/sync"
    scope = {"project_id": project_id}
    config = {"enabled": True, "source_project_id": project_id, "repository_id": repo["id"], "expected_version": 0}
    repo_path = f"{projects_path(runs)}/{project_id}/repositories/{repo['id']}"

    async def status(params: dict[str, str] = scope) -> dict:
        return (await runs.client.get(path, params=params)).json()

    async def configure(payload: dict[str, object], params: dict[str, str] = scope, expected: int = 200) -> dict:
        response = await runs.client.put(path, params=params, json=payload, headers=runs.headers())
        assert response.status_code == expected, response.text
        return response.json()

    async def due() -> None:
        async with runs.sessions() as db, db.begin():
            await db.execute(update(WorkflowScoreSyncSource).where(
                WorkflowScoreSyncSource.workspace_id == UUID(runs.workspace_id),
            ).values(next_sync_at=datetime.now(UTC)))

    async def values(params: dict[str, str] = scope) -> set[str]:
        data = (await runs.client.get(report, params=params)).json()
        return {item["run_id"] for item in data["selected"]["failures"]["items"]} if data["selected"] else set()

    assert (await status())["status"] == "unconfigured"
    await configure(config, expected=422)
    check = await runs.client.post(repo_path + "/check", headers=runs.headers(), json={"version": repo["version"]})
    assert check.json()["status"] == "connected", check.text
    await configure(config, {"project_id": another}, expected=422)
    async with member(runs, "同步只读者") as (reader, _):
        reply = await reader.put(path, params=scope, headers=headers(reader), json=config)
        assert reply.status_code == 403
        assert (await reader.get(path, params=scope)).status_code == 200
    assert (await configure(config))["status"] == "queued"
    await configure(config, expected=409)
    assert await sync.run_one()
    missing = await status()
    assert missing["status"] == "failed" and missing["error_code"] == "WORKFLOW_SCORE_SYNC_BRANCH_MISSING"
    assert not await sync.run_one()

    await fixture.git("switch", "-c", BRANCH)
    now = datetime.now(UTC) - timedelta(minutes=1)
    month = now.strftime("%Y-%m")
    a, b = f"scores/alice/{month}.jsonl", f"scores/bob/{month}.jsonl"
    first, second = workflow_sample("r-first", now), workflow_sample("r-second", now)
    fixture.write(a, first, {"source": "session"})
    fixture.write(b, second)
    fixture.write(f"scores/non-member/deep/{month}.jsonl", workflow_sample("r-irrelevant", now))
    sha1 = await fixture.commit()
    await due()
    assert await sync.run_one()
    synced = await status()
    assert synced["revision"] == sha1 and synced["status"] == "ready"
    assert datetime.fromisoformat(synced["last_synced_at"]).utcoffset() == timedelta(0)
    assert set(fixture.reader.read_paths) == {a, b}
    assert await values() == {"r-first", "r-second"}
    assert await values({}) == await values({"project_id": another}) == set()
    previous_fetches = fixture.runner.network_calls.count("fetch")
    await due()
    assert await sync.run_one()
    assert fixture.runner.network_calls.count("fetch") == previous_fetches

    # 原有手动导入缓存与分支标签共存，同一原始标签只计一次。
    manual = await runs.client.post(report + "/import", params=scope, headers=runs.headers(), json={
        "jsonl": json.dumps(first),
    })
    assert manual.status_code == 200
    current = await runs.client.get(report, params=scope)
    assert current.json()["selected"]["stats"]["total"] == 2

    fixture.reader.read_paths.clear()
    fixture.write(a, first, workflow_sample("r-third", now))
    sha2 = await fixture.commit()
    await due()
    fixture.runner.fail_fetch = True
    assert await sync.run_one()
    assert (await status())["status"] == "failed" and (await status())["revision"] == sha1
    assert await values() == {"r-first", "r-second"}
    fixture.runner.fail_fetch = False
    await due()
    assert await sync.run_one()
    assert (await status())["revision"] == sha2
    assert fixture.reader.read_paths == [a]
    assert await values() == {"r-first", "r-second", "r-third"}

    # 坏文件不会替换上一次完整快照；修复后分支中的删除在一次事务内发布。
    (fixture.upstream / a).write_text("{invalid}", encoding="utf-8")
    await fixture.commit()
    await due()
    assert await sync.run_one()
    assert (await status())["error_code"] == "KNOWLEDGE_WORKFLOW_SCORE_FORMAT"
    assert await values() == {"r-first", "r-second", "r-third"}
    fixture.write(a, workflow_sample("r-third", now))
    (fixture.upstream / b).unlink()
    await fixture.commit()
    await due()
    assert await sync.run_one()
    assert await values() == {"r-first", "r-third"}

    fixture.write(a, workflow_sample("r-in-flight", now))
    await fixture.commit()
    await due()
    fixture.runner.release_fetch = asyncio.Event()
    fixture.runner.fetch_started.clear()
    in_flight = asyncio.create_task(sync.run_one())
    try:
        async with asyncio.timeout(15):
            await fixture.runner.fetch_started.wait()
        assert (await status())["status"] == "syncing"
        assert not await sync.run_one()
        assert (await configure({"enabled": False, "expected_version": 1}))["status"] == "disabled"
    finally:
        fixture.runner.release_fetch.set()
        await in_flight
        fixture.runner.release_fetch = None
    assert await values() == {"r-first"}
    assert (await configure(config, params={}))["status"] == "queued"
    assert await sync.run_one()
    assert await values({}) == {"r-in-flight"}
    assert await values() == {"r-first"}
    disabled = await runs.client.put(
        repo_path, headers=runs.headers(), json=repository_input(repo, status="disabled"),
    )
    assert disabled.status_code == 200, disabled.text
    assert (await status({}))["status"] == "source_changed"
    assert await values({}) == set()
    assert not list(fixture.reader.root.iterdir())


async def test_workflow_score_sync_publishes_atomically(runs: RunsHarness, tmp_path: Path) -> None:
    await exercise_score_sync(runs, tmp_path)
