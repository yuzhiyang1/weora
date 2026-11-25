"""在独立临时数据库验证迁移前进、回退以及新模型与迁移一致性。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import String, UniqueConstraint, create_engine, inspect
from sqlalchemy.schema import SchemaItem

from tests.support.settings import IsolatedAppSettings
from weora.bootstrap import settings
from weora.bootstrap.application import create_app  # noqa: F401
from weora.shared.infrastructure.database import Base

SQLITE_RULES_REVISION = "20261125_0036"


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
    # SQLite 只覆盖核心与 Rules 迁移。知识库迁移包含 PostgreSQL 的 ALTER CONSTRAINT。
    command.upgrade(config, SQLITE_RULES_REVISION)

    def included(obj: SchemaItem, name: str | None, type_: str, reflected: bool, compare_to: SchemaItem | None) -> bool:
        table = obj if type_ == "table" else getattr(obj, "table", None)
        # 知识库分支同时迁移了 Skill 的项目归属、命名空间和搜索索引。
        return table is None or (
            not table.name.startswith("knowledge_")
            and table.name not in {"skills", "skill_search_versions", "skill_search_terms"}
        )

    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert {"collaboration_tasks", "runs", "run_steps", "run_events", "run_dispatch_outbox"} <= tables
            context = MigrationContext.configure(connection, opts={"include_object": included})
            assert compare_metadata(context, Base.metadata) == []
        command.downgrade(config, "20260918_0003")
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert "runs" not in tables
            assert "collaboration_tasks" not in tables
            assert "agent_versions" in tables
        command.upgrade(config, SQLITE_RULES_REVISION)
        with engine.connect() as connection:
            assert "runs" in inspect(connection).get_table_names()
    finally:
        engine.dispose()


def test_repository_revision_nullable_after_upgrading_a_legacy_0027_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重现已停在 0027、且尚未执行 revision 可空迁移的数据库。"""
    path = tmp_path / "legacy.db"
    isolated = IsolatedAppSettings(database_url=f"sqlite+aiosqlite:///{path}", environment="test")
    monkeypatch.setattr(settings, "get_settings", lambda: isolated)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "migrations"))

    command.upgrade(config, "20261110_0027")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            # 当前 0027 的祖先已包含修复，显式恢复历史数据库实际保留的约束。
            operations = Operations(MigrationContext.configure(connection))
            with operations.batch_alter_table("project_run_repositories") as batch:
                batch.alter_column("revision", existing_type=String(64), nullable=False)

        command.upgrade(config, SQLITE_RULES_REVISION)
        with engine.connect() as connection:
            columns = {column["name"]: column for column in inspect(connection).get_columns("project_run_repositories")}
            assert columns["revision"]["nullable"]
    finally:
        engine.dispose()
