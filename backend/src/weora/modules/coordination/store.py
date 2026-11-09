"""协调器自己的持久化边界；按 scope 加锁，不依赖进程内单例或 Redis 状态。"""

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import JSON, DateTime, String, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from weora.modules.context.domain import SecretCipher
from weora.modules.coordination.domain import (
    Config,
    CoordinationError,
    Effect,
    Json,
    State,
)
from weora.modules.coordination.engine import Engine, later
from weora.modules.coordination.records import records
from weora.shared.infrastructure.database import Base, SessionFactory


class CoordinationScope(Base):
    __tablename__ = "coordination_scopes"
    __table_args__ = (UniqueConstraint("workspace_id", "source_id", "route_key", name="uq_coordination_source_route"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(index=True)
    source_id: Mapped[UUID] = mapped_column(index=True)
    route_key: Mapped[str] = mapped_column(String(256))
    payload: Mapped[Json] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SqlCoordinationStore:
    def __init__(self, sessions: SessionFactory, cipher: SecretCipher) -> None:
        self.sessions, self.cipher = sessions, cipher
        self.codec = TypeAdapter(State)

    async def scopes(self, workspace_id: UUID | None = None) -> list[tuple[UUID, str]]:
        async with self.sessions() as session:
            query = select(CoordinationScope.workspace_id, CoordinationScope.id)
            if workspace_id:
                query = query.where(CoordinationScope.workspace_id == workspace_id)
            return [(row[0], row[1]) for row in (await session.execute(query)).all()]

    async def transact[T](
        self, workspace_id: UUID, scope_id: str, action: Callable[[Engine, State], T], *, create: Config | None = None
    ) -> T:
        """串行修改指定范围的协调状态；回调异常整体回滚，首次创建竞争映射为版本冲突。"""
        try:
            return await self._transact(workspace_id, scope_id, action, create=create)
        except IntegrityError:
            raise CoordinationError("CONFIG_REVISION_CONFLICT") from None

    async def _transact[T](
        self, workspace_id: UUID, scope_id: str, action: Callable[[Engine, State], T], *, create: Config | None = None
    ) -> T:
        """同 scope 的接单、输入、效果队列和授权修订串行提交；IO 必须在回调外。"""
        async with self.sessions() as session, session.begin():
            row = await session.scalar(
                select(CoordinationScope)
                .where(
                    CoordinationScope.workspace_id == workspace_id,
                    CoordinationScope.id == scope_id,
                )
                .with_for_update()
            )
            if row is None:
                if create is None:
                    raise CoordinationError("SCOPE_NOT_FOUND")
                state = State(create)
                row = CoordinationScope(
                    id=scope_id,
                    workspace_id=workspace_id,
                    source_id=UUID(create.source_id),
                    route_key=create.route_key or "-",
                    payload={},
                    updated_at=datetime.now(UTC),
                )
                session.add(row)
            else:
                state = self.codec.validate_json(self.cipher.decrypt(str(row.payload["encrypted"])))
            engine = Engine(records(state), state.config, lambda: datetime.now(UTC).isoformat())
            result = action(engine, state)
            encoded = self.codec.dump_json(state).decode()
            # 不允许无限堆积掩盖生产容量问题；来源清理和归档应在达到上限前执行。
            if len(encoded.encode()) > 16 * 1024 * 1024:
                raise CoordinationError("SCOPE_CAPACITY_EXCEEDED")
            row.payload = {
                "encrypted": self.cipher.encrypt(encoded),
                "config": self.cipher.encrypt(TypeAdapter(Config).dump_json(state.config).decode()),
            }
            row.updated_at = datetime.now(UTC)
            await session.flush()
            # 持久化的是编码后的快照；调用方之后修改返回对象不会隐式更新数据库。
            return result

    async def snapshot(self, workspace_id: UUID, scope_id: str) -> State:
        async with self.sessions() as session:
            row = await session.scalar(
                select(CoordinationScope).where(
                    CoordinationScope.workspace_id == workspace_id, CoordinationScope.id == scope_id
                )
            )
            if not row:
                raise CoordinationError("SCOPE_NOT_FOUND")
            return self.codec.validate_json(self.cipher.decrypt(str(row.payload["encrypted"])))

    async def config_for_routes(self, workspace_id: UUID, routes: tuple[str, ...]) -> Config | None:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(CoordinationScope).where(
                        CoordinationScope.workspace_id == workspace_id, CoordinationScope.route_key.in_(routes)
                    )
                )
            ).all()
            by_route = {row.route_key: row for row in rows}
            for route in routes:
                row = by_route.get(route)
                if row:
                    return TypeAdapter(Config).validate_json(self.cipher.decrypt(str(row.payload["config"])))
        return None

    async def claim(
        self, workspace_id: UUID, scope_id: str, effect_id: str, owner: str, *, seconds: int = 120
    ) -> Effect | None:
        """只领取待办或已失效的租约，返回本轮 owner/generation 快照；竞争失败返回 None。"""

        def claim(engine: Engine, state: State) -> Effect | None:
            effect = next((item for item in state.effects if item.id == effect_id), None)
            now = engine.now()
            if not effect or not effect.is_dispatchable(now):
                return None
            effect.state, effect.owner = "running", owner
            effect.lease_until = later(now, seconds)
            effect.attempts += 1
            return TypeAdapter(Effect).validate_python(asdict(effect))

        return await self.transact(workspace_id, scope_id, claim)

    async def renew(self, workspace_id: UUID, scope_id: str, effect: Effect, *, seconds: int = 60) -> bool:
        """仅当前有效 owner 可以延长租约，不会恢复已过期或被其他执行者接管的工作。"""

        def renew(engine: Engine, state: State) -> bool:
            current = next((item for item in state.effects if item.id == effect.id), None)
            if not current or not current.is_owned_by(effect.owner, engine.now()):
                return False
            current.lease_until = later(engine.now(), seconds)
            return True

        return await self.transact(workspace_id, scope_id, renew)

    async def settle(
        self, workspace_id: UUID, scope_id: str, effect: Effect, *, error: str | None = None, unknown: bool = False
    ) -> None:
        """结算当前尝试并释放租约；执行期间收到新一代工作时保留待办，不错误标成完成。"""

        def settle(engine: Engine, state: State) -> None:
            current = next((item for item in state.effects if item.id == effect.id), None)
            if not current or not current.is_owned_by(effect.owner, engine.now()):
                raise CoordinationError("EFFECT_LEASE_LOST")
            if unknown:
                current.state = "unknown"
            elif error:
                current.state = "blocked"
            else:
                current.state = "pending" if current.generation != effect.generation else "done"
            current.error = error
            current.owner = current.lease_until = None

        await self.transact(workspace_id, scope_id, settle)

    async def defer(
        self, workspace_id: UUID, scope_id: str, effect: Effect, *, delay: int = 2, error: str | None = None
    ) -> None:
        def defer(engine: Engine, state: State) -> None:
            current = next((item for item in state.effects if item.id == effect.id), None)
            if current and current.owner == effect.owner:
                current.state, current.owner, current.lease_until = "pending", None, None
                current.available_at = later(engine.now(), delay)
                current.error = error

        await self.transact(workspace_id, scope_id, defer)
