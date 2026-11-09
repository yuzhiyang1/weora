"""W07/W11 真实 PostgreSQL 验证；故障注入只发生在每例独占的随机 schema。"""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from tests.api.test_runs import RunsHarness
from tests.delivery.fakes import Browser, Remote, Sandbox
from tests.delivery.test_service import scope
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.application import create_app
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import ArtifactRecord, DeliveryError, PlanRecord, RemoteRecord
from weora.modules.delivery.infrastructure.persistence import SqlAlchemyDeliveryStore
from weora.modules.identity.infrastructure.models import WorkspaceMembershipModel
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.models import RunEventModel, RunModel
from weora.modules.runs.infrastructure.module_access import ContextModuleAccess, DeliveryModuleAccess
from weora.shared.infrastructure.database import Base

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@dataclass
class PostgresHarness:
    """所有连接都只解析自有 schema，连接池重建也不能回落到公共业务表。"""

    runs: RunsHarness
    engine: AsyncEngine
    url: str = field(repr=False)
    schema: str

    def reopen(self) -> AsyncEngine:
        return create_async_engine(self.url, connect_args={"server_settings": {"search_path": f'"{self.schema}"'}})


@pytest.fixture
async def postgres_runs() -> AsyncIterator[PostgresHarness]:
    url = os.environ.get("WEORA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("未显式提供 PostgreSQL 集成测试连接")
    # 标识符仅由程序生成；从不接受用户传入 schema，也不包含 public 回退路径。
    schema = "weora_approval_recovery_test_" + uuid4().hex
    admin = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": f'"{schema}"'}})
    created = False
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT current_schema()")) == schema
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        app = create_app(
            IsolatedAppSettings(environment="test", session_cookie_secure=False),
            session_factory=sessions,
            object_storage_factory=lambda _: None,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            registration = await client.post(
                "/api/v1/auth/register",
                json={
                    "display_name": "审批恢复验收",
                    "email": "isolated-postgres@example.com",
                    "password": "isolated generated fixture password",
                    "workspace_name": "私有 schema 验收组",
                },
            )
            assert registration.status_code == 201, registration.text
            identity = registration.json()
            workspace_id = identity["workspace"]["id"]
            headers = {"X-CSRF-Token": client.cookies["weora_csrf"]}
            agent = await client.post(
                f"/api/v1/workspaces/{workspace_id}/agents",
                json={
                    "display_name": "审批恢复助手",
                    "description": "不请求模型或外部工具",
                    "system_prompt": "仅验证平台授权与持久化。",
                    "skill_version_ids": [],
                },
                headers=headers,
            )
            assert agent.status_code == 201, agent.text
            agent_id = agent.json()["id"]
            version = await client.post(
                f"/api/v1/workspaces/{workspace_id}/agents/{agent_id}/publish",
                json={"version": "0.1.0", "changelog": "PostgreSQL 独立集成用例"},
                headers=headers,
            )
            assert version.status_code == 201, version.text
            runs = RunsHarness(
                client, app, sessions, workspace_id, identity["user"]["id"], agent_id, version.json()["id"]
            )
            yield PostgresHarness(runs, engine, url, schema)
    finally:
        await engine.dispose()
        if created:
            async with admin.begin() as connection:
                # 仅清理由上面 uuid4 生成、且由本 fixture 成功创建的 schema。
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def prepared(harness: PostgresHarness) -> tuple[DeliveryService, Sandbox, RunActor, UUID]:
    runs = harness.runs
    response = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    assert response.status_code == 201, response.text
    run_id = UUID(response.json()["id"])
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    access = DeliveryModuleAccess(
        ContextModuleAccess(runs.app.state.identity_service, runs.app.state.run_service),
        SqlAlchemyExecutionStore(runs.sessions),
        None,
        execution_workspace_id=actor.workspace_id,
    )
    sandbox = Sandbox()
    service = DeliveryService(SqlAlchemyDeliveryStore(runs.sessions), access, sandbox, Browser(), Remote())
    runs.app.state.delivery_service = service
    return service, sandbox, actor, run_id


async def test_postgres_http_approval_scope_and_superseding_plan(postgres_runs: PostgresHarness) -> None:
    """人工接口受 CSRF 保护；计划精确范围和版本变化即时撤销旧执行权。"""
    service, sandbox, actor, run_id = await prepared(postgres_runs)
    runs = postgres_runs.runs
    executions = SqlAlchemyExecutionStore(runs.sessions)
    first = await service.propose(actor, run_id, scope(), "只改批准目录", "回归测试")
    assert (await runs.client.get(f"{runs.path}/{run_id}")).json()["status"] == "WAITING_APPROVAL"
    path = f"{runs.path}/{run_id}/plan/{first.id}/decision"
    assert (await runs.client.post(path, json={"decision": "approve"})).status_code == 403
    approved = await runs.client.post(path, json={"decision": "approve"}, headers=runs.headers())
    assert approved.status_code == 200, approved.text
    assert approved.json()["plan"]["status"] == "APPROVED"
    lease = await executions.claim(
        workspace_id=actor.workspace_id, run_id=run_id, owner="approved-worker", lease_seconds=30
    )
    assert lease is not None
    with pytest.raises(DeliveryError) as error:
        await service.command(actor, run_id, ("sh", "-c", "echo forbidden"), lease.fencing_token)
    assert error.value.code == "COMMAND_DENIED" and sandbox.calls == 0
    evidence = await service.command(actor, run_id, ("python", "-m", "pytest"), lease.fencing_token)
    assert evidence.status == "PASSED" and evidence.plan_id == first.id
    second = await service.propose(actor, run_id, replace(scope(), paths=("tests",)), "新范围", "需重新批准")
    assert second.version == 2 and second.status == "PENDING"
    assert not await executions.authorize_token(actor, run_id, lease.fencing_token, write=True)
    with pytest.raises(DeliveryError) as old:
        await service.decide(actor, run_id, first.id, "approve")
    assert old.value.code == "PLAN_CHANGED"
    previous = await service.store.get_plan(actor.workspace_id, first.id)
    assert previous is not None and previous.status == "SUPERSEDED"
    assert await service.store.latest_plan(uuid4(), run_id) is None


async def test_postgres_approval_cas_and_remote_unknown_survive_new_pool(postgres_runs: PostgresHarness) -> None:
    """并发审批只能提交一次；未知远端结果在全新连接池中仍不可重占业务键。"""
    service, _, actor, run_id = await prepared(postgres_runs)
    now = datetime.now(UTC)
    proposals = [
        PlanRecord(uuid4(), run_id, actor.workspace_id, actor.user_id, 1, scope(), "方案", "风险", "PENDING", now)
        for _ in range(6)
    ]
    winners = await asyncio.gather(
        *(service.store.save_plan(plan, expected_version=0, expected_status=None) for plan in proposals)
    )
    assert sum(winners) == 1
    saved = await service.store.latest_plan(actor.workspace_id, run_id)
    assert saved is not None
    approved = replace(
        saved, status="APPROVED", decided_by=actor.user_id, decided_at=now, expires_at=now + timedelta(hours=1)
    )
    approvals = await asyncio.gather(
        *(service.store.save_plan(approved, expected_version=1, expected_status="PENDING") for _ in range(6))
    )
    assert sum(approvals) == 1
    pending = RemoteRecord(uuid4(), run_id, actor.workspace_id, "d" * 64, "SENDING", 1, now, now, plan_id=saved.id)
    claims = await asyncio.gather(*(service.store.claim_remote(replace(pending, id=uuid4())) for _ in range(6)))
    assert sum(claims) == 1
    original = await service.store.get_remote(actor.workspace_id, run_id, pending.business_key)
    assert original is not None
    unknown = replace(original, status="UNKNOWN", error_code="REMOTE_UNKNOWN")
    assert await service.store.finish_remote(unknown, expected_status="SENDING")
    reopened = postgres_runs.reopen()
    try:
        store = SqlAlchemyDeliveryStore(async_sessionmaker(reopened, expire_on_commit=False))
        assert await store.latest_plan(actor.workspace_id, run_id) == approved
        assert await store.get_remote(actor.workspace_id, run_id, pending.business_key) == unknown
        assert not await store.claim_remote(replace(pending, id=uuid4()))
        assert not await store.finish_remote(pending, expected_status="SENDING")
    finally:
        await reopened.dispose()


@pytest.mark.parametrize("revoke", ["approval", "membership"])
async def test_postgres_revocation_interrupts_running_tool(postgres_runs: PostgresHarness, revoke: str) -> None:
    """持续执行的工具也必须响应持久化撤权，不能等输出完成才检查。"""
    service, sandbox, actor, run_id = await prepared(postgres_runs)
    plan = await service.propose(actor, run_id, scope(), "运行长命令", "允许中途撤销")
    await service.decide(actor, run_id, plan.id, "approve")
    executions = SqlAlchemyExecutionStore(postgres_runs.runs.sessions)
    lease = await executions.claim(
        workspace_id=actor.workspace_id, run_id=run_id, owner="revocation-worker", lease_seconds=30
    )
    assert lease is not None
    sandbox.block = True
    operation = asyncio.create_task(service.command(actor, run_id, ("python", "-m", "pytest"), lease.fencing_token))
    try:
        await asyncio.wait_for(sandbox.started.wait(), 5)
        if revoke == "approval":
            await service.decide(actor, run_id, plan.id, "revoke")
        else:
            async with postgres_runs.runs.sessions() as session, session.begin():
                await session.execute(
                    update(WorkspaceMembershipModel)
                    .where(
                        WorkspaceMembershipModel.user_id == actor.user_id,
                        WorkspaceMembershipModel.workspace_id == actor.workspace_id,
                    )
                    .values(status="inactive")
                )
        with pytest.raises(DeliveryError):
            await asyncio.wait_for(operation, 5)
        assert sandbox.cleanups >= 1
        assert await service.store.artifacts(actor.workspace_id, run_id) == []
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_postgres_lease_recovery_is_explicit_persistent_and_read_only(postgres_runs: PostgresHarness) -> None:
    """租约失效不自动重跑；人工恢复只读，旧 Worker 永远不能覆盖新尝试。"""
    _, _, actor, run_id = await prepared(postgres_runs)
    runs = postgres_runs.runs
    store = SqlAlchemyExecutionStore(runs.sessions)
    claims = await asyncio.gather(
        *(
            store.claim(workspace_id=actor.workspace_id, run_id=run_id, owner=f"worker-{i}", lease_seconds=30)
            for i in range(6)
        )
    )
    leases = [lease for lease in claims if lease is not None]
    assert len(leases) == 1
    lease = leases[0]
    assert not await store.finish(replace(lease, owner="wrong-owner"), status="SUCCEEDED", output="不可提交")
    # PostgreSQL 使用数据库时钟；仅把本例私有表中的租约推进到过去，不更改公共实例时钟。
    async with runs.sessions() as session, session.begin():
        await session.execute(
            update(RunModel)
            .where(RunModel.id == run_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    assert not await store.renew(lease, lease_seconds=30)
    assert not await store.finish(lease, status="SUCCEEDED", output="迟到结果")
    assert await store.recover_expired() == 1
    assert await store.recover_expired() == 0
    assert (
        await store.claim(workspace_id=actor.workspace_id, run_id=run_id, owner="automatic-retry", lease_seconds=30)
        is None
    )
    reopened = postgres_runs.reopen()
    try:
        sessions = async_sessionmaker(reopened, expire_on_commit=False)
        recovered_store = SqlAlchemyExecutionStore(sessions)
        assert not await recovered_store.recover_read_only(RunActor(uuid4(), actor.workspace_id), run_id)
        response = await runs.client.post(
            f"{runs.path}/{run_id}/recover", json={"mode": "read_only"}, headers=runs.headers()
        )
        assert response.status_code == 200, response.text
        assert response.json()["recovery_read_only"] is True
        assert await recovered_store.recover_read_only(actor, run_id)
        new = await recovered_store.claim(
            workspace_id=actor.workspace_id, run_id=run_id, owner="human-recovered", lease_seconds=30
        )
        assert new is not None and new.fencing_token > lease.fencing_token
        assert await recovered_store.authorize_token(actor, run_id, new.fencing_token)
        assert not await recovered_store.authorize_token(actor, run_id, new.fencing_token, write=True)
        assert not await recovered_store.finish(lease, status="SUCCEEDED", output="旧进程迟到结果")
        assert await recovered_store.finish(new, status="SUCCEEDED", output="恢复后只读结论")
        assert not await recovered_store.recover_read_only(actor, run_id)
        async with sessions() as session:
            model = await session.get(RunModel, run_id)
            assert model is not None and model.output_text == "恢复后只读结论"
            events = list(
                await session.scalars(
                    select(RunEventModel).where(RunEventModel.run_id == run_id).order_by(RunEventModel.sequence)
                )
            )
            assert [item.sequence for item in events] == [1, 2, 3, 4, 5, 6]
            assert [item.event_type for item in events] == [
                "run.queued",
                "run.started",
                "run.recovery_required",
                "run.recovered_read_only",
                "run.started",
                "run.succeeded",
            ]
    finally:
        await reopened.dispose()


async def test_postgres_artifact_sequence_is_unique_under_concurrency(postgres_runs: PostgresHarness) -> None:
    """真实连接并发写事实结果时，顺序可持久化且不重复，不依赖墙钟分辨率。"""
    service, _, actor, run_id = await prepared(postgres_runs)
    now = datetime.now(UTC)
    artifacts = [
        ArtifactRecord(
            uuid4(),
            run_id,
            actor.workspace_id,
            "report",
            "PASSED",
            f"事实 {index}",
            now,
            now + timedelta(days=1),
            "e" * 64,
        )
        for index in range(6)
    ]
    stored = await asyncio.gather(*(service.store.add_artifact(item) for item in artifacts))
    assert sorted(item.sequence for item in stored) == list(range(1, 7))
    assert [item.sequence for item in await service.store.artifacts(actor.workspace_id, run_id)] == list(range(1, 7))


async def test_postgres_terminal_event_failure_rolls_back_entire_commit(postgres_runs: PostgresHarness) -> None:
    """终态事件落库失败时，输出和状态也必须回滚，不能留下没有审计的成功。"""
    _, _, actor, run_id = await prepared(postgres_runs)
    runs = postgres_runs.runs
    store = SqlAlchemyExecutionStore(runs.sessions)
    lease = await store.claim(
        workspace_id=actor.workspace_id, run_id=run_id, owner="transaction-worker", lease_seconds=30
    )
    assert lease is not None

    def fail_own_event(_: object, __: object, target: RunEventModel) -> None:
        if target.run_id == run_id and target.event_type == "run.succeeded":
            raise RuntimeError("isolated terminal event failure")

    event.listen(RunEventModel, "before_insert", fail_own_event)
    try:
        with pytest.raises(RuntimeError, match="isolated terminal event failure"):
            await store.finish(lease, status="SUCCEEDED", output="不可部分提交的结果")
    finally:
        event.remove(RunEventModel, "before_insert", fail_own_event)
    async with runs.sessions() as session:
        model = await session.get(RunModel, run_id)
        assert model is not None and model.status == "RUNNING"
        assert model.output_text is None and model.last_sequence == 2
        assert await session.scalar(select(func.count()).select_from(RunEventModel)) == 2
    assert await store.finish(lease, status="SUCCEEDED", output="事务修复后重新提交")


async def test_postgres_cancellation_wins_against_late_success(postgres_runs: PostgresHarness) -> None:
    """取消持久化后，即使旧 Worker 已算出结果，也只能提交取消终态而不能泄露输出。"""
    _, _, actor, run_id = await prepared(postgres_runs)
    runs = postgres_runs.runs
    store = SqlAlchemyExecutionStore(runs.sessions)
    lease = await store.claim(workspace_id=actor.workspace_id, run_id=run_id, owner="cancel-worker", lease_seconds=30)
    assert lease is not None
    response = await runs.client.post(f"{runs.path}/{run_id}/cancel", headers=runs.headers())
    assert response.status_code == 200 and response.json()["cancellation_requested_at"] is not None
    assert not await store.authorize_token(actor, run_id, lease.fencing_token)
    assert not await store.renew(lease, lease_seconds=30)
    assert await store.finish(lease, status="SUCCEEDED", output="已经算出但不可交付的正文")
    assert not await store.finish(lease, status="SUCCEEDED", output="重复提交")
    detail = (await runs.client.get(f"{runs.path}/{run_id}")).json()
    assert detail["status"] == "CANCELLED" and detail["output_text"] is None
    assert detail["failure_code"] == "REMOTE_COMPLETION_UNCONFIRMED"
    assert not await store.recover_read_only(actor, run_id)
