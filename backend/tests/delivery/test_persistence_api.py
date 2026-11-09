"""真实 SQL 事务与 HTTP 权限验证，不使用 Redis 或外部供应商。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from tests.api.test_runs import RunsHarness, runs
from tests.delivery.fakes import Access, Browser, Remote, Sandbox
from tests.delivery.test_service import scope
from weora.modules.delivery.api.routes import router
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import ArtifactRecord, PlanRecord, RemoteRecord
from weora.modules.delivery.infrastructure.persistence import (
    DeliveryArtifactModel,
    DeliveryPlanModel,
    SqlAlchemyDeliveryStore,
)
from weora.modules.runs.application.ports import RunActor

# fixture 注册本身是这里的用途；显式别名同时避免把它当作未使用的普通导入。
delivery_runs = runs


@pytest.mark.anyio
async def test_frozen_continuation_branch_and_sha_survive_sql_restart(delivery_runs: RunsHarness) -> None:
    service, access, store = await configured(delivery_runs)
    selected = replace(
        scope(remote=True),
        actions=("git.commit", "git.push"),
        source_delivery_id=str(uuid4()),
        source_branch="weora/" + "d" * 24,
        source_revision="b" * 40,
    )
    selected.validate()
    record = PlanRecord(
        uuid4(),
        access.run_id,
        access.actor.workspace_id,
        access.actor.user_id,
        1,
        selected,
        "续写原PR",
        "SHA 漂移拒绝",
        "PENDING",
        datetime.now(UTC),
    )
    assert await store.save_plan(record, expected_version=0, expected_status=None)
    restored = await SqlAlchemyDeliveryStore(delivery_runs.sessions).latest_plan(
        access.actor.workspace_id, access.run_id
    )
    assert restored and restored.scope == selected and restored.scope.digest == selected.digest
    assert replace(selected, source_revision="c" * 40).digest != selected.digest


async def configured(runs: RunsHarness) -> tuple[DeliveryService, Access, SqlAlchemyDeliveryStore]:
    response = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    assert response.status_code == 201, response.text
    run_id = UUID(response.json()["id"])
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    access = Access(actor, run_id)
    store = SqlAlchemyDeliveryStore(runs.sessions)
    service = DeliveryService(store, access, Sandbox(), Browser(), Remote())
    runs.app.state.delivery_service = service
    if not any(getattr(route, "path", "").endswith("/plan") for route in runs.app.routes):
        runs.app.include_router(router, prefix="/api/v1")
    return service, access, store


@pytest.mark.anyio
async def test_sql_plan_compare_and_swap_keeps_only_one_concurrent_approval(delivery_runs: RunsHarness) -> None:
    runs = delivery_runs
    service, access, store = await configured(runs)
    actor, run_id = access.actor, access.run_id
    plan = await service.propose(actor, run_id, scope(), "只改登录模块", "影响登录")
    now = datetime.now(UTC)
    approved = replace(
        plan, status="APPROVED", decided_by=actor.user_id, decided_at=now, expires_at=now + timedelta(hours=1)
    )
    results = await asyncio.gather(
        *(store.save_plan(approved, expected_version=1, expected_status="PENDING") for _ in range(4))
    )
    assert sum(results) == 1
    recovered = await SqlAlchemyDeliveryStore(runs.sessions).latest_plan(actor.workspace_id, run_id)
    assert recovered == approved
    next_plan = await service.propose(actor, run_id, replace(scope(), paths=("tests",)), "只改测试", "低风险")
    assert next_plan.version == 2 and next_plan.status == "PENDING"
    async with runs.sessions() as session:
        assert (
            await session.scalar(select(DeliveryPlanModel.status).where(DeliveryPlanModel.id == plan.id))
            == "SUPERSEDED"
        )


@pytest.mark.anyio
async def test_new_plan_version_uniqueness_and_remote_claim_survive_restart(delivery_runs: RunsHarness) -> None:
    runs = delivery_runs
    _, access, store = await configured(runs)
    now = datetime.now(UTC)
    actor, run_id = access.actor, access.run_id
    plans = [
        PlanRecord(uuid4(), run_id, actor.workspace_id, actor.user_id, 1, scope(), "计划", "风险", "PENDING", now)
        for _ in range(3)
    ]
    results = await asyncio.gather(*(store.save_plan(plan, expected_version=0, expected_status=None) for plan in plans))
    assert sum(results) == 1
    record = RemoteRecord(uuid4(), run_id, actor.workspace_id, "b" * 64, "SENDING", 1, now, now)
    assert await store.claim_remote(record)
    assert not await store.claim_remote(replace(record, id=uuid4()))
    reopened = SqlAlchemyDeliveryStore(runs.sessions)
    assert await reopened.get_remote(actor.workspace_id, run_id, record.business_key) == record
    unknown = replace(record, status="UNKNOWN", error_code="REMOTE_UNKNOWN")
    assert await reopened.finish_remote(unknown, expected_status="SENDING")
    assert not await reopened.finish_remote(record, expected_status="SENDING")


@pytest.mark.anyio
async def test_source_cleanup_redacts_all_derived_bodies_and_revokes_grant(delivery_runs: RunsHarness) -> None:
    runs = delivery_runs
    service, access, store = await configured(runs)
    actor, run_id = access.actor, access.run_id
    plan = await service.propose(actor, run_id, scope(), "敏感上下文推导", "风险详情")
    await service.decide(actor, run_id, plan.id, "approve", reason="批准详情")
    artifact = await service.command(actor, run_id, ("python", "-m", "pytest"), 7)
    assert await service.purge_runs(actor.workspace_id, (run_id,)) == 2
    recovered = await store.latest_plan(actor.workspace_id, run_id)
    assert recovered and recovered.summary == "[上下文已过期]" and recovered.risk == "[上下文已过期]"
    assert recovered.status == "REVOKED" and recovered.reason is None and recovered.scope.commands == ()
    evidence = (await store.artifacts(actor.workspace_id, run_id))[0]
    assert evidence.body is None and evidence.command == () and evidence.sha256 == artifact.sha256
    async with runs.sessions() as session:
        assert await session.scalar(select(DeliveryPlanModel.scope_sha256)) == plan.scope.digest
    assert await service.purge_runs(uuid4(), (run_id,)) == 0


@pytest.mark.anyio
async def test_sql_expired_artifact_body_is_physically_purged(delivery_runs: RunsHarness) -> None:
    runs = delivery_runs
    service, access, store = await configured(runs)
    now = datetime.now(UTC)
    artifact = ArtifactRecord(
        uuid4(),
        access.run_id,
        access.actor.workspace_id,
        "report",
        "PASSED",
        "sensitive",
        now - timedelta(days=8),
        now - timedelta(seconds=1),
        "c" * 64,
    )
    await store.add_artifact(artifact)
    assert await service.purge_expired() == 1
    async with runs.sessions() as session:
        model = await session.get(DeliveryArtifactModel, artifact.id)
        assert model and model.body is None and model.sha256 == "c" * 64


@pytest.mark.anyio
async def test_http_plan_approval_csrf_and_revoked_content(delivery_runs: RunsHarness) -> None:
    runs = delivery_runs
    _, access, _ = await configured(runs)
    path = f"{runs.path}/{access.run_id}"
    assert (await runs.client.get(path + "/plan")).json() == {"plan": None}
    payload = {
        "scope": {
            "repository_id": "test-repo",
            "base_revision": "a" * 40,
            "paths": ["src", "tests"],
            "commands": [["python", "-m", "pytest"]],
            "actions": ["file.write", "command.run", "git.diff"],
        },
        "summary": "修复计划",
        "risk": "需回归登录",
    }
    proposed = await runs.client.post(path + "/plan", json=payload, headers=runs.headers())
    assert proposed.status_code == 200, proposed.text
    plan_id = proposed.json()["plan"]["id"]
    no_csrf = await runs.client.post(path + f"/plan/{plan_id}/decision", json={"decision": "approve"})
    assert no_csrf.status_code == 403
    approved = await runs.client.post(
        path + f"/plan/{plan_id}/decision", json={"decision": "approve"}, headers=runs.headers()
    )
    assert approved.status_code == 200 and approved.json()["plan"]["status"] == "APPROVED"
    bad_workspace = str(uuid4())
    cross = await runs.client.get(f"/api/v1/workspaces/{bad_workspace}/runs/{access.run_id}/plan")
    assert cross.status_code == 404
    access.allowed = False
    assert (await runs.client.get(path + "/plan")).status_code == 404
    assert (await runs.client.get(path + "/artifacts")).status_code == 404
    assert (await runs.client.get(path + "/deliveries")).status_code == 404


@pytest.mark.anyio
async def test_remote_binding_survives_database_reopen_and_api_cannot_supply_it(delivery_runs: RunsHarness) -> None:
    runs = delivery_runs
    service, access, store = await configured(runs)
    plan = await service.propose(
        access.actor, access.run_id, replace(scope(remote=True), remote_binding="b" * 64), "冻结远端", "风险"
    )
    reopened = SqlAlchemyDeliveryStore(runs.sessions)
    persisted = await reopened.latest_plan(access.actor.workspace_id, access.run_id)
    assert persisted == plan and persisted.scope.digest == plan.scope.digest
    path = f"{runs.path}/{access.run_id}/plan"
    response = await runs.client.post(
        path,
        headers=runs.headers(),
        json={
            "scope": {
                "repository_id": "test-repo",
                "base_revision": "a" * 40,
                "paths": ["src"],
                "commands": [],
                "actions": ["git.push"],
                "remote_id": "test-remote",
                "target_branch": "main",
                "remote_binding": "forged-by-caller",
            },
            "summary": "伪造连接",
            "risk": "风险",
        },
    )
    assert response.status_code == 422
    assert await store.latest_plan(access.actor.workspace_id, access.run_id) == plan
