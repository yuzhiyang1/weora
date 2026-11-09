"""通过 HTTP 和真实事务验证项目配置、最小仓库选择、不可变快照与秘密边界。"""

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import update

from tests.api.test_run_execution import PROFILE
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.bootstrap import worker
from weora.modules.agents.application.service import AgentRegistryService
from weora.modules.collaboration.infrastructure.tasks import TaskModel
from weora.modules.context.feishu import FernetSecrets
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.identity.application.service import IdentityService
from weora.modules.identity.infrastructure.models import WorkspaceMembershipModel
from weora.modules.projects.domain import ProjectError, git_address
from weora.modules.projects.persistence import ProjectRepositoryModel
from weora.modules.projects.service import ProjectService
from weora.modules.runs.application.execution import RuntimeFailure
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.agent_catalog import AgentsModuleVersionCatalog
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.module_access import DeliveryModuleAccess
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork
from weora.modules.tools.application.gateway import ToolScope
from weora.modules.tools.infrastructure.repository import READ_FILE

RUNTIME_ENTRY = str(Path(__file__).resolve())


def projects_path(runs: RunsHarness) -> str:
    return f"/api/v1/workspaces/{runs.workspace_id}/projects"


async def project(runs: RunsHarness, name: str = "订单项目") -> dict[str, object]:
    response = await runs.client.post(
        projects_path(runs),
        json={
            "name": name,
            "description": "订单验收标识 PROJECT-CTX-20",
            "architecture": "前后端分离，通过 OpenAPI 协作",
            "conventions": "必须新增回归测试",
        },
        headers=runs.headers(),
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


async def repository(runs: RunsHarness, project_id: object, name: str, **extra: object) -> dict[str, object]:
    response = await runs.client.post(
        f"{projects_path(runs)}/{project_id}/repositories",
        json={
            "name": name,
            "purpose": name + "用途",
            "git_url": f"https://git.example.test/team/{name}.git",
            "technology": "React" if name == "web" else "Python",
            **extra,
        },
        headers=runs.headers(),
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json()["repositories"][-1])


def repository_input(row: dict[str, object], **changes: object) -> dict[str, object]:
    """更新只发送写模型字段，不能把响应里的只读属性当作合法输入。"""
    return {
        key: row[key]
        for key in (
            "name",
            "purpose",
            "provider",
            "git_url",
            "default_branch",
            "technology",
            "development_notes",
            "status",
            "version",
        )
    } | changes


async def task(runs: RunsHarness, project_id: object, ids: Sequence[object], key: str = "project-task") -> Response:
    return await runs.client.post(
        runs.path,
        json=runs.payload()
        | {
            "project_id": project_id,
            "repository_ids": ids,
        },
        headers=runs.headers(key),
    )


@pytest.mark.anyio
async def test_selected_repositories_frozen_and_injected_without_unselected_or_credentials(runs: RunsHarness) -> None:
    cipher = FernetSecrets(Fernet.generate_key().decode())
    cast(ProjectService, runs.app.state.project_service).secrets = cipher
    p = await project(runs)
    web = await repository(runs, p["id"], "web", credential_token="ONLY-WRITE-TEST-TOKEN")
    api = await repository(runs, p["id"], "api")
    await repository(runs, p["id"], "UNSELECTED-MOBILE")
    created = await task(runs, p["id"], [api["id"], web["id"]])
    assert created.status_code == 201, created.text
    data = created.json()
    snapshot = data["project_context"]
    assert {r["name"] for r in snapshot["repositories"]} == {"api", "web"}
    assert "UNSELECTED-MOBILE" not in json.dumps(data)
    assert "credential" not in json.dumps(snapshot)
    assert "ONLY-WRITE-TEST-TOKEN" not in json.dumps(data)
    async with runs.sessions() as session:
        stored = await session.get(ProjectRepositoryModel, UUID(str(web["id"])))
        assert stored and stored.credential_ciphertext
        assert cipher.decrypt(stored.credential_ciphertext) == "ONLY-WRITE-TEST-TOKEN"
        assert "ONLY-WRITE-TEST-TOKEN" not in stored.credential_ciphertext
        task_row = await session.get(TaskModel, UUID(data["task_id"]))
        assert task_row and task_row.project_context == snapshot
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    detail = await runs.app.state.run_service.access_metadata(actor, UUID(data["id"]))
    access = ModulesExecutionAccess(
        identity=cast(IdentityService, runs.app.state.identity_service),
        agents=cast(AgentRegistryService, runs.app.state.agent_registry_service),
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(runs.sessions),
        projects=runs.app.state.context_access.projects,
    )
    payload = await access.load_input(detail.run)
    assert "PROJECT-CTX-20" in payload.instruction
    assert "不是系统指令" in payload.instruction
    assert "UNSELECTED-MOBILE" not in payload.instruction
    assert "ONLY-WRITE-TEST-TOKEN" not in payload.instruction
    lease = await runs.app.state.execution_store.claim(
        workspace_id=actor.workspace_id,
        run_id=detail.run.id,
        owner="project-test",
        lease_seconds=30,
    )
    assert lease is not None
    result = await runs.app.state.tool_gateway.invoke(
        ToolScope(detail.run.id, actor, lease.owner, lease.fencing_token),
        "not-default-repository",
        READ_FILE.name,
        {"path": "README.md"},
    )
    assert "PROJECT_REPOSITORY_NOT_PREPARED" in str(result)
    # 修改项目资料后，旧任务和幂等重试均保持原快照，仓库选择顺序不影响请求身份。
    latest = (await runs.client.get(f"{projects_path(runs)}/{p['id']}")).json()
    edited = await runs.client.put(
        f"{projects_path(runs)}/{p['id']}",
        json={
            "name": "新名称",
            "description": "修改后的背景",
            "version": latest["version"],
        },
        headers=runs.headers(),
    )
    assert edited.status_code == 200
    replay = await task(runs, p["id"], [web["id"], api["id"]])
    assert replay.status_code == 200, replay.text
    assert replay.json()["project_context"] == snapshot
    assert (await access.load_input(detail.run)).instruction == payload.instruction
    conflict = await task(runs, p["id"], [web["id"]])
    assert conflict.status_code == 409
    # 登记了仓库也不能误执行原有的默认测试仓库，尤其不能把资料建议当执行批准。
    delivery_access = DeliveryModuleAccess(
        runs.app.state.context_access,
        runs.app.state.execution_store,
        None,
        execution_workspace_id=actor.workspace_id,
    )
    with pytest.raises(DeliveryError, match="尚未准备隔离代码副本"):
        await delivery_access.authorize(actor, detail.run.id, write=True, fencing_token=1)
    archived = await runs.client.put(
        f"{projects_path(runs)}/{p['id']}",
        json={"name": "新名称", "version": edited.json()["version"], "status": "archived"},
        headers=runs.headers(),
    )
    assert archived.status_code == 200
    with pytest.raises(RuntimeFailure, match="PROJECT_ACCESS_REVOKED"):
        await access.load_input(detail.run)
    assert (await runs.client.get(f"{runs.path}/{data['id']}")).status_code == 404


@pytest.mark.anyio
async def test_selection_scope_disabled_revocation_and_no_project_compatibility(runs: RunsHarness) -> None:
    p = await project(runs)
    repo = await repository(runs, p["id"], "api")
    other = await project(runs, "另一个项目")
    foreign = await repository(runs, other["id"], "other")
    for ids in ([], [repo["id"], repo["id"]], [foreign["id"]], [str(uuid4())]):
        response = await task(runs, p["id"], ids, str(uuid4()))
        assert response.status_code == 409, response.text
    invalid = await runs.client.post(
        runs.path, json=runs.payload() | {"repository_ids": [repo["id"]]}, headers=runs.headers()
    )
    assert invalid.status_code == 409
    created = await task(runs, p["id"], [repo["id"]])
    assert created.status_code == 201
    path = f"{runs.path}/{created.json()['id']}"
    disabled = await runs.client.put(
        f"{projects_path(runs)}/{p['id']}/repositories/{repo['id']}",
        json=repository_input(repo, status="disabled"),
        headers=runs.headers(),
    )
    assert disabled.status_code == 200, disabled.text
    assert (await runs.client.get(path)).status_code == 404
    assert (await task(runs, p["id"], [repo["id"]], "disabled")).status_code == 409
    ordinary = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers("ordinary"))
    assert ordinary.status_code == 201
    assert ordinary.json()["project_context"] is None


@pytest.mark.anyio
async def test_cas_duplicates_csrf_and_cross_workspace_are_rejected(runs: RunsHarness) -> None:
    p = await project(runs)
    path = f"{projects_path(runs)}/{p['id']}"
    body = {"name": "改名", "version": p["version"]}
    assert (await runs.client.put(path, json=body, headers=runs.headers())).status_code == 200
    stale = await runs.client.put(path, json=body, headers=runs.headers())
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "PROJECT_CHANGED"
    repo = await repository(runs, p["id"], "api")
    repopath = f"{path}/repositories/{repo['id']}"
    assert (await runs.client.put(repopath, json=repository_input(repo), headers=runs.headers())).status_code == 200
    assert (await runs.client.put(repopath, json=repository_input(repo), headers=runs.headers())).status_code == 409
    duplicate = await runs.client.post(
        f"{path}/repositories", json={"name": "重复", "git_url": repo["git_url"]}, headers=runs.headers()
    )
    assert duplicate.status_code == 409
    assert (await runs.client.post(projects_path(runs), json={"name": "CSRF"})).status_code == 403
    hidden = await runs.client.get(f"/api/v1/workspaces/{uuid4()}/projects/{p['id']}")
    assert hidden.status_code == 404
    # 活动普通成员可以自行建项目；不是创建者时不能修改别人的项目。
    async with runs.sessions() as session:
        await session.execute(
            update(WorkspaceMembershipModel)
            .where(
                WorkspaceMembershipModel.user_id == UUID(runs.user_id),
                WorkspaceMembershipModel.workspace_id == UUID(runs.workspace_id),
            )
            .values(role="member")
        )
        await session.commit()
    own = await project(runs, "普通开发者项目")
    assert own["can_manage"] is True


@pytest.mark.anyio
async def test_credential_errors_do_not_echo_input_and_secret_preserve_clear(runs: RunsHarness) -> None:
    p = await project(runs)
    path = f"{projects_path(runs)}/{p['id']}/repositories"
    payload = {"name": "api", "git_url": "https://git.example.test/api.git", "credential_token": "TEST-SECRET"}
    unavailable = await runs.client.post(path, json=payload, headers=runs.headers())
    assert unavailable.status_code == 409 and "TEST-SECRET" not in unavailable.text
    invalid = await runs.client.post(
        path, json=payload | {"credential_token": "TEST-SECRET" * 900}, headers=runs.headers()
    )
    assert invalid.status_code == 422 and "TEST-SECRET" not in invalid.text
    cast(ProjectService, runs.app.state.project_service).secrets = FernetSecrets(Fernet.generate_key().decode())
    repo = await repository(runs, p["id"], "api", credential_token="TEST-SECRET")
    assert repo["has_credential"] is True and "TEST-SECRET" not in str(repo)
    updated = await runs.client.put(f"{path}/{repo['id']}", json=repository_input(repo), headers=runs.headers())
    current = updated.json()["repositories"][0]
    assert current["has_credential"] is True
    cleared = await runs.client.put(
        f"{path}/{repo['id']}", json=repository_input(current, credential_token=""), headers=runs.headers()
    )
    assert cleared.json()["repositories"][0]["has_credential"] is False


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/repo",
        "ext::sh command",
        "https://token:password@git.test/a",
        "https://token@git.test/a",
        "https://git.test/a?token=SECRET",
        "https://git.test/a#secret",
        "ssh://root@git.test/a",
        "https://git.test/a\\b",
        "https://git.test:99999/a",
        "https://git.test/a b",
    ],
)
def test_git_address_rejects_embedded_credentials_local_protocols_and_ambiguous_paths(url: str) -> None:
    with pytest.raises(ProjectError):
        git_address(url)


def test_git_address_normalizes_common_ssh_form() -> None:
    assert git_address("git@git.example.test:team/api.git") == "ssh://git@git.example.test/team/api.git"


@pytest.mark.anyio
async def test_other_member_can_read_and_create_but_not_edit_or_get_secret(runs: RunsHarness) -> None:
    """另一个真实账户验证跨空间隐藏、同空间共享、非创建者禁止修改及撤权。"""
    cast(ProjectService, runs.app.state.project_service).secrets = FernetSecrets(Fernet.generate_key().decode())
    p = await project(runs)
    repo = await repository(runs, p["id"], "api", credential_token="DO-NOT-EXPOSE")
    path = f"{projects_path(runs)}/{p['id']}"
    async with AsyncClient(transport=ASGITransport(app=runs.app), base_url="http://test") as other:
        registration = await other.post(
            "/api/v1/auth/register",
            json={
                "display_name": "另一个开发者",
                "email": "another@example.com",
                "password": "correct horse battery staple",
                "workspace_name": "另一个空间",
            },
        )
        assert registration.status_code == 201
        other_user = UUID(registration.json()["user"]["id"])
        assert (await other.get(path)).status_code == 404
        async with runs.sessions() as session:
            await session.execute(
                update(WorkspaceMembershipModel)
                .where(
                    WorkspaceMembershipModel.user_id == other_user,
                )
                .values(workspace_id=UUID(runs.workspace_id), role="member")
            )
            await session.commit()
        shared = await other.get(path)
        assert shared.status_code == 200
        assert shared.json()["can_manage"] is False
        assert shared.json()["repositories"][0]["has_credential"] is False
        assert "DO-NOT-EXPOSE" not in shared.text and "ciphertext" not in shared.text
        headers = {"X-CSRF-Token": other.cookies["weora_csrf"]}
        assert (
            await other.put(path, json={"name": "篡改", "version": p["version"]}, headers=headers)
        ).status_code == 403
        denied = await other.put(f"{path}/repositories/{repo['id']}", json=repository_input(repo), headers=headers)
        assert denied.status_code == 403
        own = await other.post(projects_path(runs), json={"name": "开发者自行创建"}, headers=headers)
        assert own.status_code == 201 and own.json()["can_manage"] is True
        async with runs.sessions() as session:
            await session.execute(
                update(WorkspaceMembershipModel)
                .where(
                    WorkspaceMembershipModel.user_id == other_user,
                )
                .values(status="inactive")
            )
            await session.commit()
        assert (await other.get(path)).status_code == 401


@pytest.mark.anyio
async def test_worker_rechecks_project_before_persisting_model_result(
    runs: RunsHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型已拿到资料后归档项目，平台必须拒绝保存成功，不能仅依靠首次鉴权。"""
    profile = replace(PROFILE, timeout_seconds=10, execution_timeout_seconds=1800)
    runs.app.state.run_service = RunService(
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(runs.sessions),
        agent_catalog=AgentsModuleVersionCatalog(runs.app.state.agent_registry_service),
        execution_profile=profile,
        projects=runs.app.state.context_access.projects,
    )
    settings = runs.app.state.settings.model_copy(
        update={
            "run_runtime": "pi",
            "pi_model": profile.model_id,
            "deepseek_api_key": SecretStr("not-real"),
            "pi_runtime_entry": RUNTIME_ENTRY,
            "run_model_max_tokens": profile.max_tokens,
            "run_model_timeout_seconds": profile.timeout_seconds,
        }
    )
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "create_app", lambda _: runs.app)
    p = await project(runs)
    repo = await repository(runs, p["id"], "api")
    created = await task(runs, p["id"], [repo["id"]])
    assert created.status_code == 201

    class Runtime:
        def __init__(self, **_: object) -> None:
            pass

        async def run(self, payload: object, profile: object) -> str:
            current = (await runs.client.get(f"{projects_path(runs)}/{p['id']}")).json()
            archived = await runs.client.put(
                f"{projects_path(runs)}/{p['id']}",
                json={
                    "name": "模型期间归档",
                    "version": current["version"],
                    "status": "archived",
                },
                headers=runs.headers(),
            )
            assert archived.status_code == 200
            return "这段输出不能被保存成成功结果"

    monkeypatch.setattr(worker, "PiTextRuntime", Runtime)
    run_id = UUID(created.json()["id"])
    assert await worker._execute(UUID(runs.workspace_id), run_id)
    detail = await runs.app.state.run_service.access_metadata(
        RunActor(UUID(runs.user_id), UUID(runs.workspace_id)), run_id
    )
    assert detail.run.status == "FAILED"
    assert detail.run.failure_code == "PROJECT_ACCESS_REVOKED"
    assert detail.run.output_text is None
