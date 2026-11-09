"""多连接目录复用设置版本存储；每次修改原子保存元数据、主要选择和加密 Key。"""

import json
from dataclasses import asdict, dataclass, field, replace
from typing import cast
from uuid import UUID, uuid4

from weora.modules.console_settings.domain import (
    ModelConfiguration,
    ModelPreferences,
    SecretCipher,
    SettingsActor,
    SettingsError,
    SettingsRepository,
    SettingValue,
    SettingVersion,
)
from weora.modules.console_settings.providers import PROVIDERS, validate_model


@dataclass(frozen=True)
class ModelConnection:
    id: str
    name: str
    provider: str
    model_id: str


@dataclass(frozen=True)
class ExecutionBudget:
    """配置版本内的实际预算，不随之后的部署默认值变化。"""

    max_tokens: int
    timeout_seconds: int
    execution_timeout_seconds: int


@dataclass(frozen=True)
class ModelBudget:
    """控制台输入；空项保留继承语义，执行时使用同版本冻结的实际预算。"""

    max_tokens: int | None
    timeout_seconds: int | None
    execution_timeout_seconds: int | None

    def resolve(self, defaults: ModelPreferences) -> ExecutionBudget:
        return ExecutionBudget(
            self.max_tokens if self.max_tokens is not None else defaults.max_tokens,
            self.timeout_seconds if self.timeout_seconds is not None else defaults.timeout_seconds,
            self.execution_timeout_seconds
            if self.execution_timeout_seconds is not None
            else defaults.execution_timeout_seconds,
        )


@dataclass(frozen=True)
class ModelCatalog:
    """目录版本包含全部连接；密文分开保存，列表投影不能反序列化秘密。"""

    revision: int
    version_id: str | None
    connections: tuple[ModelConnection, ...]
    primary_id: str
    budget: ModelBudget
    execution_budget: ExecutionBudget
    encrypted_keys: dict[str, str] = field(repr=False)
    inherit_deployment: bool = False

    def connection(self, identifier: str) -> ModelConnection:
        found = next((item for item in self.connections if item.id == identifier), None)
        if found is None:
            raise SettingsError("MODEL_CONNECTION_NOT_FOUND", "模型配置已不存在，请刷新列表", 404)
        return found


class ModelCatalogService:
    """由 ConsoleSettingsService 完成管理员鉴权后调用，执行读取仅接受可信团队 ID。"""

    def __init__(
        self,
        store: SettingsRepository,
        cipher: SecretCipher | None,
        defaults: ModelPreferences,
        deployment_key: str,
        runtime_enabled: bool,
    ) -> None:
        self.store, self.cipher = store, cipher
        self.defaults, self.deployment_key, self.runtime_enabled = defaults, deployment_key, runtime_enabled

    @staticmethod
    def decode(row: SettingVersion) -> ModelCatalog:
        """秘密容器只包含逐条加密串，轮换一条 Key 不需要解密其他连接。"""
        try:
            keys = json.loads(row.encrypted_key or "{}")
            if not isinstance(keys, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in keys.items()
            ):
                raise ValueError
            values = row.values
            entries = cast(list[dict[str, str]], values["connections"])
            return ModelCatalog(
                revision=row.revision,
                version_id=str(row.id),
                connections=tuple(ModelConnection(**item) for item in entries),
                primary_id=str(values["primary_id"]),
                budget=ModelBudget(
                    cast(int | None, values["max_tokens"]),
                    cast(int | None, values["timeout_seconds"]),
                    cast(int | None, values["execution_timeout_seconds"]),
                ),
                # 旧目录只有显式整数；新目录同时保存空项和已解析的预算。
                execution_budget=ExecutionBudget(
                    int(cast(int, values.get("effective_max_tokens", values["max_tokens"]))),
                    int(cast(int, values.get("effective_timeout_seconds", values["timeout_seconds"]))),
                    int(
                        cast(
                            int, values.get("effective_execution_timeout_seconds", values["execution_timeout_seconds"])
                        )
                    ),
                ),
                encrypted_keys=keys,
            )
        except (ValueError, TypeError, KeyError):
            raise SettingsError("MODEL_CONFIGURATION_UNAVAILABLE", "模型配置版本不可用，请联系管理员", 503) from None

    async def load(self, workspace_id: UUID, revision: int | None = None) -> ModelCatalog:
        """旧单连接按原值呈现，第一次写入才转为目录；旧版本行保持可解析。"""
        row = await self.store.read(workspace_id, "models")
        if row:
            catalog = self.decode(row)
        else:
            previous = await self.store.read(workspace_id, "model")
            values = previous.values if previous else cast(dict[str, SettingValue], asdict(self.defaults))
            catalog = ModelCatalog(
                revision=0,
                version_id=None,
                connections=(
                    ModelConnection(
                        "legacy", str(values["name"]), str(values.get("provider", "deepseek")), str(values["model_id"])
                    ),
                ),
                primary_id="legacy",
                budget=ModelBudget(
                    int(cast(int, values["max_tokens"])),
                    int(cast(int, values["timeout_seconds"])),
                    int(cast(int, values["execution_timeout_seconds"])),
                ),
                execution_budget=ExecutionBudget(
                    int(cast(int, values["max_tokens"])),
                    int(cast(int, values["timeout_seconds"])),
                    int(cast(int, values["execution_timeout_seconds"])),
                ),
                encrypted_keys={"legacy": previous.encrypted_key} if previous and previous.encrypted_key else {},
                inherit_deployment=previous is None or bool(values.get("inherit_deployment_key")),
            )
        if revision is not None and catalog.revision != revision:
            raise SettingsError("SETTINGS_CONFLICT", "配置已被更新，请刷新列表后重试", 409)
        return catalog

    def public(self, catalog: ModelCatalog) -> dict[str, object]:
        return {
            "revision": catalog.revision,
            "primary_id": catalog.primary_id,
            "items": [asdict(item) | self.key_status(catalog, item.id) for item in catalog.connections],
            "budget": asdict(catalog.budget),
            "effective_budget": asdict(catalog.execution_budget),
            "providers": PROVIDERS,
            "runtime_enabled": self.runtime_enabled,
        }

    def key_status(self, catalog: ModelCatalog, identifier: str) -> dict[str, object]:
        inherited = identifier == "legacy" and catalog.inherit_deployment and bool(self.deployment_key)
        saved = bool(catalog.encrypted_keys.get(identifier))
        return {
            "has_key": saved or inherited,
            "key_source": "saved" if saved else "deployment" if inherited else "missing",
        }

    def key(self, catalog: ModelCatalog, identifier: str) -> str:
        encrypted = catalog.encrypted_keys.get(identifier)
        if not encrypted:
            return self.deployment_key if identifier == "legacy" and catalog.inherit_deployment else ""
        if self.cipher is None:
            raise SettingsError("SETTINGS_ENCRYPTION_UNAVAILABLE", "部署尚未配置凭据加密密钥", 503)
        try:
            return self.cipher.decrypt(encrypted)
        except Exception:
            raise SettingsError("MODEL_KEY_UNAVAILABLE", "模型密钥无法解密，请管理员重新配置", 503) from None

    def configuration(self, catalog: ModelCatalog, identifier: str | None = None) -> ModelConfiguration:
        entry = catalog.connection(identifier or catalog.primary_id)
        return ModelConfiguration(
            ModelPreferences(
                name=entry.name, provider=entry.provider, model_id=entry.model_id, **asdict(catalog.execution_budget)
            ),
            catalog.version_id,
            self.key(catalog, entry.id),
        )

    def legacy_public(self, catalog: ModelCatalog) -> dict[str, object]:
        entry = catalog.connection(catalog.primary_id)
        return {
            "name": entry.name,
            "provider": entry.provider,
            "model_id": entry.model_id,
            **asdict(catalog.execution_budget),
            **self.key_status(catalog, entry.id),
            "revision": catalog.revision,
            "runtime_enabled": self.runtime_enabled,
        }

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        if self.cipher is None:
            raise SettingsError("SETTINGS_ENCRYPTION_UNAVAILABLE", "部署尚未配置凭据加密密钥", 503)
        return self.cipher.encrypt(plaintext)

    async def persist(self, actor: SettingsActor, catalog: ModelCatalog) -> ModelCatalog:
        """全目录使用同一个 CAS 指针，防止并发设置出两个主要 Key 或覆盖另一条编辑。"""
        keys = dict(catalog.encrypted_keys)
        if catalog.inherit_deployment and not keys.get("legacy") and self.deployment_key:
            keys["legacy"] = self.encrypt(self.deployment_key)
        values: dict[str, SettingValue] = {
            "connections": [asdict(item) for item in catalog.connections],
            "primary_id": catalog.primary_id,
            **asdict(catalog.budget),
            **{f"effective_{key}": value for key, value in asdict(catalog.execution_budget).items()},
        }
        saved = await self.store.save(
            actor.workspace_id,
            actor.user_id,
            "models",
            catalog.revision,
            values,
            json.dumps(keys, separators=(",", ":")),
        )
        return self.decode(saved)

    async def save_connection(
        self,
        actor: SettingsActor,
        revision: int,
        name: str,
        provider: str,
        model_id: str,
        api_key: str | None,
        identifier: str | None,
        make_primary: bool,
    ) -> ModelCatalog:
        """保存一组模型与 Key；跨厂商修改必须提供新 Key，绝不把原厂商的 Key 送到另一家。"""
        validate_model(provider, model_id)
        catalog = await self.load(actor.workspace_id, revision)
        previous = catalog.connection(identifier) if identifier else None
        if previous is None and len(catalog.connections) >= 100:
            raise SettingsError("MODEL_CONNECTION_LIMIT", "最多保存 100 条模型配置", 422)
        if previous and previous.provider != provider and not api_key:
            raise SettingsError("MODEL_KEY_REQUIRED", "切换厂商时请填写该厂商的 API Key", 422)
        identifier = identifier or str(uuid4())
        keys = dict(catalog.encrypted_keys)
        if api_key is not None:
            encrypted = self.encrypt(api_key)
            if encrypted:
                keys[identifier] = encrypted
            else:
                keys.pop(identifier, None)
        if previous is None and not keys.get(identifier):
            raise SettingsError("MODEL_KEY_REQUIRED", "新增配置请填写 API Key", 422)
        entry = ModelConnection(identifier, name, provider, model_id)
        entries = tuple(entry if item.id == identifier else item for item in catalog.connections)
        if previous is None:
            entries += (entry,)
        updated = replace(
            catalog,
            connections=entries,
            encrypted_keys=keys,
            primary_id=identifier if make_primary else catalog.primary_id,
            inherit_deployment=False if identifier == "legacy" and api_key is not None else catalog.inherit_deployment,
        )
        if make_primary and not self.key(updated, identifier):
            raise SettingsError("MODEL_KEY_REQUIRED", "主要配置必须有可用的 API Key", 422)
        return await self.persist(actor, updated)

    async def select(self, actor: SettingsActor, revision: int, identifier: str) -> ModelCatalog:
        catalog = await self.load(actor.workspace_id, revision)
        catalog.connection(identifier)
        if not self.key(catalog, identifier):
            raise SettingsError("MODEL_KEY_REQUIRED", "请先为该配置保存 API Key", 422)
        return await self.persist(actor, replace(catalog, primary_id=identifier))

    async def remove(self, actor: SettingsActor, revision: int, identifier: str) -> ModelCatalog:
        """只从新目录移除备用项；历史版本仍保留以服务已固定的运行。"""
        catalog = await self.load(actor.workspace_id, revision)
        catalog.connection(identifier)
        if catalog.primary_id == identifier:
            raise SettingsError("MODEL_PRIMARY_REQUIRED", "请先切换主要配置，再移除此项", 409)
        keys = {key: value for key, value in catalog.encrypted_keys.items() if key != identifier}
        return await self.persist(
            actor,
            replace(
                catalog,
                connections=tuple(item for item in catalog.connections if item.id != identifier),
                encrypted_keys=keys,
                inherit_deployment=False if identifier == "legacy" else catalog.inherit_deployment,
            ),
        )

    async def save_budget(self, actor: SettingsActor, revision: int, budget: ModelBudget) -> ModelCatalog:
        """仅预算保存时解析默认值；切换 Key 或编辑名称不得重新计算已冻结预算。"""
        catalog = await self.load(actor.workspace_id, revision)
        return await self.persist(
            actor, replace(catalog, budget=budget, execution_budget=budget.resolve(self.defaults))
        )

    async def save_legacy(
        self, actor: SettingsActor, revision: int, preferences: ModelPreferences, api_key: str | None
    ) -> ModelCatalog:
        """兼容原单配置 API，所有新写入都使用目录指针，避免两条写入链路相互覆盖。"""
        validate_model(preferences.provider, preferences.model_id)
        catalog = await self.load(actor.workspace_id, revision)
        current = catalog.connection(catalog.primary_id)
        if current.provider != preferences.provider and not api_key:
            raise SettingsError("MODEL_KEY_REQUIRED", "切换厂商时请填写该厂商的 API Key", 422)
        keys = dict(catalog.encrypted_keys)
        if api_key is None and keys.get(current.id):
            self.key(catalog, current.id)
        if api_key is not None:
            keys[current.id] = self.encrypt(api_key)
        entry = replace(current, name=preferences.name, provider=preferences.provider, model_id=preferences.model_id)
        return await self.persist(
            actor,
            replace(
                catalog,
                connections=tuple(entry if item.id == entry.id else item for item in catalog.connections),
                encrypted_keys=keys,
                budget=ModelBudget(
                    preferences.max_tokens, preferences.timeout_seconds, preferences.execution_timeout_seconds
                ),
                execution_budget=ExecutionBudget(
                    preferences.max_tokens, preferences.timeout_seconds, preferences.execution_timeout_seconds
                ),
                inherit_deployment=False
                if api_key is not None and entry.id == "legacy"
                else catalog.inherit_deployment,
            ),
        )
