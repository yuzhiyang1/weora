"""在独立临时数据库验证迁移前进、回退以及新模型与迁移一致性。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import UniqueConstraint, create_engine, inspect

from tests.support.settings import IsolatedAppSettings
from weora.bootstrap import settings
from weora.bootstrap.application import create_app  # noqa: F401
from weora.shared.infrastructure.database import Base


def test_tool_call_identity_has_no_redundant_unique_constraint() -> None:
    """PostgreSQL 会合并建表时与主键相同的唯一声明，元数据不能再要求它存在。"""
    table = Base.metadata.tables["tool_invocations"]
    primary_columns = tuple(column.name for column in table.primary_key.columns)
    assert primary_columns == ("run_id", "call_id")
    assert not any(
        isinstance(constraint, UniqueConstraint)
        and tuple(column.name for column in constraint.columns) == primary_columns
        for constraint in table.constraints
    )


def test_console_run_migration_matches_metadata_and_can_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只操作本测试创建的 SQLite 文件，不读取或迁移开发者的真实数据库。"""
    path = tmp_path / "migration.db"
    monkeypatch.setattr(
        settings,
        "get_settings",
        lambda: IsolatedAppSettings(
            database_url=f"sqlite+aiosqlite:///{path}",
            environment="test",
        ),
    )
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "migrations"))
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert {"collaboration_tasks", "runs", "run_steps", "run_events", "run_dispatch_outbox"} <= tables
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        command.downgrade(config, "20260918_0003")
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert "runs" not in tables
            assert "collaboration_tasks" not in tables
            assert "agent_versions" in tables
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert "runs" in inspect(connection).get_table_names()
    finally:
        engine.dispose()
