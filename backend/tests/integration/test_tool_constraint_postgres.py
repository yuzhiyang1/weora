"""显式启用的 PostgreSQL 迁移回归，仅创建本测试独有的临时 schema。"""

import os
import runpy
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.parametrize("separate_unique", [False, True])
async def test_tool_migration_preserves_postgres_primary_key(separate_unique: bool) -> None:
    """覆盖 CREATE TABLE 合并约束与 ALTER TABLE 单独添加约束两种真实状态。"""
    url = os.environ.get("WEORA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("未显式提供 PostgreSQL 集成测试连接")
    engine = create_async_engine(url)
    schema = "weora_constraint_test_" + uuid4().hex
    migration = runpy.run_path(
        str(Path(__file__).parents[2] / "migrations/versions/20261017_0011_tool_call_primary_key.py")
    )
    try:
        async with engine.begin() as connection:
            # 名称完全由测试生成，不接受外部传入的 schema 或 SQL 标识符。
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            unique = "" if separate_unique else ", CONSTRAINT uq_tool_invocation_call UNIQUE (run_id, call_id)"
            await connection.execute(
                text(
                    "CREATE TABLE tool_invocations (run_id UUID, call_id VARCHAR(128), "
                    f"PRIMARY KEY (run_id, call_id){unique})"
                )
            )
            if separate_unique:
                await connection.execute(
                    text("ALTER TABLE tool_invocations ADD CONSTRAINT uq_tool_invocation_call UNIQUE(run_id,call_id)")
                )
            run_id = uuid4()
            await connection.execute(
                text("INSERT INTO tool_invocations VALUES (:run_id, 'existing-call')"), {"run_id": run_id}
            )

            def migrate(sync_connection: Connection, direction: str) -> None:
                with Operations.context(MigrationContext.configure(sync_connection)):
                    migration[direction]()

            for direction in ("upgrade", "downgrade", "upgrade"):
                await connection.run_sync(migrate, direction)
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_constraint "
                            "WHERE conrelid='tool_invocations'::regclass AND contype='p'"
                        )
                    )
                    == 1
                )
                assert await connection.scalar(text("SELECT count(*) FROM tool_invocations")) == 1
            assert (
                await connection.scalar(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid='tool_invocations'::regclass AND contype='p'"
                    )
                )
                == "PRIMARY KEY (run_id, call_id)"
            )
            await connection.execute(
                text(
                    "INSERT INTO tool_invocations VALUES (:run_id, 'existing-call') "
                    "ON CONFLICT (run_id, call_id) DO NOTHING"
                ),
                {"run_id": run_id},
            )
            assert await connection.scalar(text("SELECT count(*) FROM tool_invocations")) == 1
            # 正常路径也在同一事务内删除自有 schema；异常时事务回滚会撤销创建。
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        await engine.dispose()
