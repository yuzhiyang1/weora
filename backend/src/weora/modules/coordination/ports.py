"""应用层只认识事务端口，不依赖 PostgreSQL ORM 或 Pi 进程实现。"""

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from weora.modules.coordination.domain import Config, Effect, State
from weora.modules.coordination.engine import Engine


class CoordinationStore(Protocol):
    async def scopes(self, workspace_id: UUID | None = None) -> list[tuple[UUID, str]]: ...
    async def snapshot(self, workspace_id: UUID, scope_id: str) -> State: ...
    async def config_for_routes(self, workspace_id: UUID, routes: tuple[str, ...]) -> Config | None: ...
    async def transact[T](
        self, workspace_id: UUID, scope_id: str, action: Callable[[Engine, State], T], *, create: Config | None = None
    ) -> T: ...
    async def claim(
        self, workspace_id: UUID, scope_id: str, effect_id: str, owner: str, *, seconds: int = 120
    ) -> Effect | None: ...
    async def settle(
        self, workspace_id: UUID, scope_id: str, effect: Effect, *, error: str | None = None, unknown: bool = False
    ) -> None: ...
    async def renew(self, workspace_id: UUID, scope_id: str, effect: Effect, *, seconds: int = 60) -> bool: ...
    async def defer(
        self, workspace_id: UUID, scope_id: str, effect: Effect, *, delay: int = 2, error: str | None = None
    ) -> None: ...
