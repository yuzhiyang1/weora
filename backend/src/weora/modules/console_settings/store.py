"""追加配置版本并以 CAS 更新当前指针，旧运行继续引用旧版本。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from weora.modules.console_settings.domain import SettingsError, SettingValue, SettingVersion
from weora.shared.infrastructure.database import Base, SessionFactory


class SettingVersionModel(Base):
    """不可变的设置版本；密钥使用独立加密字段，绝不放入公开 JSON。"""

    __tablename__ = "console_setting_versions"
    __table_args__ = (UniqueConstraint("workspace_id", "kind", "revision", name="uq_console_setting_revision"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int]
    values: Mapped[dict[str, SettingValue]] = mapped_column(JSON())
    encrypted_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SettingHeadModel(Base):
    """每个团队、每类设置只有一个当前版本。"""

    __tablename__ = "console_setting_heads"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    revision: Mapped[int]
    version_id: Mapped[UUID] = mapped_column(ForeignKey("console_setting_versions.id"))


class SettingsStore:
    """持久化仅访问本模块表，活动成员授权交给应用服务。"""

    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    async def read(self, workspace_id: UUID, kind: str, version_id: UUID | None = None) -> SettingVersion | None:
        """显式版本也必须属于当前团队与配置种类。"""
        async with self.sessions() as session:
            statement = select(SettingVersionModel).where(
                SettingVersionModel.workspace_id == workspace_id, SettingVersionModel.kind == kind
            )
            if version_id is None:
                statement = statement.join(SettingHeadModel, SettingHeadModel.version_id == SettingVersionModel.id)
            else:
                statement = statement.where(SettingVersionModel.id == version_id)
            row = await session.scalar(statement)
            return SettingVersion(row.id, row.revision, dict(row.values), row.encrypted_key) if row else None

    async def save(
        self,
        workspace_id: UUID,
        user_id: UUID,
        kind: str,
        revision: int,
        values: dict[str, SettingValue],
        encrypted_key: str | None = None,
    ) -> SettingVersion:
        """原子追加并换头；并发编辑失败时版本与秘密写入一起回滚。"""
        identifier = uuid4()
        try:
            async with self.sessions() as session, session.begin():
                session.add(
                    SettingVersionModel(
                        id=identifier,
                        workspace_id=workspace_id,
                        kind=kind,
                        revision=revision + 1,
                        values=values,
                        encrypted_key=encrypted_key,
                        created_by=user_id,
                        created_at=datetime.now(UTC),
                    )
                )
                await session.flush()
                if revision == 0:
                    session.add(
                        SettingHeadModel(
                            workspace_id=workspace_id,
                            kind=kind,
                            revision=1,
                            version_id=identifier,
                        )
                    )
                    await session.flush()
                else:
                    result = await session.execute(
                        update(SettingHeadModel)
                        .where(
                            SettingHeadModel.workspace_id == workspace_id,
                            SettingHeadModel.kind == kind,
                            SettingHeadModel.revision == revision,
                        )
                        .values(revision=revision + 1, version_id=identifier)
                        .returning(SettingHeadModel.version_id)
                    )
                    if result.scalar_one_or_none() != identifier:
                        raise SettingsError("SETTINGS_CONFLICT", "设置已被更新，请刷新后重试", 409)
        except IntegrityError:
            raise SettingsError("SETTINGS_CONFLICT", "设置已被更新，请刷新后重试", 409) from None
        return SettingVersion(identifier, revision + 1, values, encrypted_key)
