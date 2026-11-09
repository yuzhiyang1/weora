"""通过 HTTP 和真实事务覆盖设置权限、秘密、版本与任务快照。"""

import base64
import json
from collections.abc import AsyncIterator
from sys import executable
from types import TracebackType
from uuid import UUID, uuid4

import aiohttp
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select, update

from tests.api.test_projects import project, repository
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.bootstrap.application import create_app
from weora.bootstrap.console_settings import model_profile, resolve_run_profile
from weora.modules.console_settings.domain import GitPreferences, ModelPreferences, SettingsActor, SettingsError
from weora.modules.console_settings.store import SettingVersionModel
from weora.modules.context.feishu import FernetSecrets
from weora.modules.identity.infrastructure.models import WorkspaceMembershipModel
from weora.modules.runs.application.harness import OMP_RUNTIME, PI_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile, RunActor


def path(harness: RunsHarness, kind: str) -> str:
    return f"/api/v1/workspaces/{harness.workspace_id}/settings/{kind}"


def model_input(revision: int = 0, **changes: object) -> dict[str, object]:
    return {
        "revision": revision,
        "name": "团队模型",
        "provider": "deepseek",
        "model_id": "deepseek-chat",
        "max_tokens": 4096,
        "timeout_seconds": 120,
        "execution_timeout_seconds": 1800,
    } | changes


@pytest.mark.anyio
async def test_settings_csrf_role_and_workspace_boundaries(runs: RunsHarness) -> None:
    assert (await runs.client.get(path(runs, "model"))).status_code == 200
    harness = path(runs, "harness")
    payload = {"harness": "oh-my-pi", "revision": 0}
    assert (await runs.client.get(harness)).json() == {"harness": "pi", "revision": 0, "omp_available": False}
    assert (await runs.client.put(harness, json=payload)).status_code == 403
    assert (await runs.client.put(harness, json=payload, headers=runs.headers())).status_code == 409
    assert (await runs.client.put(path(runs, "model"), json=model_input())).status_code == 403
    foreign = f"/api/v1/workspaces/{uuid4()}/settings/model"
    assert (await runs.client.get(foreign)).status_code == 403
    async with runs.sessions() as session, session.begin():
        await session.execute(
            update(WorkspaceMembershipModel)
            .where(WorkspaceMembershipModel.user_id == UUID(runs.user_id))
            .values(role="member")
        )
    assert (await runs.client.get(harness)).status_code == 403
    assert (await runs.client.put(harness, json=payload, headers=runs.headers())).status_code == 403
    assert (await runs.client.get(path(runs, "git"))).status_code == 200
    assert (await runs.client.get(path(runs, "model"))).status_code == 403
    assert (await runs.client.put(path(runs, "model"), json=model_input(), headers=runs.headers())).status_code == 403


@pytest.mark.anyio
async def test_model_key_is_write_only_versioned_and_cas_protected(runs: RunsHarness) -> None:
    service = runs.app.state.console_settings_service
    service.cipher = FernetSecrets(Fernet.generate_key().decode())
    first = await runs.client.put(path(runs, "model"), json=model_input(api_key="secret-first"), headers=runs.headers())
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 1 and first.json()["has_key"]
    assert "secret-first" not in first.text and "encrypted_key" not in first.text
    frozen = await service.resolve_model(UUID(runs.workspace_id))
    second = await runs.client.put(
        path(runs, "model"), json=model_input(1, api_key="secret-second", timeout_seconds=200), headers=runs.headers()
    )
    assert second.status_code == 200
    conflict = await runs.client.put(
        path(runs, "model"), json=model_input(1, api_key="stale-secret"), headers=runs.headers()
    )
    assert conflict.status_code == 409
    old = await service.resolve_model(UUID(runs.workspace_id), frozen.configuration_id)
    assert old.api_key == "secret-first" and old.preferences.timeout_seconds == 120
    base = ExecutionProfile("pi-agent-core@1.3.0", "deepseek", "deepseek-chat", 4096, 120, 1800)
    fixed = model_profile(old, base)
    resolved, key = await resolve_run_profile(service, UUID(runs.workspace_id), fixed, base)
    assert resolved == fixed and key == "secret-first"
    with pytest.raises(SettingsError):
        await service.resolve_model(uuid4(), frozen.configuration_id)
    async with runs.sessions() as session:
        rows = (await session.scalars(select(SettingVersionModel).where(SettingVersionModel.kind == "models"))).all()
        assert len(rows) == 2
        assert all("secret-" not in (row.encrypted_key or "") and "api_key" not in row.values for row in rows)


@pytest.mark.anyio
async def test_key_omission_preserves_clear_does_not_fall_back(runs: RunsHarness) -> None:
    service = runs.app.state.console_settings_service
    service.cipher = FernetSecrets(Fernet.generate_key().decode())
    service._deployment_key = "deployment-secret"
    await runs.client.put(path(runs, "model"), json=model_input(api_key="saved-secret"), headers=runs.headers())
    kept = await runs.client.put(path(runs, "model"), json=model_input(1), headers=runs.headers())
    assert kept.status_code == 200 and kept.json()["has_key"]
    assert (await service.resolve_model(UUID(runs.workspace_id))).api_key == "saved-secret"
    cleared = await runs.client.put(path(runs, "model"), json=model_input(2, api_key=""), headers=runs.headers())
    assert cleared.status_code == 200 and not cleared.json()["has_key"]
    assert (await service.resolve_model(UUID(runs.workspace_id))).api_key == ""
    probe = await runs.client.post(path(runs, "model/check"), json={"revision": 3}, headers=runs.headers())
    assert probe.status_code == 400 and probe.json()["detail"]["code"] == "MODEL_KEY_REQUIRED"


@pytest.mark.anyio
async def test_missing_encryption_and_invalid_inputs_never_echo_key(runs: RunsHarness) -> None:
    secret = "should-not-echo"
    response = await runs.client.put(path(runs, "model"), json=model_input(api_key=secret), headers=runs.headers())
    assert response.status_code == 503 and secret not in response.text
    invalid = await runs.client.put(
        path(runs, "model"), json=model_input(api_key=secret + " with-space", timeout_seconds=0), headers=runs.headers()
    )
    assert invalid.status_code == 422 and secret not in invalid.text
    result = await runs.client.get(path(runs, "model"))
    assert result.json()["revision"] == 0


@pytest.mark.anyio
async def test_git_preferences_validate_identity_and_reject_stale_write(runs: RunsHarness) -> None:
    values = {
        "revision": 0,
        "name_mode": "manual",
        "email_mode": "manual",
        "manual_name": "研发协作",
        "manual_email": "dev@example.com",
        "co_authored_by": True,
    }
    result = await runs.client.put(path(runs, "git"), json=values, headers=runs.headers())
    assert result.status_code == 200 and result.json()["revision"] == 1
    assert (await runs.client.put(path(runs, "git"), json=values, headers=runs.headers())).status_code == 409
    invalid = values | {"revision": 1, "manual_name": "dev\nCo-authored-by: attacker"}
    assert (await runs.client.put(path(runs, "git"), json=invalid, headers=runs.headers())).status_code == 422


@pytest.mark.anyio
async def test_profile_updates_only_current_user_and_preserves_session(runs: RunsHarness) -> None:
    response = await runs.client.put(
        "/api/v1/auth/profile", json={"display_name": "新的显示名"}, headers=runs.headers()
    )
    assert response.status_code == 200 and response.json()["user"]["display_name"] == "新的显示名"
    restored = await runs.client.get("/api/v1/auth/me")
    assert restored.json()["user"]["display_name"] == "新的显示名"
    assert restored.json()["user"]["id"] == runs.user_id
    for invalid in (
        {"display_name": " "},
        {"display_name": "合法", "user_id": str(uuid4())},
        {"display_name": "合法", "email": "new@example.com"},
        {"display_name": "合法", "avatar": "data:image/svg+xml;base64,PHN2Zz4="},
    ):
        assert (await runs.client.put("/api/v1/auth/profile", json=invalid, headers=runs.headers())).status_code == 422
    assert (await runs.client.put("/api/v1/auth/profile", json={"display_name": "未授权"})).status_code == 403


@pytest.mark.anyio
async def test_new_tasks_freeze_team_model_and_git_identity_while_replay_keeps_old(runs: RunsHarness) -> None:
    """走真实应用装配，不能仅验证设置表的写入而遗漏执行层消费。"""
    key = SecretStr(Fernet.generate_key().decode())
    settings = runs.app.state.settings.model_copy(
        update={
            "run_runtime": "pi",
            "pi_model": "deepseek-chat",
            "pi_runtime_entry": __file__,
            "omp_runtime_entry": __file__,
            "omp_bun_executable": executable,
            "deepseek_api_key": SecretStr("deployment-only"),
            "context_encryption_key": key,
            "context_cursor_key": key,
        }
    )
    app = create_app(settings, session_factory=runs.sessions, object_storage_factory=lambda _: None)
    service = app.state.console_settings_service
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    settings_actor = SettingsActor(actor.user_id, actor.workspace_id)

    await service.save_git(settings_actor, 0, GitPreferences("actor_agent", "actor", co_authored_by=True))
    await service.save_model(
        settings_actor, 0, ModelPreferences(model_id="deepseek-chat", timeout_seconds=160), "first-key"
    )
    p = await project(runs)
    repo = await repository(runs, p["id"], "web")
    arguments = dict(
        actor=actor,
        workspace_id=actor.workspace_id,
        agent_id=UUID(runs.agent_id),
        agent_version="0.1.0",
        instruction="验证团队设置快照",
        idempotency_key="settings-task",
        request_id="settings-test",
        project_id=UUID(str(p["id"])),
        repository_ids=(UUID(str(repo["id"])),),
    )
    detail, created = await app.state.run_service.create(**arguments)
    assert created and detail.run.execution_profile.timeout_seconds == 160
    assert detail.run.execution_profile.runtime_id == PI_RUNTIME
    selected = await service.save_harness(settings_actor, 0, "oh-my-pi")
    assert selected["revision"] == 1
    with pytest.raises(SettingsError) as conflict:
        await service.save_harness(settings_actor, 0, "pi")
    assert conflict.value.code == "SETTINGS_CONFLICT"
    identity = detail.task.project_context["git_identity"]
    assert identity["email"] == "run-owner@example.com"
    assert " & " in identity["name"] and "agents-noreply@weora.local" in identity["co_author"]
    await service.save_model(
        settings_actor, 1, ModelPreferences(model_id="deepseek-chat", timeout_seconds=220), "second-key"
    )
    await service.save_git(settings_actor, 1, GitPreferences())
    replay, created_again = await app.state.run_service.create(**arguments)
    assert not created_again and replay.run.execution_profile == detail.run.execution_profile
    assert replay.task.project_context["git_identity"] == identity
    current = await app.state.run_service.create(**(arguments | {"idempotency_key": "settings-task-next"}))
    assert current[0].run.execution_profile.timeout_seconds == 220
    frozen = current[0].run.execution_profile
    assert frozen.runtime_id == OMP_RUNTIME
    await service.save_harness(settings_actor, 1, "pi")
    deployment = app.state.run_service._execution_profile
    resolved, key = await resolve_run_profile(service, actor.workspace_id, frozen, deployment)
    assert resolved == frozen and key == "second-key"
    assert current[0].task.project_context["git_identity"]["name"] == "Weora"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"data": [{"id": "deepseek-chat"}]}, 200),
        (200, {"data": []}, 422),
        (200, {"data": None}, 502),
        (200, {"data": [], "padding": "x" * 65536}, 502),
        (401, {"secret": "probe-only-key"}, 502),
    ],
)
async def test_model_probe_uses_fixed_origin_and_safe_errors(
    runs: RunsHarness, monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, object], expected: int
) -> None:
    """脚本化官方响应，测试不访问外网，也不发送真实密钥。"""
    service = runs.app.state.console_settings_service
    service.cipher = FernetSecrets(Fernet.generate_key().decode())
    saved = await runs.client.put(
        path(runs, "model"), json=model_input(api_key="probe-only-key"), headers=runs.headers()
    )
    assert saved.status_code == 200

    class Content:
        async def iter_chunked(self, limit: int) -> AsyncIterator[bytes]:
            # 模拟网络分段，确保不能把第一次读取误当作完整 JSON。
            assert limit == 8192
            encoded = json.dumps(body).encode()
            for offset in range(0, len(encoded), 7):
                yield encoded[offset : offset + 7]

    class Probe:
        content = Content()

        def __init__(self) -> None:
            self.status = status

        async def __aenter__(self) -> "Probe":
            return self

        async def __aexit__(
            self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
        ) -> None:
            pass

    class Client:
        def __init__(self, **options: object) -> None:
            assert options["trust_env"] is False

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(
            self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
        ) -> None:
            pass

        def get(self, url: str, *, allow_redirects: bool, headers: dict[str, str]) -> Probe:
            assert url == "https://api.deepseek.com/models" and not allow_redirects
            assert headers == {"Authorization": "Bearer probe-only-key"}
            return Probe()

    monkeypatch.setattr(aiohttp, "ClientSession", Client)
    result = await runs.client.post(path(runs, "model/check"), json={"revision": 1}, headers=runs.headers())
    assert result.status_code == expected, result.text
    assert "probe-only-key" not in result.text and "secret" not in result.text


@pytest.mark.anyio
async def test_unreadable_key_keeps_admin_rotation_available(runs: RunsHarness) -> None:
    """失效密钥不能挡住设置入口，也不能静默替换已固定运行的旧版本。"""
    service = runs.app.state.console_settings_service
    service.cipher = FernetSecrets(Fernet.generate_key().decode())
    await runs.client.put(path(runs, "model"), json=model_input(api_key="old-key"), headers=runs.headers())
    frozen = await service.resolve_model(UUID(runs.workspace_id))
    service.cipher = FernetSecrets(Fernet.generate_key().decode())

    visible = await runs.client.get(path(runs, "model"))
    assert visible.status_code == 200 and visible.json()["has_key"]
    probe = await runs.client.post(path(runs, "model/check"), json={"revision": 1}, headers=runs.headers())
    assert probe.status_code == 503
    rotated = await runs.client.put(path(runs, "model"), json=model_input(1, api_key="new-key"), headers=runs.headers())
    assert rotated.status_code == 200 and "new-key" not in rotated.text
    assert (await service.resolve_model(UUID(runs.workspace_id))).api_key == "new-key"
    with pytest.raises(SettingsError, match="无法解密"):
        await service.resolve_model(UUID(runs.workspace_id), frozen.configuration_id)


@pytest.mark.anyio
async def test_private_avatar_can_be_saved_reloaded_and_removed(runs: RunsHarness) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    value = "data:image/png;base64," + base64.b64encode(png).decode()
    response = await runs.client.put(
        "/api/v1/auth/profile", json={"display_name": "头像用户", "avatar": value}, headers=runs.headers()
    )
    assert response.status_code == 200 and response.json()["user"]["avatar_url"].startswith("/api/v1/auth/avatar?v=")
    image = await runs.client.get(response.json()["user"]["avatar_url"])
    assert image.content == png and image.headers["cache-control"] == "private, no-store"
    assert "avatar" not in (await runs.client.get("/api/v1/auth/me")).json()["user"]
    await runs.client.put(
        "/api/v1/auth/profile", json={"display_name": "头像用户", "avatar": None}, headers=runs.headers()
    )
    assert (await runs.client.get("/api/v1/auth/avatar")).status_code == 404
