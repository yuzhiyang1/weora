"""Identity 应用层依赖的持久化和安全端口。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class UserRecord:
    """应用层需要的用户认证数据。"""

    id: UUID
    display_name: str
    email: str
    password_hash: str
    status: str


@dataclass(frozen=True, slots=True)
class PrincipalRecord:
    """一次已认证请求所需的最小用户和工作空间上下文。"""

    user_id: UUID
    display_name: str
    email: str
    workspace_id: UUID
    workspace_name: str
    role: str
    avatar_revision: str | None = None


@dataclass(frozen=True, slots=True)
class RevokedSessionRecord:
    """退出时写审计事件所需的会话归属。"""

    user_id: UUID
    email: str


class IdentityStore(Protocol):
    async def workspace_member_names(self, workspace_id: UUID) -> list[tuple[UUID, str]]: ...
    """Identity 聚合在一个事务内需要的数据库操作。"""

    async def get_user_by_email(self, email: str) -> UserRecord | None: ...

    async def update_profile(
        self, user_id: UUID, name: str, avatar: str | None, update_avatar: bool, now: datetime
    ) -> None: ...

    async def get_avatar(self, user_id: UUID) -> str | None: ...

    async def create_registration(
        self,
        *,
        user_id: UUID,
        display_name: str,
        email: str,
        password_hash: str,
        workspace_id: UUID,
        workspace_name: str,
        occurred_at: datetime,
    ) -> None: ...

    async def create_session(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        token_hash: str,
        csrf_token_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None: ...

    async def get_principal_by_session(
        self,
        *,
        token_hash: str,
        now: datetime,
    ) -> PrincipalRecord | None: ...

    async def get_principal_by_session_and_csrf(
        self,
        *,
        token_hash: str,
        csrf_token_hash: str,
        now: datetime,
    ) -> PrincipalRecord | None: ...

    async def get_principal_by_user_id(
        self, user_id: UUID, *, workspace_id: UUID | None = None
    ) -> PrincipalRecord | None: ...

    async def revoke_session(
        self,
        *,
        token_hash: str,
        csrf_token_hash: str,
        revoked_at: datetime,
    ) -> RevokedSessionRecord | None: ...

    async def add_auth_event(
        self,
        *,
        event_id: UUID,
        event_type: str,
        subject: str,
        user_id: UUID | None,
        request_id: str,
        occurred_at: datetime,
    ) -> None: ...

    async def count_recent_login_failures(self, *, subject: str, since: datetime) -> int: ...


class IdentityUnitOfWork(Protocol):
    """保证注册相关写入要么全部成功，要么全部回滚。"""

    store: IdentityStore

    async def __aenter__(self) -> "IdentityUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


IdentityUnitOfWorkFactory = Callable[[], IdentityUnitOfWork]


class PasswordHasher(Protocol):
    """密码摘要算法端口。"""

    def hash(self, password: str) -> str: ...

    def verify(self, password: str, password_hash: str | None) -> bool: ...


class TokenManager(Protocol):
    """高熵会话令牌签发与摘要端口。"""

    def issue(self) -> str: ...

    def digest(self, token: str) -> str: ...
