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
from weora.modules.identity.application.invitations import (
    InviteSigner,
    InviteUnavailable,
    NoActiveWorkspace,
    WorkspaceAccessDenied,
    WorkspaceInvite,
    WorkspaceMember,
    WorkspaceRecord,
)
from weora.modules.identity.application.ports import (
    IdentityStore,
    IdentityUnitOfWorkFactory,
    PasswordHasher,
    PrincipalRecord,
    TokenManager,
)


@dataclass(frozen=True, slots=True)
class RegisterCommand:
    """自主注册创建空间；受邀注册在同一事务中加入指定空间。"""

    display_name: str
    email: str
    password: str
    workspace_name: str
    request_id: str
    invite_token: str | None = None


@dataclass(frozen=True, slots=True)
class LoginCommand:
    """使用邮箱和密码创建新会话。"""

    email: str
    password: str
    request_id: str
    invite_token: str | None = None


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
        invite_signer: InviteSigner | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._password_hasher = password_hasher
        self._token_manager = token_manager
        self._password_verification_enabled = password_verification_enabled
        self._registration_enabled = registration_enabled
        self._session_ttl = session_ttl
        self._login_max_failures = login_max_failures
        self._login_failure_window = login_failure_window
        self._invite_signer = invite_signer or InviteSigner(None)

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
        """受邀注册可在关闭公开注册时使用，兑换失败则不保留新账户。"""
        if command.invite_token is None and not self._registration_enabled:
            raise RegistrationDisabledError

        email = command.email.strip().lower()
        user_id = uuid4()
        async with self._uow_factory() as uow:
            invite = None
            if command.invite_token is not None:
                invite = await self._locked_invite(uow.store, command.invite_token)
                self._require_pending(invite)
            if await uow.store.get_user_by_email(email) is not None:
                raise EmailUnavailableError
            now = datetime.now(UTC)
            display_name = command.display_name.strip()
            password_hash = self._password_hasher.hash(command.password)
            if invite is None:
                workspace_id = uuid4()
                await uow.store.create_registration(
                    user_id=user_id,
                    display_name=display_name,
                    email=email,
                    password_hash=password_hash,
                    occurred_at=now,
                    workspace_id=workspace_id,
                    workspace_name=command.workspace_name.strip(),
                )
                principal = PrincipalRecord(
                    user_id,
                    display_name,
                    email,
                    workspace_id,
                    command.workspace_name.strip(),
                    "owner",
                )
            else:
                await uow.store.create_user(
                    user_id=user_id,
                    display_name=display_name,
                    email=email,
                    password_hash=password_hash,
                    occurred_at=now,
                )
                principal = await self._join_invite(uow.store, invite, user_id, command.request_id)
            result = await self._create_session(uow.store, principal, now)
            await self._audit(uow.store, "registered", email, user_id, command.request_id, now)
            await uow.commit()
            return result

    async def _create_session(self, store: IdentityStore, principal: PrincipalRecord, now: datetime) -> AuthResult:
        session_token, csrf_token = self._token_manager.issue(), self._token_manager.issue()
        await store.create_session(
            session_id=uuid4(),
            user_id=principal.user_id,
            workspace_id=principal.workspace_id,
            token_hash=self._token_manager.digest(session_token),
            csrf_token_hash=self._token_manager.digest(csrf_token),
            created_at=now,
            expires_at=now + self._session_ttl,
        )
        return AuthResult(principal, session_token, csrf_token)

    @staticmethod
    async def _audit(
        store: IdentityStore,
        event: str,
        subject: str,
        user_id: UUID,
        request_id: str,
        now: datetime,
    ) -> None:
        """只记录业务标识和操作者，原始链接不进入审计记录。"""
        await store.add_auth_event(
            event_id=uuid4(),
            event_type=event,
            subject=subject,
            user_id=user_id,
            request_id=request_id,
            occurred_at=now,
        )

    @staticmethod
    async def _require_manager(store: IdentityStore, actor: PrincipalRecord, workspace_id: UUID) -> None:
        if actor.workspace_id != workspace_id:
            raise WorkspaceAccessDenied
        current = await store.get_principal_by_user_id(actor.user_id, workspace_id=workspace_id)
        if current is None or current.role not in {"owner", "admin"}:
            raise WorkspaceAccessDenied

    async def list_workspaces(self, actor: PrincipalRecord) -> list[WorkspaceRecord]:
        async with self._uow_factory() as uow:
            return await uow.store.list_workspaces(actor.user_id)

    async def list_members(self, actor: PrincipalRecord, workspace_id: UUID) -> list[WorkspaceMember]:
        if actor.workspace_id != workspace_id:
            raise WorkspaceAccessDenied
        async with self._uow_factory() as uow:
            return await uow.store.list_members(workspace_id)

    async def switch_workspace(
        self,
        actor: PrincipalRecord,
        workspace_id: UUID,
        session_token: str,
    ) -> PrincipalRecord:
        async with self._uow_factory() as uow:
            if not await uow.store.lock_workspace(workspace_id):
                raise WorkspaceAccessDenied
            principal = await uow.store.get_principal_by_user_id(actor.user_id, workspace_id=workspace_id)
            if principal is None:
                raise WorkspaceAccessDenied
            await self._activate(uow.store, principal, session_token)
            await uow.commit()
            return principal

    async def _activate(self, store: IdentityStore, principal: PrincipalRecord, session_token: str) -> None:
        if not await store.activate_workspace(
            self._token_manager.digest(session_token),
            principal.user_id,
            principal.workspace_id,
            datetime.now(UTC),
        ):
            raise AuthenticationRequiredError

    async def list_invites(self, actor: PrincipalRecord, workspace_id: UUID) -> list[WorkspaceInvite]:
        async with self._uow_factory() as uow:
            await self._require_manager(uow.store, actor, workspace_id)
            return await uow.store.list_invites(workspace_id)

    async def create_invite(
        self,
        actor: PrincipalRecord,
        workspace_id: UUID,
        hours: int,
        request_id: str,
    ) -> tuple[WorkspaceInvite, str]:
        async with self._uow_factory() as uow:
            if actor.workspace_id != workspace_id or not await uow.store.lock_workspace(workspace_id):
                raise WorkspaceAccessDenied
            await self._require_manager(uow.store, actor, workspace_id)
            invite_id, now = uuid4(), datetime.now(UTC)
            token = self._invite_signer.sign(invite_id)
            invite = await uow.store.create_invite(
                invite_id,
                workspace_id,
                actor.user_id,
                now,
                now + timedelta(hours=hours),
            )
            await self._audit(
                uow.store, "invite.created", f"{workspace_id}:{invite_id}", actor.user_id, request_id, now
            )
            await uow.commit()
            return invite, token

    async def copy_invite(self, actor: PrincipalRecord, workspace_id: UUID, invite_id: UUID) -> str:
        async with self._uow_factory() as uow:
            await self._require_manager(uow.store, actor, workspace_id)
            self._require_pending(await uow.store.get_invite(invite_id, workspace_id=workspace_id))
            return self._invite_signer.sign(invite_id)

    async def revoke_invite(
        self,
        actor: PrincipalRecord,
        workspace_id: UUID,
        invite_id: UUID,
        request_id: str,
    ) -> None:
        async with self._uow_factory() as uow:
            if actor.workspace_id != workspace_id or not await uow.store.lock_workspace(workspace_id):
                raise WorkspaceAccessDenied
            await self._require_manager(uow.store, actor, workspace_id)
            invite = await uow.store.get_invite(invite_id, workspace_id=workspace_id)
            if invite is not None and invite.revoked_at is not None:
                return
            self._require_pending(invite)
            now = datetime.now(UTC)
            await uow.store.revoke_invite(invite_id, now)
            await self._audit(
                uow.store, "invite.revoked", f"{workspace_id}:{invite_id}", actor.user_id, request_id, now
            )
            await uow.commit()

    async def remove_member(
        self,
        actor: PrincipalRecord,
        workspace_id: UUID,
        user_id: UUID,
        request_id: str,
    ) -> None:
        async with self._uow_factory() as uow:
            if actor.workspace_id != workspace_id or not await uow.store.lock_workspace(workspace_id):
                raise WorkspaceAccessDenied
            await self._require_manager(uow.store, actor, workspace_id)
            now = datetime.now(UTC)
            if not await uow.store.lock_active_user(user_id):
                raise WorkspaceAccessDenied
            if not await uow.store.remove_member(workspace_id, user_id, now):
                raise WorkspaceAccessDenied
            await self._audit(uow.store, "member.removed", f"{workspace_id}:{user_id}", actor.user_id, request_id, now)
            await uow.commit()

    @staticmethod
    def _require_pending(invite: WorkspaceInvite | None) -> WorkspaceInvite:
        if invite is None or invite.status(datetime.now(UTC)) != "pending":
            raise InviteUnavailable
        return invite

    async def preview_invite(self, token: str) -> WorkspaceInvite:
        invite_id = self._invite_signer.verify(token)
        async with self._uow_factory() as uow:
            return self._require_pending(await uow.store.get_invite(invite_id))

    async def _locked_invite(self, store: IdentityStore, token: str) -> WorkspaceInvite:
        invite_id = self._invite_signer.verify(token)
        invite = await store.get_invite(invite_id)
        if invite is None or not await store.lock_workspace(invite.workspace_id):
            raise InviteUnavailable
        # 锁后重读，才能看到在等待期间提交的撤销或兑换。
        current = await store.get_invite(invite_id, workspace_id=invite.workspace_id)
        if current is None:
            raise InviteUnavailable
        return current

    async def _join_invite(
        self,
        store: IdentityStore,
        invite: WorkspaceInvite,
        user_id: UUID,
        request_id: str,
    ) -> PrincipalRecord:
        if not await store.lock_active_user(user_id):
            raise AuthenticationRequiredError
        principal = await store.get_principal_by_user_id(user_id, workspace_id=invite.workspace_id)
        # 同一用户重试可恢复会话，已是成员也不消耗有效链接。
        if principal is not None and invite.used_by == user_id:
            return principal
        self._require_pending(invite)
        if principal is not None:
            return principal
        now = datetime.now(UTC)
        await store.join_workspace(invite.workspace_id, user_id, now)
        await store.consume_invite(invite.id, user_id, now)
        await self._audit(store, "invite.accepted", f"{invite.workspace_id}:{invite.id}", user_id, request_id, now)
        joined = await store.get_principal_by_user_id(user_id, workspace_id=invite.workspace_id)
        assert joined is not None
        return joined

    async def accept_invite(
        self,
        actor: PrincipalRecord,
        token: str,
        session_token: str,
        request_id: str,
    ) -> PrincipalRecord:
        async with self._uow_factory() as uow:
            invite = await self._locked_invite(uow.store, token)
            principal = await self._join_invite(uow.store, invite, actor.user_id, request_id)
            await self._activate(uow.store, principal, session_token)
            await uow.commit()
            return principal

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
        principal: PrincipalRecord | None = None
        result: AuthResult | None = None
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
            allow_bypass = not self._password_verification_enabled and command.invite_token is None
            password_valid = allow_bypass or self._password_hasher.verify(command.password, password_hash)
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
                assert user is not None
                principal = await uow.store.get_principal_by_user_id(user.id)
                if principal is None and command.invite_token is not None:
                    invite = await self._locked_invite(uow.store, command.invite_token)
                    principal = await self._join_invite(uow.store, invite, user.id, command.request_id)
                elif principal is not None:
                    # 等待并发撤权提交后重查，避免签出一个后续可能复活的旧会话。
                    if await uow.store.lock_active_user(user.id):
                        principal = await uow.store.get_principal_by_user_id(user.id)
                    else:
                        principal = None
                if principal is not None:
                    result = await self._create_session(uow.store, principal, now)
                    await self._audit(uow.store, "login_succeeded", email, user.id, command.request_id, now)
            await uow.commit()

        if invalid_credentials:
            raise InvalidCredentialsError
        if result is None:
            raise NoActiveWorkspace
        return result

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
