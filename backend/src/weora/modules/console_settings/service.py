"""编排团队设置、不可变版本和只读模型连接探测。"""

from dataclasses import asdict
from typing import Literal, cast
from uuid import UUID

from weora.modules.console_settings.domain import (
    GitPreferences,
    Memberships,
    ModelConfiguration,
    ModelPreferences,
    SecretCipher,
    SettingsActor,
    SettingsError,
    SettingsRepository,
    SettingValue,
    SettingVersion,
)
from weora.modules.console_settings.model_catalog import ModelBudget, ModelCatalogService
from weora.modules.console_settings.model_probe import probe_model
from weora.modules.runs.application.harness import HarnessId


class ConsoleSettingsService:
    """配置不授予执行权；新设置仅在新运行创建时冻结。"""

    def __init__(
        self,
        store: SettingsRepository,
        memberships: Memberships,
        cipher: SecretCipher | None,
        defaults: ModelPreferences,
        deployment_key: str = "",
        runtime_enabled: bool = False,
        omp_available: bool = False,
    ) -> None:
        self.store, self.memberships, self.cipher = store, memberships, cipher
        self.defaults, self._deployment_key = defaults, deployment_key
        self.runtime_enabled = runtime_enabled
        self.omp_available = omp_available

    def deployment_key(self) -> str:
        """仅供兼容无控制台版本引用的历史运行，HTTP 层不调用。"""
        return self._deployment_key

    async def authorize(self, actor: SettingsActor, *, manager: bool = False) -> None:
        """每次请求重新验证活动成员，团队配置写入仅允许所有者和管理员。"""
        role = await self.memberships.role(actor)
        if role is None or (manager and role not in {"owner", "admin"}):
            raise SettingsError("SETTINGS_FORBIDDEN", "没有管理这项团队配置的权限", 403)

    async def selected_harness(self, workspace_id: UUID) -> tuple[HarnessId, int]:
        """兼容未配置的工作空间，只有新运行会查询当前选择。"""
        row = await self.store.read(workspace_id, "harness")
        if row is None:
            return "pi", 0
        return cast(HarnessId, row.values["harness"]), row.revision

    async def harness(self, actor: SettingsActor) -> dict[str, object]:
        await self.authorize(actor, manager=True)
        selected, revision = await self.selected_harness(actor.workspace_id)
        return {"harness": selected, "revision": revision, "omp_available": self.omp_available}

    async def save_harness(self, actor: SettingsActor, revision: int, selected: HarnessId) -> dict[str, object]:
        await self.authorize(actor, manager=True)
        if selected == "oh-my-pi":
            config = await self.resolve_model(actor.workspace_id)
            if not self.omp_available or config.preferences.provider != "deepseek" or not config.api_key:
                raise SettingsError("HARNESS_UNAVAILABLE", "Oh My Pi 需要可用的部署入口和 DeepSeek 模型连接", 409)
        row = await self.store.save(actor.workspace_id, actor.user_id, "harness", revision, {"harness": selected})
        return {"harness": selected, "revision": row.revision, "omp_available": self.omp_available}

    async def git(self, actor: SettingsActor) -> tuple[GitPreferences, int]:
        await self.authorize(actor)
        row = await self.store.read(actor.workspace_id, "git")
        if row is None:
            return GitPreferences(), 0
        return GitPreferences(
            name_mode=cast(Literal["actor_agent", "actor", "environment", "manual"], row.values["name_mode"]),
            email_mode=cast(Literal["actor", "environment", "manual"], row.values["email_mode"]),
            manual_name=str(row.values["manual_name"]),
            manual_email=str(row.values["manual_email"]),
            co_authored_by=bool(row.values["co_authored_by"]),
        ), row.revision

    async def save_git(self, actor: SettingsActor, revision: int, values: GitPreferences) -> int:
        """保存署名偏好，不读取宿主个人 Git 配置，不触发提交或推送。"""
        await self.authorize(actor, manager=True)
        row = await self.store.save(
            actor.workspace_id,
            actor.user_id,
            "git",
            revision,
            cast(dict[str, SettingValue], asdict(values)),
        )
        return row.revision

    def _preferences(self, row: SettingVersion | None) -> ModelPreferences:
        """公开设置读取无需解密，旧密钥失效时管理员仍能进入页面轮换。"""
        if row is None:
            return self.defaults
        values = row.values
        return ModelPreferences(
            name=str(values["name"]),
            provider=str(values.get("provider", "deepseek")),
            model_id=str(values["model_id"]),
            max_tokens=int(cast(int, values["max_tokens"])),
            timeout_seconds=int(cast(int, values["timeout_seconds"])),
            execution_timeout_seconds=int(cast(int, values["execution_timeout_seconds"])),
        )

    def _model(self, row: SettingVersion | None) -> ModelConfiguration:
        """从固定配置版本恢复执行参数；秘密仅返回内部装配层。"""
        if row is None:
            return ModelConfiguration(self.defaults, api_key=self._deployment_key)
        preferences = self._preferences(row)
        key = self._deployment_key if row.values.get("inherit_deployment_key") else ""
        if row.encrypted_key:
            if self.cipher is None:
                raise SettingsError("SETTINGS_ENCRYPTION_UNAVAILABLE", "部署尚未配置凭据加密密钥", 503)
            try:
                key = self.cipher.decrypt(row.encrypted_key)
            except Exception:
                raise SettingsError("MODEL_KEY_UNAVAILABLE", "模型密钥无法解密，请管理员重新配置", 503) from None
        return ModelConfiguration(preferences, str(row.id), key)

    def _models(self) -> ModelCatalogService:
        return ModelCatalogService(self.store, self.cipher, self.defaults, self._deployment_key, self.runtime_enabled)

    async def model(self, actor: SettingsActor) -> dict[str, object]:
        """兼容旧查询，公开投影不需要解密。"""
        await self.authorize(actor, manager=True)
        models = self._models()
        return models.legacy_public(await models.load(actor.workspace_id))

    async def save_model(
        self, actor: SettingsActor, revision: int, values: ModelPreferences, api_key: str | None
    ) -> dict[str, object]:
        await self.authorize(actor, manager=True)
        models = self._models()
        return models.legacy_public(await models.save_legacy(actor, revision, values, api_key))

    async def model_connections(self, actor: SettingsActor) -> dict[str, object]:
        await self.authorize(actor, manager=True)
        models = self._models()
        return models.public(await models.load(actor.workspace_id))

    async def save_connection(
        self,
        actor: SettingsActor,
        revision: int,
        name: str,
        provider: str,
        model_id: str,
        api_key: str | None,
        identifier: str | None = None,
        make_primary: bool = False,
    ) -> dict[str, object]:
        await self.authorize(actor, manager=True)
        models = self._models()
        saved = await models.save_connection(
            actor, revision, name, provider, model_id, api_key, identifier, make_primary
        )
        return models.public(saved)

    async def select_connection(self, actor: SettingsActor, revision: int, identifier: str) -> dict[str, object]:
        await self.authorize(actor, manager=True)
        models = self._models()
        return models.public(await models.select(actor, revision, identifier))

    async def remove_connection(self, actor: SettingsActor, revision: int, identifier: str) -> dict[str, object]:
        await self.authorize(actor, manager=True)
        models = self._models()
        return models.public(await models.remove(actor, revision, identifier))

    async def save_model_budget(self, actor: SettingsActor, revision: int, budget: ModelBudget) -> dict[str, object]:
        await self.authorize(actor, manager=True)
        models = self._models()
        return models.public(await models.save_budget(actor, revision, budget))

    async def resolve_model(self, workspace_id: UUID, version_id: str | None = None) -> ModelConfiguration:
        """仅供受信任执行装配调用；显式版本不能跨团队或退回当前配置。"""
        try:
            identifier = UUID(version_id) if version_id else None
        except ValueError:
            raise SettingsError("MODEL_CONFIGURATION_UNAVAILABLE", "模型配置版本无效", 409) from None
        catalog_row = await self.store.read(workspace_id, "models", identifier)
        if catalog_row:
            models = self._models()
            return models.configuration(models.decode(catalog_row))
        row = await self.store.read(workspace_id, "model", identifier)
        if version_id is not None and row is None:
            raise SettingsError("MODEL_CONFIGURATION_UNAVAILABLE", "运行固定的模型配置不可用", 409)
        return self._model(row)

    async def check_model(
        self, actor: SettingsActor, revision: int, identifier: str | None = None
    ) -> dict[str, object]:
        """检测指定已保存连接；目录在网络等待期间改变时不能把旧结果标为新配置可用。"""
        await self.authorize(actor, manager=True)
        models = self._models()
        catalog = await models.load(actor.workspace_id, revision)
        config = models.configuration(catalog, identifier)
        if not config.api_key:
            raise SettingsError("MODEL_KEY_REQUIRED", "请先保存模型 API Key")
        await probe_model(config.preferences.provider, config.preferences.model_id, config.api_key)
        await models.load(actor.workspace_id, revision)
        return {"status": "connected", "revision": revision, "model_id": config.preferences.model_id}
