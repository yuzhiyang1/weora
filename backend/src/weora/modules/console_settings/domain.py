"""团队设置的稳定数据契约，不依赖 HTTP 或 ORM。"""

from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID


class SettingsError(Exception):
    """仅传递可公开的错误，不保留供应商响应或密钥。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class SettingsActor:
    """身份由认证层提供，不接受请求正文指定其他用户。"""

    user_id: UUID
    workspace_id: UUID


class Memberships(Protocol):
    async def role(self, actor: SettingsActor) -> str | None: ...


class SecretCipher(Protocol):
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...


@dataclass(frozen=True)
class GitPreferences:
    """只决定提交署名，不能扩大 Git 写入或审批范围。"""

    name_mode: Literal["actor_agent", "actor", "environment", "manual"] = "environment"
    email_mode: Literal["actor", "environment", "manual"] = "environment"
    manual_name: str = ""
    manual_email: str = ""
    co_authored_by: bool = False


@dataclass(frozen=True)
class ModelPreferences:
    """公开模型参数；提供方由平台预设目录约束。"""

    name: str = "团队默认模型"
    provider: str = "deepseek"
    model_id: str = "deepseek-chat"
    max_tokens: int = 4096
    timeout_seconds: int = 120
    execution_timeout_seconds: int = 1800


@dataclass(frozen=True)
class ModelConfiguration:
    """内部执行配置，秘密不参与 repr，也不进入客户端响应。"""

    preferences: ModelPreferences
    configuration_id: str | None = None
    api_key: str = field(default="", repr=False)


SettingValue = str | int | bool | None | list[dict[str, str]]


@dataclass(frozen=True)
class SettingVersion:
    """设置应用层读取的固定版本；密钥字段不会进入 repr 或 HTTP 投影。"""

    id: UUID
    revision: int
    values: dict[str, SettingValue]
    encrypted_key: str | None = field(repr=False)


class SettingsRepository(Protocol):
    """应用服务只依赖版本读写契约；SQL/CAS 事务由持久化适配器负责。"""

    async def read(self, workspace_id: UUID, kind: str, version_id: UUID | None = None) -> SettingVersion | None: ...
    async def save(
        self,
        workspace_id: UUID,
        user_id: UUID,
        kind: str,
        revision: int,
        values: dict[str, SettingValue],
        encrypted_key: str | None = None,
    ) -> SettingVersion: ...
