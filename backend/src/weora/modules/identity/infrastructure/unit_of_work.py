"""Identity SQLAlchemy Unit of Work。"""

from datetime import datetime
from types import TracebackType
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.identity.application.errors import EmailUnavailableError
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
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from weora.shared.infrastructure.database import SessionFactory


class SqlAlchemyIdentityStore:
    """把 Identity 端口映射到 SQLAlchemy ORM。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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

    async def create_session(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        token_hash: str,
        csrf_token_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        self._session.add(
            AuthSessionModel(
                id=session_id,
                user_id=user_id,
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
