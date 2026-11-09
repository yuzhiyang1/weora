"""在独占随机 schema 验证 PostgreSQL 锁、回滚及迁移，绝不操作公共业务表。"""

from importlib import import_module

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection

from tests.api.test_task_continuations import (
    test_concurrent_input_retries_are_idempotent_and_preserve_order as check_concurrency,
)
from tests.api.test_task_continuations import (
    test_run_insert_failure_keeps_accepted_input_for_retry as check_rollback,
)
from tests.integration.test_approval_recovery_postgres import PostgresHarness
from tests.integration.test_approval_recovery_postgres import postgres_runs as postgres_runs
from weora.shared.infrastructure.database import Base

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_postgres_concurrent_continuations(postgres_runs: PostgresHarness) -> None:
    await check_concurrency(postgres_runs.runs)


async def test_postgres_failed_launch_preserves_input(postgres_runs: PostgresHarness) -> None:
    await check_rollback(postgres_runs.runs)


async def test_postgres_continuation_migration_roundtrip(postgres_runs: PostgresHarness) -> None:
    revision = import_module("migrations.versions.20261103_0016_task_continuations")

    def roundtrip(connection: Connection) -> None:
        # fixture 已创建完整元数据；先回退本次空表，再验证升级与 ORM 完全一致。
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            revision.downgrade()
            revision.upgrade()
        assert compare_metadata(context, Base.metadata) == []

    async with postgres_runs.engine.begin() as connection:
        await connection.run_sync(roundtrip)
    await check_rollback(postgres_runs.runs)

    def refuse_loss(connection: Connection) -> None:
        with (
            Operations.context(MigrationContext.configure(connection)),
            pytest.raises(RuntimeError, match="存在持续任务输入"),
        ):
            revision.downgrade()

    async with postgres_runs.engine.begin() as connection:
        await connection.run_sync(refuse_loss)
