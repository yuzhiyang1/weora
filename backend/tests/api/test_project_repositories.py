"""使用真实事务验证多仓提交固定、最小选择、连接变更与工具只读范围。"""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update

from tests.api.test_projects import project, projects_path, repository, repository_input, task
from tests.api.test_run_execution import PROFILE, ScriptedRuntime
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.context.feishu import FernetSecrets
from weora.modules.projects.domain import ProjectActor, ProjectError, RepositorySource
from weora.modules.projects.persistence import ProjectRepositoryModel
from weora.modules.runs.application.execution import RunExecutor, RunLease, RuntimeInput
from weora.modules.runs.application.ports import ExecutionProfile, RunActor
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork
from weora.modules.tools.application.gateway import ToolScope


class Reader:
    def __init__(self) -> None:
        self.revision = "a" * 40
        self.cloned: list[tuple[UUID, str]] = []
        self.checked: list[UUID] = []
        self.discarded: list[UUID] = []
        self.check_failures: set[UUID] = set()
        self.clone_failures: set[UUID] = set()

    async def check(self, source: RepositorySource) -> str:
        self.checked.append(source.repository_id)
        if source.repository_id in self.check_failures:
            raise ProjectError("REPOSITORY_DNS_FAILED", "测试：无法解析仓库")
        return self.revision

    async def clone(self, source: RepositorySource, revision: str) -> UUID:
        self.cloned.append((source.repository_id, revision))
        if source.repository_id in self.clone_failures:
            raise ProjectError("REPOSITORY_TIMEOUT", "测试：快照拉取超时")
        return uuid4()

    async def read(self, cache_id: UUID, revision: str, path: str) -> dict[str, object]:
        return {"ok": True, "revision": revision, "path": path, "content": "夹具 README"}

    async def discard(self, cache_id: UUID) -> None:
        self.discarded.append(cache_id)


@pytest.mark.anyio
async def test_legacy_codeup_alias_keeps_secret_and_connection_version(runs: RunsHarness) -> None:
    """旧网页地址等价规范化不代表换仓库；资料编辑不能清掉有效密码，也不能重复登记同仓。"""
    service = runs.app.state.project_service
    service.secrets = FernetSecrets(Fernet.generate_key().decode())
    p = await project(runs)
    url = "https://codeup.aliyun.com/org/team/repo"
    repo = await repository(
        runs, p["id"], "legacy", git_url=url, credential_username="fixture", credential_token="fixture-password"
    )
    # 模拟升级前的真实存量记录；新 API 已统一保存克隆地址。
    async with runs.sessions() as session:
        await session.execute(
            update(ProjectRepositoryModel).where(ProjectRepositoryModel.id == UUID(str(repo["id"]))).values(git_url=url)
        )
        await session.commit()
    path = f"{projects_path(runs)}/{p['id']}"
    current = (await runs.client.get(path)).json()["repositories"][0]
    assert current["git_url"] == url + ".git"
    duplicate = await runs.client.post(
        path + "/repositories", json={"name": "duplicate", "git_url": url + ".git"}, headers=runs.headers()
    )
    assert duplicate.status_code == 409
    changed = await runs.client.put(
        path + "/repositories/" + str(repo["id"]),
        json=repository_input(current, purpose="资料更新", credential_username="fixture"),
        headers=runs.headers(),
    )
    assert changed.status_code == 200
    stored = changed.json()["repositories"][0]
    assert stored["has_credential"] and stored["connection_version"] == repo["connection_version"]
    source = await service._source(
        ProjectActor(UUID(runs.user_id), UUID(runs.workspace_id)),
        UUID(str(p["id"])),
        UUID(str(repo["id"])),
        stored["connection_version"],
    )
    assert source.git_url == url + ".git" and source.token == "fixture-password"


@pytest.mark.anyio
async def test_only_selected_cloned_fixed_across_replay_and_tool_scope(runs: RunsHarness) -> None:
    reader = Reader()
    service = runs.app.state.project_service
    service.reader = reader
    p = await project(runs)
    a = await repository(runs, p["id"], "a")
    b = await repository(runs, p["id"], "b")
    excluded = await repository(runs, p["id"], "excluded")
    created = await task(runs, p["id"], [a["id"], b["id"]])
    assert created.status_code == 201
    data = created.json()
    run_id, actor = UUID(data["id"]), ProjectActor(UUID(runs.user_id), UUID(runs.workspace_id))
    prepared = await service.prepare(actor, run_id, data["project_context"])
    assert len(prepared) == 2 and all(r["status"] == "READY" for r in prepared)
    assert {r[0] for r in reader.cloned} == {UUID(str(a["id"])), UUID(str(b["id"]))}
    reader.revision = "b" * 40
    assert await service.prepare(actor, run_id, data["project_context"]) == prepared
    assert len(reader.checked) == 2 and len(reader.cloned) == 2
    lease = await runs.app.state.execution_store.claim(
        workspace_id=actor.workspace_id, run_id=run_id, owner="clone-test", lease_seconds=30
    )
    scope = ToolScope(
        run_id,
        RunActor(actor.user_id, actor.workspace_id),
        lease.owner,
        lease.fencing_token,
    )
    gateway = runs.app.state.tool_gateway
    for r in (a, b):
        result = await gateway.invoke(
            scope, str(r["id"]), "repo.file.read", {"repository_id": r["id"], "path": "README.md"}
        )
        assert result["ok"] is True and result["revision"] == "a" * 40
    denied = await gateway.invoke(
        scope, "excluded", "repo.file.read", {"repository_id": excluded["id"], "path": "README.md"}
    )
    assert "REPOSITORY_NOT_SELECTED" in str(denied)
    assert "REPOSITORY_SELECTION_REQUIRED" in str(
        await gateway.invoke(scope, "missing", "repo.file.read", {"path": "README.md"})
    )
    assert (await runs.client.get(f"{runs.path}/{run_id}/repositories")).json()["items"] == prepared


@pytest.mark.anyio
@pytest.mark.parametrize("has_ready_repository", [True, False])
async def test_run_isolates_repository_preparation_failures(runs: RunsHarness, has_ready_repository: bool) -> None:
    """走真实准备和 Run 终态链路，分别验证部分就绪、全量失败及原提交恢复。"""
    profile = replace(PROFILE, timeout_seconds=30)
    runs.app.state.run_service._execution_profile = profile
    reader = Reader()
    service = runs.app.state.project_service
    service.reader = reader
    p = await project(runs)
    dns = await repository(runs, p["id"], "a-dns")
    android = await repository(runs, p["id"], "b-android")
    backend = await repository(runs, p["id"], "c-backend")
    dns_id, android_id, backend_id = (UUID(str(repo["id"])) for repo in (dns, android, backend))
    reader.check_failures.add(dns_id)
    reader.clone_failures.add(android_id)
    selected = [dns, android, backend] if has_ready_repository else [dns, android]
    created = await task(runs, p["id"], [repo["id"] for repo in selected])
    assert created.status_code == 201, created.text
    data = created.json()
    run_id = UUID(data["id"])
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    access = ModulesExecutionAccess(
        identity=runs.app.state.identity_service,
        agents=runs.app.state.agent_registry_service,
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(runs.sessions),
        projects=runs.app.state.context_access.projects,
    )

    class RepositoryRuntime(ScriptedRuntime):
        lease: RunLease

        async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
            scope = ToolScope(run_id, actor, self.lease.owner, self.lease.fencing_token)
            gateway = runs.app.state.tool_gateway
            read = await gateway.invoke(
                scope, "backend", "repo.file.read", {"repository_id": str(backend_id), "path": "README.md"}
            )
            assert read["ok"] is True and read["revision"] == "a" * 40
            failed = await gateway.invoke(
                scope, "android", "repo.file.read", {"repository_id": str(android_id), "path": "README.md"}
            )
            assert failed == {"ok": False, "error": {"code": "REPOSITORY_TIMEOUT"}}
            return await super().run(payload, profile)

    runtime = RepositoryRuntime()

    def runtime_for(lease: RunLease) -> RepositoryRuntime:
        runtime.lease = lease
        return runtime

    executor = RunExecutor(
        store=runs.app.state.execution_store,
        access=access,
        runtime=runtime,
        runtime_factory=runtime_for,
        profile=profile,
        lease_seconds=5,
        heartbeat_seconds=0.05,
    )
    assert await executor.execute(workspace_id=actor.workspace_id, run_id=run_id, owner="repository-test")
    result = (await runs.client.get(f"{runs.path}/{run_id}")).json()
    statuses = (await runs.client.get(f"{runs.path}/{run_id}/repositories")).json()["items"]
    by_id = {repo["repository_id"]: repo for repo in statuses}
    assert by_id[str(dns_id)] == {
        "repository_id": str(dns_id),
        "status": "FAILED",
        "revision": None,
        "error_code": "REPOSITORY_DNS_FAILED",
    }
    assert by_id[str(android_id)]["status"] == "FAILED" and by_id[str(android_id)]["revision"] == "a" * 40
    if has_ready_repository:
        assert result["status"] == "SUCCEEDED" and by_id[str(backend_id)]["status"] == "READY"
        assert len(runtime.calls) == 1
        assert "REPOSITORY_TIMEOUT" in runtime.calls[0].instruction
        assert "报告 blocked" in runtime.calls[0].instruction
    else:
        assert result["status"] == "FAILED" and result["failure_code"] == "PROJECT_REPOSITORIES_UNAVAILABLE"
        assert runtime.calls == []

    # 恢复只为未解析仓库查分支，已固定的仓库继续用原提交。
    reader.check_failures.clear()
    reader.clone_failures.clear()
    reader.revision = "b" * 40
    recovered = await service.prepare(ProjectActor(actor.user_id, actor.workspace_id), run_id, data["project_context"])
    assert all(repo["status"] == "READY" for repo in recovered)
    assert reader.checked.count(android_id) == 1 and reader.cloned.count((android_id, "a" * 40)) == 2
    assert (dns_id, "b" * 40) in reader.cloned


@pytest.mark.anyio
async def test_connection_check_and_change_invalidate_old_run_binding(runs: RunsHarness) -> None:
    reader = Reader()
    service = runs.app.state.project_service
    service.reader = reader
    p = await project(runs)
    repo = await repository(runs, p["id"], "a")
    path = f"{projects_path(runs)}/{p['id']}/repositories/{repo['id']}"
    check = await runs.client.post(path + "/check", json={"version": repo["version"]}, headers=runs.headers())
    assert check.status_code == 200 and check.json()["status"] == "connected"
    assert (await runs.client.post(path + "/check", json={"version": repo["version"]})).status_code == 403
    created = await task(runs, p["id"], [repo["id"]])
    changed = await runs.client.put(
        path, json=repository_input(repo, git_url="https://other.example.test/a.git"), headers=runs.headers()
    )
    assert changed.status_code == 200
    assert changed.json()["repositories"][0]["connection_status"] == "registered"
    assert (await runs.client.get(f"{runs.path}/{created.json()['id']}")).status_code == 404
    with pytest.raises(ProjectError, match="授权已变化"):
        await service.prepare(
            ProjectActor(UUID(runs.user_id), UUID(runs.workspace_id)),
            UUID(created.json()["id"]),
            created.json()["project_context"],
        )
    assert reader.cloned == []
