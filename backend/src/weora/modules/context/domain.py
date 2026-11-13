"""上下文中立契约；群消息作者不是 Weora 登录身份。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

Json = dict[str, object]
ParseStatus = Literal["complete", "partial", "unsupported", "recalled"]


class ContextError(Exception):
    """安全错误码可以交给界面与模型，供应商原始错误不得回显。"""

    def __init__(self, code: str, message: str = "上下文暂不可用") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ReplyNotSent(ContextError):
    """发送请求尚未提交，可安全重试。"""


@dataclass(frozen=True, slots=True)
class ContextActor:
    user_id: UUID
    workspace_id: UUID


@dataclass(frozen=True, slots=True)
class ContextRunScope:
    run_id: UUID
    actor: ContextActor
    audience: Literal["private", "group"] = "private"
    source_id: UUID | None = None
    cutoff: datetime | None = None
    origin_open_id: str | None = None
    source_kind: Literal["feishu", "code_hosting"] = "feishu"


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    chat_id: str
    thread_id: str | None
    author_id: str
    created_at: datetime
    text: str
    content_type: str = "text"
    # None 仅兼容旧适配器/测试消息；新读取结果须明确正文是否已完整解析。
    parse_status: ParseStatus | None = None
    parse_notes: tuple[str, ...] = ()
    # 仅保留白名单元数据；资源不下载、不解析，时长 duration 的单位为毫秒。
    resources: tuple[Json, ...] = ()
    # 补偿读取必须保留路由和提及事实，不能把机器人或引用当作新的显式请求。
    root_id: str | None = None
    parent_id: str | None = None
    mentions: tuple[Json, ...] = ()
    human: bool = True


@dataclass(frozen=True, slots=True)
class MessagePage:
    items: tuple[Message, ...]
    next_token: str | None = None


class ContextAccess(Protocol):
    async def is_active(self, actor: ContextActor) -> bool: ...
    async def can_manage(self, actor: ContextActor) -> bool: ...
    async def can_access_run(self, scope: ContextRunScope) -> bool: ...


class ContextStore(Protocol):
    async def get(self, workspace_id: UUID, kind: str, key: str) -> Json | None: ...
    async def list(self, workspace_id: UUID, kind: str) -> list[Json]: ...
    async def change(self, workspace_id: UUID, kind: str, key: str, update: Callable[[Json | None], Json]) -> Json: ...


class SecretCipher(Protocol):
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...


class FeishuReader(Protocol):
    async def probe(self, connection: Json, chat_id: str | None = None) -> Json: ...
    async def messages(
        self,
        connection: Json,
        *,
        chat_id: str,
        start: datetime,
        end: datetime,
        page_token: str | None,
        thread_id: str | None = None,
        descending: bool = False,
    ) -> MessagePage: ...
    async def message(self, connection: Json, message_id: str) -> Message: ...
    async def upload_image(self, connection: Json, png: bytes) -> str: ...
    async def reply(
        self, connection: Json, message_id: str, text: str, business_key: str, *, image_keys: tuple[str, ...] = ()
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Json


# 回调必须以 inbox_id 作为任务提交幂等键；渠道模块不得直接写 runs 表。
InboundSubmit = Callable[[ContextActor, Json], Awaitable[str | Json]]

# 仅决定是否保存未 @ 候选；真正接单仍须绑定身份、来源授权与冻结路由全部通过。
CandidateAdmission = Callable[[UUID, UUID, str, str | None, str | None], Awaitable[bool]]
