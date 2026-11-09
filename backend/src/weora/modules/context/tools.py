"""四个只读工具；身份、运行、连接和凭据均不可由模型参数指定。"""

from datetime import datetime
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from weora.modules.context.domain import ContextError, ContextRunScope, Json, ToolSpec
from weora.modules.context.service import ContextService


class SourcesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection_id: UUID | None = None


class MessagesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    start_time: datetime | None = None
    end_time: datetime | None = None
    cursor: str | None = Field(default=None, max_length=12000)
    limit: int = Field(default=30, ge=1, le=50)


class ThreadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    thread_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    cursor: str | None = Field(default=None, max_length=12000)
    limit: int = Field(default=30, ge=1, le=50)


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    message_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


TOOL_SPECS = (
    ToolSpec(
        "context.sources.list",
        "列出本运行获授权的飞书来源。名称多义时先向用户澄清，不扫描全部群。",
        cast(Json, SourcesInput.model_json_schema()),
    ),
    ToolSpec(
        "feishu.messages.list",
        "按 UTC 时间窗口读取群历史；partial 不代表没有其他相关讨论，按游标继续。",
        cast(Json, MessagesInput.model_json_schema()),
    ),
    ToolSpec(
        "feishu.thread.read",
        "展开授权来源内话题，不能跨群；窗口固定为运行开始前七天。",
        cast(Json, ThreadInput.model_json_schema()),
    ),
    ToolSpec(
        "feishu.message.get",
        "读取同一授权来源内的明确引用消息；不下载附件。",
        cast(Json, MessageInput.model_json_schema()),
    ),
)


class ContextTools:
    specs = TOOL_SPECS

    def __init__(self, service: ContextService) -> None:
        self.service = service

    async def execute(self, scope: ContextRunScope, name: str, arguments: Json) -> Json:
        try:
            if name == "context.sources.list":
                query = SourcesInput.model_validate(arguments)
                await self.service.reserve_discovery(scope)
                return {"items": await self.service.sources(scope.actor, scope, query.collection_id)}
            if name == "feishu.messages.list":
                messages = MessagesInput.model_validate(arguments)
                return await self.service.read(
                    scope,
                    messages.source_id,
                    start_time=messages.start_time,
                    end_time=messages.end_time,
                    cursor=messages.cursor,
                    limit=messages.limit,
                )
            if name == "feishu.thread.read":
                thread = ThreadInput.model_validate(arguments)
                return await self.service.read(
                    scope, thread.source_id, thread_id=thread.thread_id, cursor=thread.cursor, limit=thread.limit
                )
            if name == "feishu.message.get":
                message = MessageInput.model_validate(arguments)
                return await self.service.read(scope, message.source_id, message_id=message.message_id)
            raise ContextError("TOOL_NOT_ALLOWED", "此运行不支持该上下文工具")
        except ValidationError:
            raise ContextError("INVALID_PARAMETERS", "上下文工具参数无效，不能指定身份或凭据") from None
        except ContextError as error:
            # 权限与覆盖缺口成为模型能辨识的事实，但不暴露供应商响应正文。
            return {
                "status": "unavailable",
                "messages": [],
                "coverage": {
                    "window_complete": False,
                    "truncated": False,
                    "reason": error.code,
                },
                "error": {"code": error.code, "message": error.message},
            }
