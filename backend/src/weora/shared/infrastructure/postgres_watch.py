"""每个进程共享 PostgreSQL 通知连接，订阅者由周期复验补偿丢失的信号。"""

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


class NotificationConnection(Protocol):
    """asyncpg 未提供类型声明，只声明监听器所用的驱动能力。"""

    async def add_listener(
        self,
        channel: str,
        callback: Callable[["NotificationConnection", int, str, str], None],
    ) -> None: ...
    async def remove_listener(
        self,
        channel: str,
        callback: Callable[["NotificationConnection", int, str, str], None],
    ) -> None: ...
    def add_termination_listener(self, callback: Callable[["NotificationConnection"], None]) -> None: ...
    def remove_termination_listener(self, callback: Callable[["NotificationConnection"], None]) -> None: ...
    def is_closed(self) -> bool: ...


class PostgresWatch:
    """同一频道的订阅不独占数据库连接。"""

    def __init__(self, channel: str) -> None:
        self._channel = channel
        self._waiters: dict[str, set[asyncio.Event]] = {}

    @contextmanager
    def subscribe(self, resource_id: UUID) -> Iterator[asyncio.Event]:
        key = str(resource_id)
        event = asyncio.Event()
        waiters = self._waiters.setdefault(key, set())
        waiters.add(event)
        try:
            yield event
        finally:
            waiters.remove(event)
            if not waiters:
                del self._waiters[key]

    def _notify(self, _: NotificationConnection, _pid: int, _channel: str, resource_id: str) -> None:
        for event in self._waiters.get(resource_id, ()):
            event.set()

    async def listen(self, engine: AsyncEngine) -> None:
        """连接中断后自动恢复；订阅期间不持有业务事务。"""
        if engine.url.drivername != "postgresql+asyncpg":
            return
        while True:
            try:
                async with engine.connect() as connection:
                    raw = await connection.get_raw_connection()
                    driver = cast(NotificationConnection, raw.driver_connection)
                    disconnected = asyncio.Event()

                    def on_disconnect(_: NotificationConnection, signal: asyncio.Event = disconnected) -> None:
                        signal.set()

                    driver.add_termination_listener(on_disconnect)
                    try:
                        await driver.add_listener(self._channel, self._notify)
                        await disconnected.wait()
                    finally:
                        driver.remove_termination_listener(on_disconnect)
                        if not driver.is_closed():
                            await driver.remove_listener(self._channel, self._notify)
            except Exception as error:
                logger.warning("postgres watch listener unavailable: %s", type(error).__name__)
            await asyncio.sleep(5)
