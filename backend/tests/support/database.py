"""测试数据库约束辅助；SQLite 不能依赖默认关闭外键的行为。"""

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine


def enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """仅给明确传入的 SQLite 测试引擎开启外键，不影响真实 PostgreSQL 配置。"""
    if engine.dialect.name != "sqlite":
        raise ValueError("该测试辅助只支持 SQLite")

    @event.listens_for(engine.sync_engine, "connect")
    def configure(connection: Any, _: object) -> None:
        # SQLAlchemy 的异步 DBAPI 桥没有稳定的公共类型，Any 仅限制在此驱动边界。
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
