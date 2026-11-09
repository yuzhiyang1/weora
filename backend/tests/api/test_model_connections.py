"""多 Key 的切换必须贯穿 HTTP、版本存储和真实运行配置装配。"""

import json
from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from types import TracebackType
from typing import cast
from uuid import UUID

import aiohttp
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, update

from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.bootstrap.console_settings import model_profile, resolve_run_profile
from weora.modules.console_settings.domain import ModelPreferences, SettingValue
from weora.modules.console_settings.providers import PROVIDERS
from weora.modules.console_settings.store import SettingVersionModel
from weora.modules.context.feishu import FernetSecrets
from weora.modules.identity.infrastructure.models import WorkspaceMembershipModel
from weora.modules.runs.application.ports import ExecutionProfile


def path(harness: RunsHarness, suffix: str = "") -> str:
    return f"/api/v1/workspaces/{harness.workspace_id}/settings/model-connections{suffix}"


def values(revision: int, key: str, **changes: object) -> dict[str, object]:
    return {
        "revision": revision,
        "name": "DeepSeek 备用",
        "provider": "deepseek",
        "model_id": "deepseek-chat",
        "api_key": key,
    } | changes


@pytest.mark.anyio
async def test_nullable_budget_roundtrips_and_freezes_deployment_defaults(runs: RunsHarness) -> None:
    service = runs.app.state.console_settings_service
    service.cipher = FernetSecrets(Fernet.generate_key().decode())
    first = await runs.client.post(path(runs), json=values(0, "test-key", make_primary=True), headers=runs.headers())
    identifier = first.json()["primary_id"]
    budget_path = f"/api/v1/workspaces/{runs.workspace_id}/settings/model-budget"
    empty = {"max_tokens": None, "timeout_seconds": None, "execution_timeout_seconds": None}
    saved = await runs.client.put(budget_path, json={"revision": 1, **empty}, headers=runs.headers())
    assert saved.status_code == 200, saved.text
    assert saved.json()["budget"] == empty
    assert saved.json()["effective_budget"]["max_tokens"] == service.defaults.max_tokens
    assert (await runs.client.get(path(runs))).json()["budget"] == empty

    # 空值保留在表单契约中，执行版本必须已经解析成明确的整数预算。
    config = await service.resolve_model(UUID(runs.workspace_id))
    original = service.defaults
    assert config.preferences.max_tokens == original.max_tokens
    assert config.preferences.timeout_seconds == original.timeout_seconds
    base = ExecutionProfile("pi-agent-core@1.3.0", "deepseek", "deepseek-chat", 4096, 120, 1800)
    frozen = model_profile(config, base)
    service.defaults = replace(original, max_tokens=8000, timeout_seconds=300, execution_timeout_seconds=3000)
    resolved, key = await resolve_run_profile(service, UUID(runs.workspace_id), frozen, base)
    assert resolved == frozen and key == "test-key"

    # 仅切换主要项不能重新解析预算，否则旧任务之外的新任务也会被意外改变。
    selected = await runs.client.post(
        path(runs, f"/{identifier}/primary"), json={"revision": 2}, headers=runs.headers()
    )
    assert selected.status_code == 200
    unchanged = await service.resolve_model(UUID(runs.workspace_id))
    assert unchanged.preferences.timeout_seconds == original.timeout_seconds
    legacy = await runs.client.get(f"/api/v1/workspaces/{runs.workspace_id}/settings/model")
    assert legacy.json()["timeout_seconds"] == original.timeout_seconds

    # 再次保存预算时才使用新的部署默认值；显式填写的项覆盖该项默认值。
    partial = await runs.client.put(
        budget_path,
        json={"revision": 3, **empty, "max_tokens": 900},
        headers=runs.headers(),
    )
    assert partial.status_code == 200 and partial.json()["budget"]["timeout_seconds"] is None
    current = await service.resolve_model(UUID(runs.workspace_id))
    assert current.preferences.max_tokens == 900
    assert current.preferences.timeout_seconds == 300
    assert (
        await service.resolve_model(UUID(runs.workspace_id), config.configuration_id)
    ).preferences == config.preferences
    assert (
        await runs.client.put(budget_path, json={"revision": 3, **empty}, headers=runs.headers())
    ).status_code == 409


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field,value",
    [
        ("max_tokens", 0),
        ("max_tokens", 16385),
        ("timeout_seconds", -1),
        ("timeout_seconds", 601),
        ("execution_timeout_seconds", 3601),
    ],
)
async def test_nullable_budget_still_validates_explicit_bounds(runs: RunsHarness, field: str, value: int) -> None:
    budget_path = f"/api/v1/workspaces/{runs.workspace_id}/settings/model-budget"
    response = await runs.client.put(
        budget_path,
        json={
            "revision": 0,
            "max_tokens": None,
            "timeout_seconds": None,
            "execution_timeout_seconds": None,
            field: value,
        },
        headers=runs.headers(),
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_multiple_keys_switch_and_rotation_keep_historical_runs_frozen(runs: RunsHarness) -> None:
    service = runs.app.state.console_settings_service
    service.cipher = FernetSecrets(Fernet.generate_key().decode())
    first = await runs.client.post(
        path(runs), json=values(0, "first-key", name="主 Key", make_primary=True), headers=runs.headers()
    )
    assert first.status_code == 200, first.text
    first_id = first.json()["primary_id"]
    frozen_first = await service.resolve_model(UUID(runs.workspace_id))
    second = await runs.client.post(path(runs), json=values(1, "second-key"), headers=runs.headers())
    second_id = second.json()["items"][-1]["id"]
    assert second.json()["primary_id"] == first_id
    third = await runs.client.post(
        path(runs),
        json=values(2, "openai-key", name="OpenAI 备用", provider="openai", model_id="gpt-4.1-mini"),
        headers=runs.headers(),
    )
    third_id = third.json()["items"][-1]["id"]
    switched = await runs.client.post(path(runs, f"/{third_id}/primary"), json={"revision": 3}, headers=runs.headers())
    assert switched.status_code == 200 and switched.json()["primary_id"] == third_id
    current = await service.resolve_model(UUID(runs.workspace_id))
    assert current.api_key == "openai-key" and current.preferences.provider == "openai"
    base = ExecutionProfile("pi-agent-core@1.3.0", "deepseek", "deepseek-chat", 4096, 120, 1800)
    profile = model_profile(current, base)
    resolved, key = await resolve_run_profile(service, UUID(runs.workspace_id), profile, base)
    assert resolved.provider == "openai" and key == "openai-key"

    await runs.client.post(path(runs, f"/{second_id}/primary"), json={"revision": 4}, headers=runs.headers())
    frozen_second = await service.resolve_model(UUID(runs.workspace_id))
    rotated = await runs.client.put(
        path(runs, f"/{second_id}"), json=values(5, "recharged-key"), headers=runs.headers()
    )
    assert rotated.status_code == 200
    assert (await service.resolve_model(UUID(runs.workspace_id))).api_key == "recharged-key"
    assert (
        await service.resolve_model(UUID(runs.workspace_id), frozen_second.configuration_id)
    ).api_key == "second-key"
    removed = await runs.client.post(path(runs, f"/{first_id}/remove"), json={"revision": 6}, headers=runs.headers())
    assert removed.status_code == 200
    assert first_id not in {item["id"] for item in removed.json()["items"]}
    assert (await service.resolve_model(UUID(runs.workspace_id), frozen_first.configuration_id)).api_key == "first-key"
    assert (
        await runs.client.post(path(runs, f"/{second_id}/remove"), json={"revision": 7}, headers=runs.headers())
    ).status_code == 409
    assert (
        await runs.client.post(path(runs, f"/{third_id}/primary"), json={"revision": 3}, headers=runs.headers())
    ).status_code == 409
    for response in [first, second, third, switched, rotated, removed]:
        assert all(secret not in response.text for secret in ["first-key", "second-key", "openai-key", "recharged-key"])
    async with runs.sessions() as session:
        rows = (await session.scalars(select(SettingVersionModel).where(SettingVersionModel.kind == "models"))).all()
        assert len(rows) == 7
        assert all("api_key" not in json.dumps(row.values) and "-key" not in (row.encrypted_key or "") for row in rows)


@pytest.mark.anyio
async def test_new_catalog_endpoints_preserve_csrf_roles_and_key_boundaries(runs: RunsHarness) -> None:
    service = runs.app.state.console_settings_service
    service.cipher = FernetSecrets(Fernet.generate_key().decode())
    assert (await runs.client.post(path(runs), json=values(0, "private-key"))).status_code == 403
    saved = await runs.client.post(path(runs), json=values(0, "private-key", make_primary=True), headers=runs.headers())
    identifier = saved.json()["primary_id"]
    # 改成另一家厂商时不能把原来的密钥自动带过去。
    no_key = values(1, "", provider="openai", model_id="gpt-4.1-mini")
    no_key.pop("api_key")
    assert (await runs.client.put(path(runs, f"/{identifier}"), json=no_key, headers=runs.headers())).status_code == 422
    assert (await runs.client.post(path(runs), json=values(1, "unsafe key"), headers=runs.headers())).status_code == 422
    assert (
        await runs.client.post(path(runs), json=values(1, "private-key", provider="custom"), headers=runs.headers())
    ).status_code == 422
    assert (
        await runs.client.post(
            path(runs), json=values(1, "private-key", model_id="unknown-model"), headers=runs.headers()
        )
    ).status_code == 422
    async with runs.sessions() as session, session.begin():
        await session.execute(
            update(WorkspaceMembershipModel)
            .where(WorkspaceMembershipModel.user_id == UUID(runs.user_id))
            .values(role="member")
        )
    assert (await runs.client.get(path(runs))).status_code == 403
    assert (
        await runs.client.post(path(runs, f"/{identifier}/primary"), json={"revision": 1}, headers=runs.headers())
    ).status_code == 403


@pytest.mark.anyio
async def test_old_single_configuration_is_imported_without_rewriting_its_version(runs: RunsHarness) -> None:
    service = runs.app.state.console_settings_service
    service.cipher = FernetSecrets(Fernet.generate_key().decode())
    old = await service.store.save(
        UUID(runs.workspace_id),
        UUID(runs.user_id),
        "model",
        0,
        cast(dict[str, SettingValue], asdict(ModelPreferences(name="原有配置"))),
        service.cipher.encrypt("old-key"),
    )
    visible = await runs.client.get(path(runs))
    assert visible.status_code == 200 and visible.json()["items"][0]["name"] == "原有配置"
    saved = await runs.client.post(path(runs), json=values(0, "backup-key"), headers=runs.headers())
    assert saved.status_code == 200 and saved.json()["primary_id"] == "legacy"
    assert (await service.resolve_model(UUID(runs.workspace_id))).api_key == "old-key"
    assert (await service.resolve_model(UUID(runs.workspace_id), str(old.id))).api_key == "old-key"


@pytest.mark.anyio
async def test_one_broken_key_does_not_block_rotation_of_another(runs: RunsHarness) -> None:
    service = runs.app.state.console_settings_service
    service.cipher = FernetSecrets(Fernet.generate_key().decode())
    await runs.client.post(path(runs), json=values(0, "broken-key", make_primary=True), headers=runs.headers())
    backup = await runs.client.post(path(runs), json=values(1, "backup-key"), headers=runs.headers())
    identifier = backup.json()["items"][-1]["id"]
    service.cipher = FernetSecrets(Fernet.generate_key().decode())
    assert (await runs.client.get(path(runs))).status_code == 200
    fixed = await runs.client.put(path(runs, f"/{identifier}"), json=values(2, "rotated-key"), headers=runs.headers())
    assert fixed.status_code == 200
    assert (
        await runs.client.post(path(runs, f"/{identifier}/primary"), json={"revision": 3}, headers=runs.headers())
    ).status_code == 200
    assert (await service.resolve_model(UUID(runs.workspace_id))).api_key == "rotated-key"


@pytest.mark.anyio
@pytest.mark.parametrize("provider", [item["id"] for item in PROVIDERS])
async def test_provider_probe_uses_only_official_origin_and_correct_auth(
    runs: RunsHarness, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    service = runs.app.state.console_settings_service
    service.cipher = FernetSecrets(Fernet.generate_key().decode())
    option = next(item for item in PROVIDERS if item["id"] == provider)
    model = option["models"][0]["id"]
    saved = await runs.client.post(
        path(runs),
        json=values(0, "probe-private", provider=provider, model_id=model, make_primary=True),
        headers=runs.headers(),
    )
    assert saved.status_code == 200
    identifier = saved.json()["primary_id"]

    class Content:
        async def iter_chunked(self, limit: int) -> AsyncIterator[bytes]:
            yield json.dumps(
                {"data": [{"id": model}], "id": "resolved-model-version", "name": "models/resolved-model-version"}
            ).encode()

    class Probe:
        status = 200
        content = Content()

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
            origins = {
                "deepseek": "https://api.deepseek.com/",
                "openai": "https://api.openai.com/",
                "anthropic": "https://api.anthropic.com/",
                "google": "https://generativelanguage.googleapis.com/",
                "moonshotai-cn": "https://api.moonshot.cn/",
            }
            assert url.startswith(origins[provider]) and not allow_redirects and "probe-private" not in url
            expected = {"Authorization": "Bearer probe-private"}
            if provider == "anthropic":
                expected = {"x-api-key": "probe-private", "anthropic-version": "2023-06-01"}
            elif provider == "google":
                expected = {"x-goog-api-key": "probe-private"}
            assert headers == expected
            return Probe()

    monkeypatch.setattr(aiohttp, "ClientSession", Client)
    result = await runs.client.post(path(runs, f"/{identifier}/check"), json={"revision": 1}, headers=runs.headers())
    assert result.status_code == 200 and "probe-private" not in result.text
