"""参考上游 github-trigger/github-pull/poll：代码托管事实进入原 Ticket，不携带执行授权。"""

from dataclasses import dataclass, field
from typing import Literal, Protocol

from weora.modules.coordination.domain import Json
from weora.modules.delivery.application.ports import ReviewReference
from weora.modules.runs.application.ports import RunActor


@dataclass(frozen=True)
class CodeEvent:
    id: str
    kind: str
    text: str
    at: str


@dataclass(frozen=True)
class ReviewSnapshot:
    outcome: Literal["open", "closed", "merged"]
    at: str
    events: tuple[CodeEvent, ...]
    cursor: Json = field(default_factory=dict)
    closed_at: str | None = None


class CodeEventReader(Protocol):
    async def authorize(self, actor: RunActor, reference: ReviewReference) -> None:
        """读取已缓存正文前重新验证连接版本、项目权限及评审绑定。"""
        ...

    async def read(self, actor: RunActor, reference: ReviewReference, cursor: Json) -> ReviewSnapshot:
        """通过已配置连接读取白名单评审，失败不能当成关闭、删除或没有新消息。"""
        ...
