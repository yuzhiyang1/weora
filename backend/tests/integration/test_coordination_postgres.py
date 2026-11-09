"""独占随机 schema 的真实 PostgreSQL 并发、幂等和迁移验证。"""

import asyncio
from dataclasses import replace
from importlib import import_module
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet
from sqlalchemy import Connection

from tests.coordination.test_engine import message, result, setup
from tests.integration.test_approval_recovery_postgres import PostgresHarness
from tests.integration.test_approval_recovery_postgres import postgres_runs as postgres_runs
from weora.modules.context.feishu import FernetSecrets
from weora.modules.coordination.domain import CoordinationError
from weora.modules.coordination.records import Effects
from weora.modules.coordination.store import SqlCoordinationStore
from weora.shared.infrastructure.database import Base

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_atomic_intake_decision_effect_claim_and_restart(postgres_runs: PostgresHarness) -> None:
    h = postgres_runs
    workspace_id = uuid4()
    _, state = setup()
    config = replace(state.config, id=uuid4().hex, workspace_id=str(workspace_id), source_id=str(uuid4()))
    cipher = FernetSecrets(Fernet.generate_key().decode())
    store = SqlCoordinationStore(h.runs.sessions, cipher)
    await store.transact(workspace_id, config.id, lambda _, current: None, create=config)
    trigger = replace(message(), workspace_id=str(workspace_id), scope_id=config.id)
    intake = await asyncio.gather(
        *(store.transact(workspace_id, config.id, lambda engine, _: engine.receive(trigger)) for _ in range(12))
    )
    assert sum(intake) == 1
    evaluation = (await store.transact(workspace_id, config.id, lambda engine, _: engine.pass_due()))[0]
    await store.transact(workspace_id, config.id, lambda engine, _: engine.read_eval(evaluation.id))
    decisions = await asyncio.gather(
        *(
            store.transact(workspace_id, config.id, lambda engine, _: engine.decide(evaluation.id, result()))
            for _ in range(8)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in decisions) == 1
    assert all(not isinstance(item, Exception) or isinstance(item, CoordinationError) for item in decisions)
    fresh = SqlCoordinationStore(h.runs.sessions, cipher)
    restored = await fresh.snapshot(workspace_id, config.id)
    assert len(restored.tickets) == len(restored.decisions) == len(restored.triggers) == 1
    effect = next(item for item in restored.effects if item.kind == "worker")
    claimed = await asyncio.gather(*(fresh.claim(workspace_id, config.id, effect.id, str(index)) for index in range(8)))
    assert sum(item is not None for item in claimed) == 1
    owner = next(item for item in claimed if item is not None)
    assert await fresh.renew(workspace_id, config.id, owner)
    await fresh.transact(
        workspace_id,
        config.id,
        lambda _, state: Effects(state).enqueue("worker", effect.business_key, dict(effect.data, input_count=2)),
    )
    await fresh.settle(workspace_id, config.id, owner)
    updated = await fresh.snapshot(workspace_id, config.id)
    current = next(item for item in updated.effects if item.id == effect.id)
    assert current.state == "pending" and current.generation > owner.generation
    new_owner = await fresh.claim(workspace_id, config.id, effect.id, "new-owner")
    assert new_owner
    assert not await fresh.renew(workspace_id, config.id, owner)
    with pytest.raises(CoordinationError, match="EFFECT_LEASE_LOST"):
        await fresh.settle(workspace_id, config.id, owner)
    await fresh.settle(workspace_id, config.id, new_owner)


async def test_coordination_migration_roundtrip_matches_metadata(postgres_runs: PostgresHarness) -> None:
    revision = import_module("migrations.versions.20261103_0017_coordination")

    def roundtrip(connection: Connection) -> None:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            revision.downgrade()
            revision.upgrade()
        assert compare_metadata(context, Base.metadata) == []

    async with postgres_runs.engine.begin() as connection:
        await connection.run_sync(roundtrip)


async def test_repository_origin_migration_roundtrip(postgres_runs: PostgresHarness) -> None:
    """真实 PostgreSQL CHECK 约束也认识仓库来源，不只依赖 Python 或 create_all 测试。"""
    from sqlalchemy import inspect

    revision = import_module("migrations.versions.20261104_0018_repository_intake")

    def roundtrip(connection: Connection) -> None:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            revision.downgrade()
            old = inspect(connection).get_check_constraints("collaboration_tasks")
            assert not any("code_hosting" in row["sqltext"] for row in old)
            revision.upgrade()
        checks = inspect(connection).get_check_constraints("collaboration_tasks")
        assert any("code_hosting" in row["sqltext"] for row in checks)
        assert compare_metadata(context, Base.metadata) == []

    async with postgres_runs.engine.begin() as connection:
        await connection.run_sync(roundtrip)
