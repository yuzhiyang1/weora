"""测试账号独立加密保存，模型只接收凭据引用，不读取账号和密码。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from weora.modules.computers.openbot import ComputerError
from weora.modules.context.domain import SecretCipher
from weora.modules.runs.application.ports import RunActor
from weora.shared.infrastructure.database import Base, SessionFactory


class ComputerCredentialModel(Base):
    __tablename__ = "computer_credentials"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    origin: Mapped[str] = mapped_column(String(2048))
    scope_id: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(80))
    ciphertext: Mapped[str] = mapped_column(Text())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ComputerCredentials:
    def __init__(self, sessions: SessionFactory, cipher: SecretCipher) -> None:
        self.sessions, self.cipher = sessions, cipher

    async def create(
        self, actor: RunActor, *, origin: str, scope_id: str, label: str, username: str, password: str
    ) -> UUID:
        """配置只对当前用户及指定话题生效，默认一天后失效；从不回显密文或明文。"""
        identity = uuid4()
        async with self.sessions.begin() as session:
            session.add(
                ComputerCredentialModel(
                    id=identity,
                    workspace_id=actor.workspace_id,
                    owner_id=actor.user_id,
                    origin=origin,
                    scope_id=scope_id,
                    label=label,
                    ciphertext=self.cipher.encrypt(json.dumps({"username": username, "password": password})),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
        return identity

    async def available(self, actor: RunActor, scope_id: str) -> list[dict[str, object]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(ComputerCredentialModel).where(
                        ComputerCredentialModel.workspace_id == actor.workspace_id,
                        ComputerCredentialModel.owner_id == actor.user_id,
                        ComputerCredentialModel.scope_id == scope_id,
                        ComputerCredentialModel.expires_at > datetime.now(UTC),
                    )
                )
            ).all()
            return [
                {"id": str(row.id), "origin": row.origin, "label": row.label, "expires_at": row.expires_at.isoformat()}
                for row in rows
            ]

    async def value(self, actor: RunActor, scope_id: str, identity: UUID, origin: str, field: str) -> str:
        """填充前再次校验用户、话题、来源及保留期，禁止网页或模型指定解密目标。"""
        async with self.sessions() as session:
            row = await session.scalar(
                select(ComputerCredentialModel).where(
                    ComputerCredentialModel.id == identity,
                    ComputerCredentialModel.workspace_id == actor.workspace_id,
                    ComputerCredentialModel.owner_id == actor.user_id,
                    ComputerCredentialModel.scope_id == scope_id,
                    ComputerCredentialModel.origin == origin,
                    ComputerCredentialModel.expires_at > datetime.now(UTC),
                )
            )
            if row is None or field not in {"username", "password"}:
                raise ComputerError(403, "COMPUTER_CREDENTIAL_DENIED", "测试凭据已失效或不适用于当前页面")
            return str(json.loads(self.cipher.decrypt(row.ciphertext))[field])

    async def revoke(self, actor: RunActor, identity: UUID) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                delete(ComputerCredentialModel).where(
                    ComputerCredentialModel.id == identity,
                    ComputerCredentialModel.workspace_id == actor.workspace_id,
                    ComputerCredentialModel.owner_id == actor.user_id,
                )
            )
