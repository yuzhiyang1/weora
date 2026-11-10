"""运行默认关闭，密钥只留在后端，配置错误不得回显输入值。"""

import pytest
from pydantic import SecretStr, ValidationError

from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.run_profile import execution_profile


def test_runtime_is_disabled_by_default() -> None:
    assert execution_profile(IsolatedAppSettings()) is None


@pytest.mark.parametrize("missing", ["pi_model", "pi_runtime_entry", "deepseek_api_key"])
def test_enabled_runtime_requires_explicit_configuration_and_hides_secrets(missing: str) -> None:
    values: dict[str, object] = {
        "run_runtime": "pi",
        "pi_model": "configured-model",
        "pi_runtime_entry": "/trusted/runtime/index.js",
        "deepseek_api_key": "secret-must-not-appear",
    }
    values.pop(missing)
    with pytest.raises(ValidationError) as error:
        IsolatedAppSettings.model_validate(values)
    assert "secret-must-not-appear" not in str(error.value)


def test_profile_never_contains_credentials() -> None:
    settings = IsolatedAppSettings(
        run_runtime="pi",
        pi_model="configured-model",
        pi_runtime_entry="/trusted/runtime/index.js",
        deepseek_api_key=SecretStr("secret-must-not-appear"),
    )
    profile = execution_profile(settings)
    assert profile is not None
    assert profile.runtime_id == "pi-agent-core@1.3.0"
    assert profile.model_id == "configured-model"
    assert "secret-must-not-appear" not in repr(profile)


def test_runtime_can_use_only_workspace_model_keys() -> None:
    values = {
        "run_runtime": "pi",
        "pi_model": "configured-model",
        "pi_runtime_entry": "/trusted/runtime/index.js",
        "run_workspace_model_keys_only": True,
    }
    settings = IsolatedAppSettings.model_validate(values)
    assert execution_profile(settings) is not None
    assert settings.deepseek_api_key is None

    with pytest.raises(ValidationError, match="不能配置全局 DeepSeek 密钥"):
        IsolatedAppSettings.model_validate({**values, "deepseek_api_key": "secret-must-not-appear"})


def test_project_build_budget_requires_separate_image() -> None:
    with pytest.raises(ValidationError, match="项目资源预算必须绑定"):
        IsolatedAppSettings(project_sandbox_resources={"memory_mb": 3072})
    settings = IsolatedAppSettings(
        project_sandbox_image="sha256:" + "c" * 64,
        project_sandbox_resources={"memory_mb": 3072, "timeout_seconds": 600},
    )
    assert settings.project_sandbox_resources.memory_mb == 3072


@pytest.mark.parametrize("values", [{"memory_mb": 4097}, {"timeout_seconds": 601}, {"network": "host"}])
def test_project_budget_rejects_invalid_limits_and_extra_capabilities(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        IsolatedAppSettings(project_sandbox_image="sha256:" + "c" * 64, project_sandbox_resources=values)
