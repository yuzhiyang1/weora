"""不读取开发者本地配置的测试 Settings。"""

from pydantic_settings import SettingsConfigDict

from weora.bootstrap.settings import AppSettings


class IsolatedAppSettings(AppSettings):
    """只接受测试显式传入的配置，避免本地 `.env` 改变断言语义。"""

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="WEORA_ISOLATED_TEST_",
        extra="ignore",
    )
