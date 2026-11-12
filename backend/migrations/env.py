"""Alembic 异步迁移环境。"""

from asyncio import run
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from weora.bootstrap.settings import get_settings
from weora.modules.agents.infrastructure import models as agent_models  # noqa: F401
from weora.modules.collaboration.infrastructure import agent_mail as agent_mail_models  # noqa: F401
from weora.modules.collaboration.infrastructure import tasks as task_models  # noqa: F401
from weora.modules.computers import credentials as computer_models  # noqa: F401
from weora.modules.console_settings import store as console_settings_models  # noqa: F401
from weora.modules.context import store as context_models  # noqa: F401
from weora.modules.coordination import store as coordination_models  # noqa: F401
from weora.modules.delivery.infrastructure import persistence as delivery_models  # noqa: F401
from weora.modules.identity.infrastructure import models as identity_models  # noqa: F401
from weora.modules.projects import persistence as project_models  # noqa: F401
from weora.modules.runs.infrastructure import models as run_models  # noqa: F401
from weora.modules.skills.infrastructure import models as skills_models  # noqa: F401
from weora.modules.tools.infrastructure import store as tool_models  # noqa: F401
from weora.shared.infrastructure.database import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# URL 由统一配置读取，避免迁移与运行时连接到不同数据库。
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """在不创建数据库连接时生成迁移 SQL。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    """在 Alembic 的同步回调中执行迁移。"""
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """使用异步 SQLAlchemy 引擎执行迁移。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run(run_migrations_online())
