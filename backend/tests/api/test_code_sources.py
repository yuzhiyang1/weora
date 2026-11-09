"""代码源配置的真实事务回归：只写身份、最小关联、版本撤权和构建建议。"""

import json
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.fernet import Fernet

from tests.api.test_projects import project, projects_path, repository, task
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.delivery.test_adapters import CaptureDocker, snapshot
from weora.bootstrap.project_execution import ProjectExecution
from weora.modules.context.feishu import FernetSecrets
from weora.modules.delivery.domain.models import DeliveryError, PlanScope
from weora.modules.projects.domain import ProjectActor, ProjectError
from weora.modules.projects.persistence import CodeSourceModel
from weora.modules.runs.application.ports import RunActor

TOKEN = "only-for-unit-test-code-source"


def path(runs: RunsHarness) -> str:
    return f"/api/v1/workspaces/{runs.workspace_id}/code-sources"


async def create_source(runs: RunsHarness) -> dict[str, object]:
    runs.app.state.code_source_service.secrets = FernetSecrets(Fernet.generate_key().decode())
    response = await runs.client.post(
        path(runs),
        json={
            "name": "云效单测",
            "organization": "org-test",
            "api_token": TOKEN,
        },
        headers=runs.headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.anyio
async def test_code_source_secret_ciphertext_cas_and_no_readback(runs: RunsHarness) -> None:
    source = await create_source(runs)
    assert source["has_credential"] and source["verification_status"] == "not_checked"
    assert TOKEN not in json.dumps(source) and "ciphertext" not in json.dumps(source)
    async with runs.sessions() as session:
        row = await session.get(CodeSourceModel, UUID(str(source["id"])))
        assert row and row.credential_ciphertext and TOKEN not in row.credential_ciphertext
        assert runs.app.state.code_source_service.secrets.decrypt(row.credential_ciphertext) == TOKEN
    request = {key: source[key] for key in ("name", "provider", "api_origin", "organization", "enabled", "version")}
    changed = await runs.client.put(path(runs) + "/" + str(source["id"]), json=request, headers=runs.headers())
    assert changed.status_code == 200 and changed.json()["has_credential"]
    stale = await runs.client.put(path(runs) + "/" + str(source["id"]), json=request, headers=runs.headers())
    assert stale.status_code == 409
    assert TOKEN not in (await runs.client.get(path(runs))).text
    denied = await runs.client.put(path(runs) + "/" + str(source["id"]), json=request)
    assert denied.status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize(
    "origin",
    [
        "https://attacker.test",
        "https://openapi-rdc.aliyuncs.com/path",
        "https://openapi-rdc.aliyuncs.com:443",
        "http://openapi-rdc.aliyuncs.com",
    ],
)
async def test_api_token_cannot_be_sent_to_custom_host(runs: RunsHarness, origin: str) -> None:
    response = await runs.client.post(
        path(runs),
        json={
            "name": "不可用",
            "organization": "org-test",
            "api_origin": origin,
            "api_token": TOKEN,
        },
        headers=runs.headers(),
    )
    assert response.status_code == 422
    assert TOKEN not in response.text


@pytest.mark.anyio
async def test_source_binding_and_revocation_invalidate_old_task(runs: RunsHarness) -> None:
    source = await create_source(runs)
    p = await project(runs)
    repo = await repository(
        runs,
        p["id"],
        "server",
        provider="codeup",
        git_url="https://codeup.aliyun.com/org-test/team/server.git",
        code_source_id=source["id"],
        platform_repository="123",
    )
    created = await task(runs, p["id"], [repo["id"]])
    assert created.status_code == 201, created.text
    assert TOKEN not in created.text
    binding = created.json()["project_context"]["repositories"][0]
    assert binding["code_source_id"] == source["id"] and binding["code_source_version"] == 1
    request = {key: source[key] for key in ("name", "provider", "api_origin", "organization", "enabled", "version")}
    disabled = await runs.client.put(
        path(runs) + "/" + str(source["id"]), json=request | {"enabled": False}, headers=runs.headers()
    )
    assert disabled.status_code == 200
    hidden = await runs.client.get(f"{runs.path}/{created.json()['id']}")
    assert hidden.status_code == 404
    with pytest.raises(ProjectError, match="已变化"):
        await runs.app.state.code_source_service.identity(
            ProjectActor(UUID(runs.user_id), UUID(runs.workspace_id)), UUID(str(source["id"])), 1
        )


@pytest.mark.anyio
async def test_repository_rejects_other_organization_and_unknown_build_profile(runs: RunsHarness) -> None:
    source = await create_source(runs)
    p = await project(runs)
    base = {
        "name": "web",
        "provider": "codeup",
        "git_url": "https://codeup.aliyun.com/other/team/web.git",
        "code_source_id": source["id"],
        "platform_repository": "123",
    }
    response = await runs.client.post(
        f"{projects_path(runs)}/{p['id']}/repositories", json=base, headers=runs.headers()
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == "INVALID_CODE_SOURCE"
    response = await runs.client.post(
        f"{projects_path(runs)}/{p['id']}/repositories",
        json={
            "name": "web",
            "git_url": "https://git.example.test/web.git",
            "build_config": {"profile": "project", "commands": [["mvn", "verify"]]},
        },
        headers=runs.headers(),
    )
    assert response.status_code == 409 and "BUILD_PROFILE_UNAVAILABLE" in response.text


@pytest.mark.anyio
async def test_build_advice_frozen_and_workspace_repository_overview(runs: RunsHarness) -> None:
    p = await project(runs)
    service = runs.app.state.project_service
    service.build_profiles["project"] = ("测试环境", "registry.test/java@sha256:" + "a" * 64)
    repo = await repository(runs, p["id"], "api", build_config={"profile": "project", "commands": [["mvn", "verify"]]})
    created = await task(runs, p["id"], [repo["id"]])
    assert created.status_code == 201
    frozen = created.json()["project_context"]["repositories"][0]
    assert frozen["build_config"]["commands"] == [["mvn", "verify"]]
    assert len(frozen["build_profile_fingerprint"]) == 64
    overview = await runs.client.get(path(runs) + "/repositories")
    assert overview.status_code == 200
    assert overview.json()["items"][0]["project_name"] == p["name"]
    profiles = await runs.client.get(path(runs) + "/build-profiles")
    assert profiles.json()["items"] == [{"id": "project", "name": "测试环境"}]
    assert "registry.test" not in profiles.text


@pytest.mark.anyio
async def test_mr_adapter_uses_separate_database_identity_and_refuses_rotation(
    runs: RunsHarness, tmp_path: Path
) -> None:
    source = await create_source(runs)
    p = await project(runs)
    repo = await repository(
        runs,
        p["id"],
        "server",
        provider="codeup",
        git_url="https://codeup.aliyun.com/org-test/team/server.git",
        code_source_id=source["id"],
        platform_repository="123",
    )
    service = runs.app.state.project_service
    frozen = await service.snapshot(
        ProjectActor(UUID(runs.user_id), UUID(runs.workspace_id)), UUID(str(p["id"])), (UUID(str(repo["id"])),)
    )
    docker = CaptureDocker(snapshot(tmp_path))
    bridge = ProjectExecution(
        service, runs.app.state.run_service, docker, enabled=True, code_sources=runs.app.state.code_source_service
    )
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    scope = PlanScope(
        str(repo["id"]),
        "a" * 40,
        ("src",),
        (),
        ("review.create",),
        remote_id="project-" + str(repo["id"]),
        target_branch="main",
    )
    connection = await bridge._review_connection(actor, frozen, scope)
    provider = await bridge._review_adapter(actor, frozen, scope)
    assert connection.platform_repository == "123" and connection.organization == "org-test"
    assert provider.credentials.get(connection.credential_ref).api_token == TOKEN
    assert TOKEN not in repr(provider.credentials.get(connection.credential_ref))
    # 显式绑定的身份不能因依赖漏装配而回退到部署级凭据。
    missing_source_bridge = ProjectExecution(service, runs.app.state.run_service, docker, enabled=True)
    with pytest.raises(DeliveryError, match="不能回退"):
        await missing_source_bridge._review_connection(actor, frozen, scope)
    with pytest.raises(DeliveryError, match="不能回退"):
        await missing_source_bridge._review_adapter(actor, frozen, scope)
    request = {key: source[key] for key in ("name", "provider", "api_origin", "organization", "enabled", "version")}
    changed = await runs.client.put(
        path(runs) + "/" + str(source["id"]),
        json=request | {"organization": "another-org", "api_token": "new-unit-test-token"},
        headers=runs.headers(),
    )
    assert changed.status_code == 200
    with pytest.raises(DeliveryError, match="已变更"):
        await bridge._review_connection(actor, frozen, scope)
    new_task = await task(runs, p["id"], [repo["id"]], "new-after-org-change")
    assert new_task.status_code == 409 and "RUN_PROJECT_SELECTION_INVALID" in new_task.text
    with pytest.raises(ProjectError) as unavailable:
        await service.snapshot(
            ProjectActor(actor.user_id, actor.workspace_id), UUID(str(p["id"])), (UUID(str(repo["id"])),)
        )
    assert unavailable.value.code == "CODE_SOURCE_CHANGED"
    assert not docker.calls


@pytest.mark.anyio
async def test_image_selection_is_frozen_and_cannot_be_silently_replaced(runs: RunsHarness, tmp_path: Path) -> None:
    service = runs.app.state.project_service
    image = "sha256:" + "b" * 64
    service.build_profiles["project"] = ("测试构建环境", image)
    p = await project(runs)
    repo = await repository(runs, p["id"], "server", build_config={"profile": "project", "commands": []})
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    frozen = await service.snapshot(
        ProjectActor(actor.user_id, actor.workspace_id), UUID(str(p["id"])), (UUID(str(repo["id"])),)
    )
    docker = CaptureDocker(snapshot(tmp_path))
    bridge = ProjectExecution(service, runs.app.state.run_service, docker, enabled=True)
    scope = PlanScope(str(repo["id"]), "a" * 40, ("src",), (), ("file.write",))
    assert bridge._image(frozen, scope) == image
    service.build_profiles["project"] = ("新环境", "sha256:" + "c" * 64)
    with pytest.raises(DeliveryError, match="构建环境已变化"):
        bridge._image(frozen, scope)
    assert not docker.calls
