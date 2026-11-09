"""旧配置接入走真实 HTTP 与事务，不回显秘密、不改变 Git 身份、不覆盖页面管理结果。"""

import json
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select, update

from tests.api.test_projects import project, repository, task
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.bootstrap.code_hosting import HostingConnectionSettings
from weora.bootstrap.legacy_code_sources import LegacyCodeSources
from weora.modules.context.feishu import FernetSecrets
from weora.modules.identity.infrastructure.models import WorkspaceMembershipModel
from weora.modules.projects.persistence import CodeSourceModel, ProjectRepositoryModel

TOKEN = "synthetic-legacy-api-identity-not-real"
GIT_SECRET = "synthetic-git-password-not-real"


def path(h: RunsHarness) -> str:
    return f"/api/v1/workspaces/{h.workspace_id}/code-sources"


async def configured(h: RunsHarness) -> tuple[dict[str, object], dict[str, object]]:
    h.app.state.code_source_service.secrets = FernetSecrets(Fernet.generate_key().decode())
    h.app.state.project_service.secrets = h.app.state.code_source_service.secrets
    p = await project(h)
    repo = await repository(
        h,
        p["id"],
        "legacy",
        provider="codeup",
        git_url="https://codeup.aliyun.com/org-test/team/legacy.git",
        credential_token=GIT_SECRET,
        credential_username="test-user",
    )
    connection = HostingConnectionSettings(
        workspace_id=UUID(h.workspace_id),
        remote_id="project-" + str(repo["id"]),
        version="legacy-v1",
        provider="codeup",
        repository_id=str(repo["id"]),
        platform_repository="123",
        clone_url=str(repo["git_url"]),
        api_origin="https://openapi-rdc.aliyuncs.com",
        web_url=str(repo["git_url"]).removesuffix(".git"),
        credential_ref="legacy-test-ref",
        target_branches=("main",),
        organization="org-test",
    )
    settings = h.app.state.settings.model_copy(
        update={
            "hosting_connections": (connection,),
            "hosting_credentials": {"legacy-test-ref": SecretStr(json.dumps({"api_token": TOKEN}))},
        }
    )
    h.app.state.legacy_code_sources = LegacyCodeSources(
        settings, h.app.state.code_source_service, h.app.state.project_service
    )
    return p, repo


async def candidate(h: RunsHarness) -> dict[str, object]:
    result = await h.client.get(path(h) + "/legacy")
    assert result.status_code == 200
    assert TOKEN not in result.text and "legacy-test-ref" not in result.text
    row = result.json()["items"][0]
    return {k: row[k] for k in ("fingerprint", "repository_id", "repository_version")}


@pytest.mark.anyio
async def test_import_preserves_git_identity_invalidates_old_task_and_is_idempotent(runs: RunsHarness) -> None:
    p, repo = await configured(runs)
    old_task = await task(runs, p["id"], [repo["id"]])
    request = await candidate(runs)
    result = await runs.client.post(path(runs) + "/legacy/import", json=request, headers=runs.headers())
    assert result.status_code == 200, result.text
    assert TOKEN not in result.text and GIT_SECRET not in result.text
    assert not result.json()["already_imported"]
    source = result.json()["source"]
    again = await runs.client.post(path(runs) + "/legacy/import", json=request, headers=runs.headers())
    assert again.status_code == 200 and again.json()["already_imported"]
    assert again.json()["source"]["id"] == source["id"]
    assert not (await runs.client.get(path(runs) + "/legacy")).json()["items"]
    async with runs.sessions() as session:
        saved = await session.get(ProjectRepositoryModel, UUID(str(repo["id"])))
        assert saved and saved.version == int(str(repo["version"])) + 1
        assert saved.credential_username == "test-user" and saved.build_config is None
        assert runs.app.state.project_service.secrets.decrypt(saved.credential_ciphertext) == GIT_SECRET
        record = await session.get(CodeSourceModel, UUID(source["id"]))
        assert record and TOKEN not in record.credential_ciphertext
        assert runs.app.state.code_source_service.secrets.decrypt(record.credential_ciphertext) == TOKEN
    assert (await runs.client.get(f"{runs.path}/{old_task.json()['id']}")).status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["version", "fingerprint", "secret_field"])
async def test_invalid_import_is_atomic_and_cannot_supply_secrets(runs: RunsHarness, change: str) -> None:
    await configured(runs)
    request = await candidate(runs)
    if change == "version":
        request["repository_version"] = 900
    elif change == "fingerprint":
        request["fingerprint"] = "0" * 64
    else:
        request["api_token"] = TOKEN
    result = await runs.client.post(path(runs) + "/legacy/import", json=request, headers=runs.headers())
    assert result.status_code == (422 if change == "secret_field" else 409)
    assert TOKEN not in result.text
    async with runs.sessions() as session:
        assert not (await session.scalars(select(CodeSourceModel))).all()
        assert not (await session.scalars(select(ProjectRepositoryModel.code_source_id))).first()


@pytest.mark.anyio
async def test_import_is_admin_and_csrf_protected(runs: RunsHarness) -> None:
    await configured(runs)
    request = await candidate(runs)
    assert (await runs.client.post(path(runs) + "/legacy/import", json=request)).status_code == 403
    async with runs.sessions() as session:
        await session.execute(
            update(WorkspaceMembershipModel)
            .where(WorkspaceMembershipModel.user_id == UUID(runs.user_id))
            .values(role="member")
        )
        await session.commit()
    assert not (await runs.client.get(path(runs) + "/legacy")).json()["items"]
    assert (
        await runs.client.post(path(runs) + "/legacy/import", json=request, headers=runs.headers())
    ).status_code == 403


@pytest.mark.anyio
async def test_reimport_never_overwrites_rotation_or_reenables_source(runs: RunsHarness) -> None:
    await configured(runs)
    request = await candidate(runs)
    source = (await runs.client.post(path(runs) + "/legacy/import", json=request, headers=runs.headers())).json()[
        "source"
    ]
    payload = {k: source[k] for k in ("name", "provider", "api_origin", "organization", "enabled", "version")}
    rotated = await runs.client.put(
        path(runs) + "/" + source["id"],
        json=payload | {"api_token": "synthetic-rotated-identity"},
        headers=runs.headers(),
    )
    assert rotated.status_code == 200
    assert (
        await runs.client.post(path(runs) + "/legacy/import", json=request, headers=runs.headers())
    ).status_code == 200
    async with runs.sessions() as session:
        row = await session.get(CodeSourceModel, UUID(source["id"]))
        assert (
            runs.app.state.code_source_service.secrets.decrypt(row.credential_ciphertext)
            == "synthetic-rotated-identity"
        )
    disabled = await runs.client.put(
        path(runs) + "/" + source["id"], json=payload | {"version": 2, "enabled": False}, headers=runs.headers()
    )
    assert disabled.status_code == 200
    retry = await runs.client.post(path(runs) + "/legacy/import", json=request, headers=runs.headers())
    assert retry.status_code == 409 and "CODE_SOURCE_CHANGED" in retry.text
