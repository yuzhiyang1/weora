"""注册和会话恢复用例。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from weora.modules.identity.application.errors import (
    AuthenticationRequiredError,
    CsrfRejectedError,
    EmailUnavailableError,
    InvalidCredentialsError,
    LoginRateLimitedError,
    RegistrationDisabledError,
)
from weora.modules.identity.application.ports import (
    IdentityUnitOfWorkFactory,
    PasswordHasher,
    PrincipalRecord,
    TokenManager,
)


@dataclass(frozen=True, slots=True)
class RegisterCommand:
    """注册账户并创建第一个工作空间。"""

    display_name: str
    email: str
    password: str
    workspace_name: str
    request_id: str


@dataclass(frozen=True, slots=True)
class LoginCommand:
    """使用邮箱和密码创建新会话。"""

    email: str
    password: str
    request_id: str


@dataclass(frozen=True, slots=True)
class AuthResult:
    """认证成功后返回给 HTTP 层的上下文和原始 Cookie 令牌。"""

    principal: PrincipalRecord
    session_token: str
    csrf_token: str


class IdentityService:
    """编排身份用例，不感知 FastAPI 或 SQLAlchemy。"""

    def __init__(
        self,
        *,
        uow_factory: IdentityUnitOfWorkFactory,
        password_hasher: PasswordHasher,
        token_manager: TokenManager,
        password_verification_enabled: bool,
        registration_enabled: bool,
        session_ttl: timedelta,
        login_max_failures: int,
        login_failure_window: timedelta,
    ) -> None:
        self._uow_factory = uow_factory
        self._password_hasher = password_hasher
        self._token_manager = token_manager
        self._password_verification_enabled = password_verification_enabled
        self._registration_enabled = registration_enabled
        self._session_ttl = session_ttl
        self._login_max_failures = login_max_failures
        self._login_failure_window = login_failure_window

    async def update_profile(
        self, principal: PrincipalRecord, name: str, avatar: str | None, update_avatar: bool, request_id: str
    ) -> PrincipalRecord:
        """会话身份来自认证依赖；资料更新和审计在同一事务完成。"""
        now = datetime.now(UTC)
        async with self._uow_factory() as uow:
            await uow.store.update_profile(principal.user_id, name, avatar, update_avatar, now)
            await uow.store.add_auth_event(
                event_id=uuid4(),
                event_type="profile.updated",
                subject=principal.email,
                user_id=principal.user_id,
                request_id=request_id,
                occurred_at=now,
            )
            updated = await uow.store.get_principal_by_user_id(principal.user_id, workspace_id=principal.workspace_id)
            if updated is None:
                raise AuthenticationRequiredError
            await uow.commit()
            return updated

    async def avatar(self, user_id: UUID) -> str | None:
        async with self._uow_factory() as uow:
            return await uow.store.get_avatar(user_id)

    async def register(self, command: RegisterCommand) -> AuthResult:
        """原子创建用户、工作空间、OWNER 成员关系和登录会话。"""
        if not self._registration_enabled:
            raise RegistrationDisabledError

        email = command.email.strip().lower()
        now = datetime.now(UTC)
        user_id = uuid4()
        workspace_id = uuid4()
        session_id = uuid4()
        session_token = self._token_manager.issue()
        csrf_token = self._token_manager.issue()

        async with self._uow_factory() as uow:
            if await uow.store.get_user_by_email(email) is not None:
                raise EmailUnavailableError

            await uow.store.create_registration(
                user_id=user_id,
                display_name=command.display_name.strip(),
                email=email,
                password_hash=self._password_hasher.hash(command.password),
                workspace_id=workspace_id,
                workspace_name=command.workspace_name.strip(),
                occurred_at=now,
            )
            await uow.store.create_session(
                session_id=session_id,
                user_id=user_id,
                token_hash=self._token_manager.digest(session_token),
                csrf_token_hash=self._token_manager.digest(csrf_token),
                created_at=now,
                expires_at=now + self._session_ttl,
            )
            await uow.store.add_auth_event(
                event_id=uuid4(),
                event_type="registered",
                subject=email,
                user_id=user_id,
                request_id=command.request_id,
                occurred_at=now,
            )
            await uow.commit()

        return AuthResult(
            principal=PrincipalRecord(
                user_id=user_id,
                display_name=command.display_name.strip(),
                email=email,
                workspace_id=workspace_id,
                workspace_name=command.workspace_name.strip(),
                role="owner",
            ),
            session_token=session_token,
            csrf_token=csrf_token,
        )

    async def get_current_principal(self, session_token: str | None) -> PrincipalRecord:
        """根据服务端会话恢复当前用户及其工作空间上下文。"""
        if not session_token:
            raise AuthenticationRequiredError

        async with self._uow_factory() as uow:
            principal = await uow.store.get_principal_by_session(
                token_hash=self._token_manager.digest(session_token),
                now=datetime.now(UTC),
            )

        if principal is None:
            raise AuthenticationRequiredError
        return principal

    async def get_active_workspace_principal(self, *, user_id: UUID, workspace_id: UUID) -> PrincipalRecord | None:
        """后台执行重新验证活动成员，不依赖浏览器会话或复用提交时的授权快照。"""
        async with self._uow_factory() as uow:
            return await uow.store.get_principal_by_user_id(user_id, workspace_id=workspace_id)

    async def workspace_member_names(self, principal: PrincipalRecord) -> list[tuple[UUID, str]]:
        """任务选择器只获取活动成员的 ID 与显示名。"""
        async with self._uow_factory() as uow:
            return await uow.store.workspace_member_names(principal.workspace_id)

    async def authorize_write(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None,
    ) -> PrincipalRecord:
        """校验浏览器会话和双提交 CSRF 令牌，返回业务写操作身份。"""
        if not session_token:
            raise AuthenticationRequiredError
        if not csrf_token:
            raise CsrfRejectedError

        async with self._uow_factory() as uow:
            principal = await uow.store.get_principal_by_session_and_csrf(
                token_hash=self._token_manager.digest(session_token),
                csrf_token_hash=self._token_manager.digest(csrf_token),
                now=datetime.now(UTC),
            )
        if principal is None:
            raise CsrfRejectedError
        return principal

    async def login(self, command: LoginCommand) -> AuthResult:
        """校验凭据并签发新的可撤销浏览器会话。"""
        email = command.email.strip().lower()
        now = datetime.now(UTC)
        session_token = self._token_manager.issue()
        csrf_token = self._token_manager.issue()
        principal: PrincipalRecord | None = None
        invalid_credentials = False

        async with self._uow_factory() as uow:
            failure_count = await uow.store.count_recent_login_failures(
                subject=email,
                since=now - self._login_failure_window,
            )
            if failure_count >= self._login_max_failures:
                raise LoginRateLimitedError

            user = await uow.store.get_user_by_email(email)
            password_hash = user.password_hash if user is not None else None
            password_valid = not self._password_verification_enabled or self._password_hasher.verify(
                command.password, password_hash
            )
            if user is None or user.status != "active" or not password_valid:
                invalid_credentials = True
                await uow.store.add_auth_event(
                    event_id=uuid4(),
                    event_type="login_failed",
                    subject=email,
                    user_id=user.id if user is not None else None,
                    request_id=command.request_id,
                    occurred_at=now,
                )
            else:
                principal = await uow.store.get_principal_by_user_id(user.id)
                if principal is None:
                    invalid_credentials = True
                else:
                    await uow.store.create_session(
                        session_id=uuid4(),
                        user_id=user.id,
                        token_hash=self._token_manager.digest(session_token),
                        csrf_token_hash=self._token_manager.digest(csrf_token),
                        created_at=now,
                        expires_at=now + self._session_ttl,
                    )
                    await uow.store.add_auth_event(
                        event_id=uuid4(),
                        event_type="login_succeeded",
                        subject=email,
                        user_id=user.id,
                        request_id=command.request_id,
                        occurred_at=now,
                    )
            await uow.commit()

        if invalid_credentials or principal is None:
            raise InvalidCredentialsError
        return AuthResult(
            principal=principal,
            session_token=session_token,
            csrf_token=csrf_token,
        )

    async def logout(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None,
        request_id: str,
    ) -> None:
        """验证与会话绑定的 CSRF 令牌并撤销当前会话。"""
        if not session_token:
            raise AuthenticationRequiredError
        if not csrf_token:
            raise CsrfRejectedError

        now = datetime.now(UTC)
        async with self._uow_factory() as uow:
            revoked_session = await uow.store.revoke_session(
                token_hash=self._token_manager.digest(session_token),
                csrf_token_hash=self._token_manager.digest(csrf_token),
                revoked_at=now,
            )
            if revoked_session is None:
                raise CsrfRejectedError
            await uow.store.add_auth_event(
                event_id=uuid4(),
                event_type="logout",
                subject=revoked_session.email,
                user_id=revoked_session.user_id,
                request_id=request_id,
                occurred_at=now,
            )
            await uow.commit()
