"""上下文独立持久化；业务键加行锁，保证预算和 Inbox/Outbox 并发更新。"""

import builtins
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, String, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from weora.modules.context.domain import ContextError, Json
from weora.shared.infrastructure.database import Base, SessionFactory


class ContextDocument(Base):
    """一个受管资源一行；类型与工作空间共同组成不可跨域的业务键。"""

    __tablename__ = "context_documents"
    __table_args__ = (UniqueConstraint("workspace_id", "kind", "resource_key", name="uq_context_document_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(String(32))
    resource_key: Mapped[str] = mapped_column(String(256))
    payload: Mapped[Json] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SqlContextStore:
    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    async def get(self, workspace_id: UUID, kind: str, key: str) -> Json | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(ContextDocument).where(
                    ContextDocument.workspace_id == workspace_id,
                    ContextDocument.kind == kind,
                    ContextDocument.resource_key == key,
                )
            )
            return deepcopy(row.payload) if row else None

    async def list(self, workspace_id: UUID, kind: str) -> list[Json]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(ContextDocument)
                .where(
                    ContextDocument.workspace_id == workspace_id,
                    ContextDocument.kind == kind,
                )
                .order_by(ContextDocument.resource_key)
            )
            return [deepcopy(row.payload) for row in rows]

    async def workspace_ids(self) -> builtins.list[UUID]:
        """只枚举本模块已管理工作空间，供后台补偿与正文清理，不读取身份模块 ORM。"""
        async with self.sessions() as session:
            rows = await session.scalars(select(ContextDocument.workspace_id).distinct())
            return list(rows)

    async def change(self, workspace_id: UUID, kind: str, key: str, update: Callable[[Json | None], Json]) -> Json:
        """只重试首次插入的唯一键竞争；回调必须纯计算，不能包含外部副作用。"""
        for attempt in range(3):
            try:
                async with self.sessions() as session, session.begin():
                    row = await session.scalar(
                        select(ContextDocument)
                        .where(
                            ContextDocument.workspace_id == workspace_id,
                            ContextDocument.kind == kind,
                            ContextDocument.resource_key == key,
                        )
                        .with_for_update()
                    )
                    payload = update(deepcopy(row.payload) if row else None)
                    if row is None:
                        row = ContextDocument(
                            workspace_id=workspace_id,
                            kind=kind,
                            resource_key=key,
                            payload=payload,
                            updated_at=datetime.now(UTC),
                        )
                        session.add(row)
                    else:
                        row.payload = payload
                        row.updated_at = datetime.now(UTC)
                    await session.flush()
                    return deepcopy(payload)
            except IntegrityError:
                if attempt == 2:
                    raise ContextError("CONCURRENT_UPDATE", "配置发生并发变更，请重试") from None
        raise AssertionError("unreachable")
