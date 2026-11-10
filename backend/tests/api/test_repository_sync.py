"""用实际 Git 对象和独立数据库验收后台同步、跨 Run 复用与固定提交。"""

import asyncio
import os
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import update

from tests.api.test_projects import project, projects_path, repository, repository_input, task
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.delivery.infrastructure.git import IsolatedGitRunner
from weora.modules.projects.domain import ProjectActor, ProjectError
from weora.modules.projects.git_reader import GitRepositoryReader
from weora.modules.projects.persistence import ProjectRepositoryModel, RepositorySyncModel
from weora.modules.projects.repository_sync import RepositorySync

FIXTURE_URL = "https://93.184.216.34/test/source.git"


class LocalFixtureGit(IsolatedGitRunner):
    """仅测试把登记 URL 路由到本机 Git 夹具，生产读取器依然禁用 file 协议。"""

    def __init__(self, upstream: Path) -> None:
        super().__init__(timeout=15)
        self.upstream = upstream
        self.network_calls: list[str] = []
        self.fail_fetch = False
        self.fetch_started = asyncio.Event()
        self.release_fetch: asyncio.Event | None = None

    async def run(self, cwd: Path, env: Mapping[str, str], *args: str, stdin: bytes | None = None) -> bytes:
        if FIXTURE_URL in args:
            self.network_calls.append(args[0])
            if args[0] == "fetch" and self.fail_fetch:
                raise TimeoutError
            if args[0] == "fetch" and self.release_fetch is not None:
                self.fetch_started.set()
                await self.release_fetch.wait()
            git_env = dict(env)
            index = int(git_env["GIT_CONFIG_COUNT"])
            git_env[f"GIT_CONFIG_KEY_{index}"] = "protocol.file.allow"
            git_env[f"GIT_CONFIG_VALUE_{index}"] = "always"
            git_env["GIT_CONFIG_COUNT"] = str(index + 1)
            git_env["GIT_ALLOW_PROTOCOL"] = "file"
            args = tuple(self.upstream.as_uri() if value == FIXTURE_URL else value for value in args)
            return await super().run(cwd, git_env, *args, stdin=stdin)
        return await super().run(cwd, env, *args, stdin=stdin)


@pytest.mark.anyio
@pytest.mark.skipif(not shutil.which("git"), reason="需要实际 Git 命令")
async def test_background_sync_reuses_local_objects_for_runs_and_assessment(runs: RunsHarness, tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    runner = LocalFixtureGit(upstream)
    reader = GitRepositoryReader(tmp_path / "cache", runner=runner)
    env = reader._base_env(upstream)
    await runner.run(upstream, env, "init", "--initial-branch=main")

    async def commit(content: str) -> str:
        (upstream / "README.md").write_text(content, encoding="utf-8")
        await runner.run(upstream, env, "add", "README.md", "static.bin")
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

    (upstream / "static.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    first = await commit("first version")
    service = runs.app.state.project_service
    service.reader = reader
    sync = RepositorySync(
        runs.sessions, reader, service.repository_source, timeout_seconds=30, interval_seconds=300, max_age_seconds=900
    )
    service.repository_sync = sync
    p = await project(runs)
    repo = await repository(runs, p["id"], "large-fixture", git_url=FIXTURE_URL)
    project_id, repo_id = UUID(str(p["id"])), UUID(str(repo["id"]))
    actor = ProjectActor(UUID(runs.user_id), UUID(runs.workspace_id))
    repo_path = f"{projects_path(runs)}/{project_id}/repositories/{repo_id}"

    async def prepare(key: str) -> tuple[UUID, dict[str, object], dict[str, object]]:
        response = await task(runs, p["id"], [repo["id"]], key=key)
        assert response.status_code == 201, response.text
        data = response.json()
        run_id = UUID(data["id"])
        snapshot = data["project_context"]
        assert snapshot["repository_source_mode"] == "service-local-v1"
        rows = await service.prepare(actor, run_id, snapshot)
        return run_id, snapshot, rows[0]

    _, _, pending = await prepare("before-sync")
    assert pending["error_code"] == "REPOSITORY_SYNC_PENDING" and not runner.network_calls
    assert (await runs.client.get(repo_path + "/sync")).json() == {"status": "NOT_SCHEDULED"}
    assert (await runs.client.get(repo_path.replace(runs.workspace_id, str(repo_id)) + "/sync")).status_code == 404
    check = await runs.client.post(repo_path + "/check", json={"version": repo["version"]}, headers=runs.headers())
    assert check.json()["status"] == "connected"
    assert (await runs.client.get(repo_path + "/sync")).json()["status"] == "QUEUED"
    runner.release_fetch = asyncio.Event()
    sync_task = asyncio.create_task(sync.run_one())
    try:
        async with asyncio.timeout(15):
            await runner.fetch_started.wait()
        assert (await runs.client.get(repo_path + "/sync")).json()["status"] == "SYNCING"
        assert not await sync.run_one()  # 同仓库只有一个可执行的租约。
    finally:
        runner.release_fetch.set()
        assert await sync_task
        runner.release_fetch = None
    assert runner.network_calls == ["ls-remote", "ls-remote", "fetch"]
    assert (await runs.client.get(repo_path + "/sync")).json()["revision"] == first
    run_id, snapshot, prepared = await prepare("first-local-run")
    _, _, second_run = await prepare("second-local-run")
    assert prepared["revision"] == second_run["revision"] == first and "synced_at" in prepared
    assessment = await service.assessment_read(actor, project_id, (repo_id,), repo_id, "", directory=True)
    assert assessment["revision"] == first and assessment["connection_version"] == repo["connection_version"]
    assert any(item["name"] == "README.md" for item in cast(list[dict[str, object]], assessment["entries"]))
    assert runner.network_calls.count("fetch") == 1 and len(runner.network_calls) == 3

    next_revision = await commit("second version")

    async def due() -> None:
        async with runs.sessions() as session:
            await session.execute(
                update(RepositorySyncModel)
                .where(RepositorySyncModel.repository_id == repo_id)
                .values(
                    next_sync_at=datetime.now(UTC) - timedelta(seconds=1),
                )
            )
            await session.commit()

    await due()
    runner.fail_fetch = True
    assert await sync.run_one()
    failed = (await runs.client.get(repo_path + "/sync")).json()
    assert failed["revision"] == first and failed["error_code"] == "REPOSITORY_TIMEOUT"
    _, _, unavailable = await prepare("after-failed-sync")
    assert unavailable["error_code"] == "REPOSITORY_TIMEOUT"
    assert (await service.read_file(actor, run_id, snapshot, repo_id, "README.md"))["content"] == "first version"

    runner.fail_fetch = False
    await due()
    assert await sync.run_one()
    assert (await runs.client.get(repo_path + "/sync")).json()["revision"] == next_revision
    assert (await service.prepare(actor, run_id, snapshot))[0]["revision"] == first
    later_run, later_snapshot, later = await prepare("after-successful-sync")
    assert later["revision"] == next_revision
    assert (await service.read_file(actor, later_run, later_snapshot, repo_id, "README.md"))[
        "content"
    ] == "second version"
    old_read = await service.assessment_read(actor, project_id, (repo_id,), repo_id, "README.md", revision=first)
    assert old_read["content"] == "first version"
    assert runner.network_calls.count("fetch") == 3

    changed = await runs.client.put(
        repo_path, json=repository_input(repo, git_url="https://93.184.216.34/test/other.git"), headers=runs.headers()
    )
    assert changed.status_code == 200, changed.text
    assert (await runs.client.get(repo_path + "/sync")).json()["status"] == "NOT_SCHEDULED"
    with pytest.raises(ProjectError, match="授权已变化"):
        await service.read_file(actor, run_id, snapshot, repo_id, "README.md")
    with pytest.raises(ProjectError, match="连接已变化"):
        await service.assessment_read(
            actor,
            project_id,
            (repo_id,),
            repo_id,
            "README.md",
            revision=first,
            connection_version=int(str(repo["connection_version"])),
        )
    assert not await sync.purge_retired()
    async with runs.sessions() as session:
        await session.execute(
            update(ProjectRepositoryModel)
            .where(ProjectRepositoryModel.id == repo_id)
            .values(
                updated_at=datetime.now(UTC) - timedelta(days=8),
            )
        )
        await session.commit()
    assert await sync.purge_retired()
    assert not list((tmp_path / "cache").iterdir())
