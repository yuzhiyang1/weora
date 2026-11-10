"""Identity SQLAlchemy Unit of Work。"""

from datetime import datetime
from types import TracebackType
from uuid import UUID, uuid4

from sqlalchemy import Select, func, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from weora.modules.identity.application.errors import EmailUnavailableError
from weora.modules.identity.application.invitations import WorkspaceInvite, WorkspaceMember, WorkspaceRecord
from weora.modules.identity.application.ports import (
    IdentityStore,
    PrincipalRecord,
    RevokedSessionRecord,
    UserRecord,
)
from weora.modules.identity.infrastructure.models import (
    AuthEventModel,
    AuthSessionModel,
    UserModel,
    WorkspaceInviteModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from weora.shared.infrastructure.database import SessionFactory


class SqlAlchemyIdentityStore:
    """把 Identity 端口映射到 SQLAlchemy ORM。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def workspace_member_names(self, workspace_id: UUID) -> list[tuple[UUID, str]]:
        rows = await self._session.execute(
            select(UserModel.id, UserModel.display_name)
            .join(
                WorkspaceMembershipModel,
                WorkspaceMembershipModel.user_id == UserModel.id,
            )
            .where(
                WorkspaceMembershipModel.workspace_id == workspace_id,
                WorkspaceMembershipModel.status == "active",
                UserModel.status == "active",
            )
            .order_by(UserModel.display_name, UserModel.id)
        )
        return [(user_id, name) for user_id, name in rows]

    async def list_workspaces(self, user_id: UUID) -> list[WorkspaceRecord]:
        rows = await self._session.execute(
            select(WorkspaceModel.id, WorkspaceModel.name, WorkspaceMembershipModel.role)
            .join(WorkspaceMembershipModel, WorkspaceMembershipModel.workspace_id == WorkspaceModel.id)
            .where(
                WorkspaceMembershipModel.user_id == user_id,
                WorkspaceMembershipModel.status == "active",
                WorkspaceModel.status == "active",
            )
            .order_by(WorkspaceMembershipModel.created_at, WorkspaceModel.id)
        )
        return [WorkspaceRecord(*row) for row in rows]

    async def list_members(self, workspace_id: UUID) -> list[WorkspaceMember]:
        rows = await self._session.execute(
            select(UserModel.id, UserModel.display_name, UserModel.email_normalized, WorkspaceMembershipModel.role)
            .join(WorkspaceMembershipModel, WorkspaceMembershipModel.user_id == UserModel.id)
            .where(
                WorkspaceMembershipModel.workspace_id == workspace_id,
                WorkspaceMembershipModel.status == "active",
                UserModel.status == "active",
            )
            .order_by(WorkspaceMembershipModel.created_at, UserModel.id)
        )
        return [WorkspaceMember(*row) for row in rows]

    async def lock_workspace(self, workspace_id: UUID) -> bool:
        """同一空间的兑换、撤销与移除按事务串行；SQLite 通过首条写入取得锁。"""
        if self._session.get_bind().dialect.name == "sqlite":
            statement = (
                update(WorkspaceModel)
                .where(WorkspaceModel.id == workspace_id, WorkspaceModel.status == "active")
                .values(updated_at=WorkspaceModel.updated_at)
                .returning(WorkspaceModel.id)
            )
            return await self._session.scalar(statement) is not None
        return (
            await self._session.scalar(
                select(WorkspaceModel.id)
                .where(WorkspaceModel.id == workspace_id, WorkspaceModel.status == "active")
                # NO KEY UPDATE 仍串行化邀请变更，并允许其他用例插入外键引用。
                .with_for_update(key_share=True)
            )
            is not None
        )

    async def lock_active_user(self, user_id: UUID) -> bool:
        """邀请变更先锁空间再锁用户，登录只获取用户锁。"""
        if self._session.get_bind().dialect.name == "sqlite":
            statement = (
                update(UserModel)
                .where(UserModel.id == user_id, UserModel.status == "active")
                .values(updated_at=UserModel.updated_at)
                .returning(UserModel.id)
            )
            return await self._session.scalar(statement) is not None
        locked_user = (
            select(UserModel.id)
            .where(UserModel.id == user_id, UserModel.status == "active")
            .with_for_update(key_share=True)
        )
        return await self._session.scalar(locked_user) is not None

    async def create_invite(
        self,
        invite_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        now: datetime,
        expires_at: datetime,
    ) -> WorkspaceInvite:
        self._session.add(
            WorkspaceInviteModel(
                id=invite_id,
                workspace_id=workspace_id,
                created_by=user_id,
                created_at=now,
                expires_at=expires_at,
            )
        )
        await self._session.flush()
        invite = await self.get_invite(invite_id, workspace_id=workspace_id)
        assert invite is not None
        return invite

    @staticmethod
    def _invite_query() -> Select[tuple[WorkspaceInviteModel, str, str, str]]:
        creator = aliased(UserModel)
        consumer = aliased(UserModel)
        return (
            select(WorkspaceInviteModel, WorkspaceModel.name, creator.display_name, consumer.display_name)
            .join(WorkspaceModel, WorkspaceModel.id == WorkspaceInviteModel.workspace_id)
            .join(creator, creator.id == WorkspaceInviteModel.created_by)
            .outerjoin(consumer, consumer.id == WorkspaceInviteModel.used_by)
            .where(WorkspaceModel.status == "active")
        )

    @staticmethod
    def _invite_record(
        model: WorkspaceInviteModel,
        workspace: str,
        creator: str,
        consumer: str | None,
    ) -> WorkspaceInvite:
        return WorkspaceInvite(
            model.id,
            model.workspace_id,
            workspace,
            creator,
            model.created_at,
            model.expires_at,
            model.used_by,
            consumer,
            model.revoked_at,
        )

    async def list_invites(self, workspace_id: UUID) -> list[WorkspaceInvite]:
        rows = await self._session.execute(
            self._invite_query()
            .where(WorkspaceInviteModel.workspace_id == workspace_id)
            .order_by(WorkspaceInviteModel.created_at.desc(), WorkspaceInviteModel.id.desc())
        )
        return [self._invite_record(*row) for row in rows]

    async def get_invite(self, invite_id: UUID, *, workspace_id: UUID | None = None) -> WorkspaceInvite | None:
        statement = self._invite_query().where(WorkspaceInviteModel.id == invite_id)
        if workspace_id is not None:
            statement = statement.where(WorkspaceInviteModel.workspace_id == workspace_id)
        row = (await self._session.execute(statement)).one_or_none()
        return self._invite_record(*row) if row is not None else None

    async def consume_invite(self, invite_id: UUID, user_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(WorkspaceInviteModel)
            .where(WorkspaceInviteModel.id == invite_id)
            .values(used_by=user_id, used_at=now)
        )

    async def revoke_invite(self, invite_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(WorkspaceInviteModel).where(WorkspaceInviteModel.id == invite_id).values(revoked_at=now)
        )

    async def remove_member(self, workspace_id: UUID, user_id: UUID, now: datetime) -> bool:
        removed = await self._session.scalar(
            update(WorkspaceMembershipModel)
            .where(
                WorkspaceMembershipModel.workspace_id == workspace_id,
                WorkspaceMembershipModel.user_id == user_id,
                WorkspaceMembershipModel.status == "active",
                WorkspaceMembershipModel.role == "member",
            )
            .values(status="inactive", updated_at=now)
            .returning(WorkspaceMembershipModel.id)
        )
        if removed is None:
            return False
        sessions = update(AuthSessionModel).where(
            AuthSessionModel.user_id == user_id, AuthSessionModel.revoked_at.is_(None)
        )
        if not await self.list_workspaces(user_id):
            # 最后一个成员关系被移除时注销旧会话，日后重新受邀也不能复活旧 Cookie。
            await self._session.execute(sessions.values(revoked_at=now))
        else:
            await self._session.execute(
                sessions.where(AuthSessionModel.workspace_id == workspace_id).values(workspace_id=None)
            )
        return True

    async def join_workspace(self, workspace_id: UUID, user_id: UUID, now: datetime) -> None:
        previous = await self._session.scalar(
            select(WorkspaceMembershipModel).where(
                WorkspaceMembershipModel.workspace_id == workspace_id,
                WorkspaceMembershipModel.user_id == user_id,
            )
        )
        if previous is None:
            self._session.add(
                WorkspaceMembershipModel(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    user_id=user_id,
                    role="member",
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            # 移除后可凭新邀请重新加入；邀请始终只授予普通成员。
            previous.status, previous.role, previous.updated_at = "active", "member", now
        await self._session.flush()

    async def activate_workspace(self, token_hash: str, user_id: UUID, workspace_id: UUID, now: datetime) -> bool:
        return (
            await self._session.scalar(
                update(AuthSessionModel)
                .where(
                    AuthSessionModel.token_hash == token_hash,
                    AuthSessionModel.user_id == user_id,
                    AuthSessionModel.revoked_at.is_(None),
                    AuthSessionModel.expires_at > now,
                )
                .values(workspace_id=workspace_id)
                .returning(AuthSessionModel.id)
            )
            is not None
        )

    async def update_profile(
        self, user_id: UUID, name: str, avatar: str | None, update_avatar: bool, now: datetime
    ) -> None:
        """只修改当前用户资料；邮箱和认证状态不在此用例开放。"""
        user = await self._session.get(UserModel, user_id)
        if user is None or user.status != "active":
            raise ValueError("用户已失效")
        user.display_name, user.updated_at = name, now
        if update_avatar:
            user.avatar_data_url = avatar
        await self._session.flush()

    async def get_avatar(self, user_id: UUID) -> str | None:
        return await self._session.scalar(
            select(UserModel.avatar_data_url).where(UserModel.id == user_id, UserModel.status == "active")
        )

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        model = await self._session.scalar(select(UserModel).where(UserModel.email_normalized == email))
        if model is None:
            return None
        return UserRecord(
            id=model.id,
            display_name=model.display_name,
            email=model.email_normalized,
            password_hash=model.password_hash,
            status=model.status,
        )

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
    ) -> None:
        # 未声明 ORM relationship 时，不依赖 Unit of Work 猜测跨表插入顺序。
        # 逐层 flush 只校验父记录，不提前提交；任意后续失败仍整体回滚。
        await self.create_user(
            user_id=user_id,
            display_name=display_name,
            email=email,
            password_hash=password_hash,
            occurred_at=occurred_at,
        )
        self._session.add(
            WorkspaceModel(
                id=workspace_id,
                name=workspace_name,
                status="active",
                created_by=user_id,
                created_at=occurred_at,
                updated_at=occurred_at,
            )
        )
        await self._session.flush()
        self._session.add(
            WorkspaceMembershipModel(
                id=uuid4(),
                workspace_id=workspace_id,
                user_id=user_id,
                role="owner",
                status="active",
                created_at=occurred_at,
                updated_at=occurred_at,
            )
        )

    async def create_user(
        self,
        *,
        user_id: UUID,
        display_name: str,
        email: str,
        password_hash: str,
        occurred_at: datetime,
    ) -> None:
        self._session.add(
            UserModel(
                id=user_id,
                display_name=display_name,
                email_normalized=email,
                password_hash=password_hash,
                status="active",
                created_at=occurred_at,
                updated_at=occurred_at,
            )
        )
        await self._session.flush()

    async def create_session(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        token_hash: str,
        csrf_token_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        self._session.add(
            AuthSessionModel(
                id=session_id,
                user_id=user_id,
                workspace_id=workspace_id,
                token_hash=token_hash,
                csrf_token_hash=csrf_token_hash,
                created_at=created_at,
                expires_at=expires_at,
                revoked_at=None,
            )
        )

    async def get_principal_by_session(
        self,
        *,
        token_hash: str,
        now: datetime,
    ) -> PrincipalRecord | None:
        statement = (
            select(UserModel, WorkspaceModel, WorkspaceMembershipModel)
            .join(AuthSessionModel, AuthSessionModel.user_id == UserModel.id)
            .join(WorkspaceMembershipModel, WorkspaceMembershipModel.user_id == UserModel.id)
            .join(WorkspaceModel, WorkspaceModel.id == WorkspaceMembershipModel.workspace_id)
            .where(
                AuthSessionModel.token_hash == token_hash,
                AuthSessionModel.revoked_at.is_(None),
                AuthSessionModel.expires_at > now,
                UserModel.status == "active",
                WorkspaceModel.status == "active",
                WorkspaceMembershipModel.status == "active",
            )
            .order_by(
                (AuthSessionModel.workspace_id == WorkspaceModel.id).desc().nulls_last(),
                WorkspaceMembershipModel.created_at,
                WorkspaceModel.id,
            )
            .limit(1)
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        user, workspace, membership = row
        return PrincipalRecord(
            user_id=user.id,
            display_name=user.display_name,
            email=user.email_normalized,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            role=membership.role,
            avatar_revision=user.updated_at.isoformat() if user.avatar_data_url else None,
        )

    async def get_principal_by_session_and_csrf(
        self,
        *,
        token_hash: str,
        csrf_token_hash: str,
        now: datetime,
    ) -> PrincipalRecord | None:
        """同时校验会话与其绑定的 CSRF 令牌，供业务写接口复用。"""
        statement = (
            select(UserModel, WorkspaceModel, WorkspaceMembershipModel)
            .join(AuthSessionModel, AuthSessionModel.user_id == UserModel.id)
            .join(WorkspaceMembershipModel, WorkspaceMembershipModel.user_id == UserModel.id)
            .join(WorkspaceModel, WorkspaceModel.id == WorkspaceMembershipModel.workspace_id)
            .where(
                AuthSessionModel.token_hash == token_hash,
                AuthSessionModel.csrf_token_hash == csrf_token_hash,
                AuthSessionModel.revoked_at.is_(None),
                AuthSessionModel.expires_at > now,
                UserModel.status == "active",
                WorkspaceModel.status == "active",
                WorkspaceMembershipModel.status == "active",
            )
            .order_by(
                (AuthSessionModel.workspace_id == WorkspaceModel.id).desc().nulls_last(),
                WorkspaceMembershipModel.created_at,
                WorkspaceModel.id,
            )
            .limit(1)
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        user, workspace, membership = row
        return PrincipalRecord(
            user_id=user.id,
            display_name=user.display_name,
            email=user.email_normalized,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            role=membership.role,
            avatar_revision=user.updated_at.isoformat() if user.avatar_data_url else None,
        )

    async def get_principal_by_user_id(
        self, user_id: UUID, *, workspace_id: UUID | None = None
    ) -> PrincipalRecord | None:
        statement = (
            select(UserModel, WorkspaceModel, WorkspaceMembershipModel)
            .join(WorkspaceMembershipModel, WorkspaceMembershipModel.user_id == UserModel.id)
            .join(WorkspaceModel, WorkspaceModel.id == WorkspaceMembershipModel.workspace_id)
            .where(
                UserModel.id == user_id,
                WorkspaceModel.id == workspace_id if workspace_id is not None else true(),
                UserModel.status == "active",
                WorkspaceModel.status == "active",
                WorkspaceMembershipModel.status == "active",
            )
            .order_by(WorkspaceMembershipModel.created_at)
            .limit(1)
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        user, workspace, membership = row
        return PrincipalRecord(
            user_id=user.id,
            display_name=user.display_name,
            email=user.email_normalized,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            role=membership.role,
            avatar_revision=user.updated_at.isoformat() if user.avatar_data_url else None,
        )

    async def revoke_session(
        self,
        *,
        token_hash: str,
        csrf_token_hash: str,
        revoked_at: datetime,
    ) -> RevokedSessionRecord | None:
        statement = (
            select(AuthSessionModel, UserModel)
            .join(UserModel, UserModel.id == AuthSessionModel.user_id)
            .where(
                AuthSessionModel.token_hash == token_hash,
                AuthSessionModel.csrf_token_hash == csrf_token_hash,
                AuthSessionModel.revoked_at.is_(None),
                AuthSessionModel.expires_at > revoked_at,
            )
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        session, user = row
        session.revoked_at = revoked_at
        return RevokedSessionRecord(user_id=user.id, email=user.email_normalized)

    async def add_auth_event(
        self,
        *,
        event_id: UUID,
        event_type: str,
        subject: str,
        user_id: UUID | None,
        request_id: str,
        occurred_at: datetime,
    ) -> None:
        self._session.add(
            AuthEventModel(
                id=event_id,
                event_type=event_type,
                subject=subject,
                user_id=user_id,
                request_id=request_id,
                occurred_at=occurred_at,
            )
        )

    async def count_recent_login_failures(self, *, subject: str, since: datetime) -> int:
        last_success = (
            select(func.max(AuthEventModel.occurred_at))
            .where(
                AuthEventModel.subject == subject,
                AuthEventModel.event_type == "login_succeeded",
            )
            .scalar_subquery()
        )
        statement = select(func.count(AuthEventModel.id)).where(
            AuthEventModel.subject == subject,
            AuthEventModel.event_type == "login_failed",
            AuthEventModel.occurred_at >= since,
            or_(last_success.is_(None), AuthEventModel.occurred_at > last_success),
        )
        return int((await self._session.scalar(statement)) or 0)


class SqlAlchemyIdentityUnitOfWork:
    """为一次 Identity 用例持有独立数据库事务。"""

    store: IdentityStore

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> "SqlAlchemyIdentityUnitOfWork":
        self._session = self._session_factory()
        self.store = SqlAlchemyIdentityStore(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc_type is not None:
            await self._session.rollback()
        await self._session.close()
        if isinstance(exc_value, IntegrityError) and _is_email_conflict(exc_value):
            raise EmailUnavailableError from exc_value

    async def commit(self) -> None:
        """提交事务，并把并发邮箱冲突转换为稳定应用错误。"""
        if self._session is None:
            raise RuntimeError("Unit of Work 尚未进入事务上下文")
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if _is_email_conflict(exc):
                raise EmailUnavailableError from exc
            raise


def _is_email_conflict(error: IntegrityError) -> bool:
    """只将邮箱唯一约束映射为注册冲突，不掩盖外键等基础设施错误。"""
    details = str(error.orig)
    return "users_email_normalized_key" in details or "UNIQUE constraint failed: users.email_normalized" in details
