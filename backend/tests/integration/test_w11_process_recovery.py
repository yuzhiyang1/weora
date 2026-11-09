"""真实 PostgreSQL、独立进程强杀及恢复；确定性 Runtime 不冒充模型联调。"""

import asyncio
import os
import sys
from dataclasses import asdict
from uuid import UUID

import pytest
from sqlalchemy import select, update

from tests.integration.test_approval_recovery_postgres import PostgresHarness, prepared
from tests.integration.test_approval_recovery_postgres import postgres_runs as postgres_runs
from tests.support.crash_worker import PROFILE
from weora.bootstrap.worker import _reconcile_control, _reconcile_tools
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.models import RunEventModel, RunModel
from weora.modules.tools.application.gateway import ToolScope
from weora.modules.tools.infrastructure.store import SqlAlchemyToolStore, ToolInvocationModel

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def child(h: PostgresHarness, run_id: UUID, mode: str) -> asyncio.subprocess.Process:
    """凭据仅经子进程环境传递，不写文件、参数或日志。"""
    env = dict(
        os.environ,
        WEORA_TEST_POSTGRES_URL=h.url,
        WEORA_CRASH_SCHEMA=h.schema,
        WEORA_CRASH_RUN=str(run_id),
        WEORA_CRASH_WORKSPACE=h.runs.workspace_id,
        WEORA_CRASH_MODE=mode,
    )
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-m",
        "tests.support.crash_worker",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def kill_at(h: PostgresHarness, run_id: UUID, mode: str, barrier: str) -> None:
    process = await child(h, run_id, mode)
    try:
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), 20)
        assert line.decode().strip() == barrier, "独立进程未到达指定提交断点"
        process.kill()
        await asyncio.wait_for(process.wait(), 10)
        assert process.returncode != 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def reconcile_in_fresh_process(h: PostgresHarness, run_id: UUID) -> None:
    process = await child(h, run_id, "reconcile")
    out, _ = await asyncio.wait_for(process.communicate(), 20)
    assert process.returncode == 0 and out.strip() == b"RECONCILED"


async def test_killed_worker_does_not_replay_success_or_leave_unknown_tool_running(
    postgres_runs: PostgresHarness,
) -> None:
    h = postgres_runs
    service, _, actor, run_id = await prepared(h)
    async with h.runs.sessions() as session, session.begin():
        await session.execute(update(RunModel).where(RunModel.id == run_id).values(execution_profile=asdict(PROFILE)))
    await kill_at(h, run_id, "execution", "SIDE_EFFECT_COMMITTED")
    # 等待真实 PostgreSQL 时钟的租约自然到期；不更改数据库时间或租约字段。
    await asyncio.sleep(3.2)
    await reconcile_in_fresh_process(h, run_id)
    store = SqlAlchemyExecutionStore(h.runs.sessions)
    async with h.runs.sessions() as session:
        run = await session.get(RunModel, run_id)
        assert run and run.status == "RECOVERY_REQUIRED"
        events = list(await session.scalars(select(RunEventModel).where(RunEventModel.run_id == run_id)))
        assert len(events) == 3
    assert (actor.workspace_id, run_id) not in await store.pending_dispatches()
    assert len(await service.store.artifacts(actor.workspace_id, run_id)) == 2
    scope = ToolScope(run_id, actor, "checkpoint", 0)
    calls = await SqlAlchemyToolStore(h.runs.sessions).list_calls(scope)
    assert {item.call_id: item.status for item in calls} == {
        "succeeded-write": "SUCCEEDED",
        "pending-write": "UNKNOWN",
    }
    assert await store.recover_read_only(actor, run_id)
    lease = await store.claim(
        workspace_id=actor.workspace_id, run_id=run_id, owner="recovered-reader", lease_seconds=30
    )
    assert lease and lease.run.recovery_read_only
    assert not await store.authorize_token(actor, run_id, lease.fencing_token, write=True)
    await reconcile_in_fresh_process(h, run_id)
    assert len(await service.store.artifacts(actor.workspace_id, run_id)) == 2
    assert await store.finish(lease, status="SUCCEEDED", output="只读核对两份已持久证据，未重放写入")


@pytest.mark.parametrize("window", ["proposal", "decision"])
async def test_process_crash_on_approval_commit_window(postgres_runs: PostgresHarness, window: str) -> None:
    h = postgres_runs
    service, _, actor, run_id = await prepared(h)
    store = SqlAlchemyExecutionStore(h.runs.sessions)
    if window == "proposal":
        async with h.runs.sessions() as session, session.begin():
            await session.execute(
                update(RunModel).where(RunModel.id == run_id).values(execution_profile=asdict(PROFILE))
            )
        await kill_at(h, run_id, "proposal", "PROPOSAL_COMMITTED")
        await asyncio.sleep(3.2)
    else:
        from tests.context.test_run_integration import approval_scope

        await service.propose(actor, run_id, approval_scope(), "持久审批", "强杀窗口验证")
        await kill_at(h, run_id, "decision", "DECISION_COMMITTED")
    await reconcile_in_fresh_process(h, run_id)
    detail = (await h.runs.client.get(f"{h.runs.path}/{run_id}")).json()
    assert detail["status"] == ("WAITING_APPROVAL" if window == "proposal" else "QUEUED")
    plan = await service.get_plan(actor, run_id)
    assert plan and plan.status == ("PENDING" if window == "proposal" else "APPROVED")
    sequence = detail["last_sequence"]
    await _reconcile_control(h.runs.app)
    assert (await h.runs.client.get(f"{h.runs.path}/{run_id}")).json()["last_sequence"] == sequence
    assert ((actor.workspace_id, run_id) in await store.pending_dispatches()) == (window == "decision")
    assert await service.store.artifacts(actor.workspace_id, run_id) == []


async def test_tool_compensation_matches_original_attempt_and_preserves_active_reader(
    postgres_runs: PostgresHarness,
) -> None:
    """新租约活跃不能掩盖旧调用，也不能因旧租约中断误终止新只读调用。"""
    h = postgres_runs
    _, _, actor, run_id = await prepared(h)
    execution = SqlAlchemyExecutionStore(h.runs.sessions)
    tools = SqlAlchemyToolStore(h.runs.sessions)
    old = await execution.claim(workspace_id=actor.workspace_id, run_id=run_id, owner="old-worker", lease_seconds=30)
    assert old
    previous = ToolScope(run_id, actor, old.owner, old.fencing_token)
    assert (await tools.reserve(previous, "old-call", "fixture.write", "a" * 64, {}, max_calls=24))[0] == "NEW"
    assert await _reconcile_tools(h.runs.app) == 0
    assert await execution.transition(actor, run_id, "RECOVERY_REQUIRED")
    assert await execution.recover_read_only(actor, run_id)
    new = await execution.claim(workspace_id=actor.workspace_id, run_id=run_id, owner="new-reader", lease_seconds=30)
    assert new
    current = ToolScope(run_id, actor, new.owner, new.fencing_token, "recovery")
    assert (await tools.reserve(current, "new-call", "fixture.read", "b" * 64, {}, max_calls=24))[0] == "NEW"
    assert await _reconcile_tools(h.runs.app) == 1
    assert {item.call_id: item.status for item in await tools.list_calls(current)} == {
        "old-call": "UNKNOWN",
        "new-call": "RUNNING",
    }
    await tools.finish(previous, "old-call", result={"ok": True}, status="SUCCEEDED", error_code=None)
    assert (await tools.reserve(previous, "old-call", "fixture.write", "a" * 64, {}, max_calls=24))[
        0
    ] == "TOOL_RECOVERY_REQUIRED"
    await tools.finish(current, "new-call", result={"ok": True}, status="SUCCEEDED", error_code=None)
    assert await _reconcile_tools(h.runs.app) == 0


async def test_legacy_tool_without_attempt_identity_waits_for_inactive_run(postgres_runs: PostgresHarness) -> None:
    """0012升级前的可空标识不推测执行者；活跃时保守等待，终态后才记未知。"""
    h = postgres_runs
    _, _, actor, run_id = await prepared(h)
    execution = SqlAlchemyExecutionStore(h.runs.sessions)
    lease = await execution.claim(
        workspace_id=actor.workspace_id, run_id=run_id, owner="legacy-worker", lease_seconds=30
    )
    assert lease
    tools = SqlAlchemyToolStore(h.runs.sessions)
    scope = ToolScope(run_id, actor, lease.owner, lease.fencing_token)
    await tools.reserve(scope, "legacy-call", "fixture.write", "c" * 64, {}, max_calls=24)
    async with h.runs.sessions() as session, session.begin():
        await session.execute(
            update(ToolInvocationModel)
            .where(ToolInvocationModel.run_id == run_id)
            .values(lease_owner=None, fencing_token=None)
        )
    assert await _reconcile_tools(h.runs.app) == 0
    assert await execution.finish(lease, status="FAILED", failure_code="WORKER_INTERRUPTED")
    assert await _reconcile_tools(h.runs.app) == 1
    assert await _reconcile_tools(h.runs.app) == 0
    assert (await tools.list_calls(scope))[0].status == "UNKNOWN"
