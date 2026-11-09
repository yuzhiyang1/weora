"""SQLAlchemy 异步数据库基础设施。"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型共享的声明基类。"""


SessionFactory = async_sessionmaker[AsyncSession]


def create_database(database_url: str) -> tuple[AsyncEngine, SessionFactory]:
    """创建进程级数据库引擎和会话工厂，真正连接会在首次请求时建立。"""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
