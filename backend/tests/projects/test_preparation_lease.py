"""真实事务验证准备材料阶段续约与取消，不等待模型才开始保护租约。"""

import asyncio
from uuid import UUID

import pytest

from tests.api.test_run_execution import PROFILE, ScriptedRuntime
from tests.api.test_run_execution import configured as configured
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.runs.application.execution import RunExecutor, RunLease, RuntimeInput
from weora.modules.runs.application.ports import RunActor, RunRecord
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore


@pytest.mark.anyio
async def test_preparation_renews_and_can_be_cancelled_before_runtime(configured: RunsHarness) -> None:
    created = await configured.client.post(configured.path, json=configured.payload(), headers=configured.headers())
    run_id = UUID(created.json()["id"])
    started, stopped = asyncio.Event(), asyncio.Event()
    renewed = asyncio.Event()

    class Access:
        async def is_active(self, actor: RunActor) -> bool:
            return True

        async def load_input(self, run: RunRecord) -> RuntimeInput:
            started.set()
            try:
                await asyncio.Event().wait()
                return RuntimeInput("", "")
            finally:
                stopped.set()

    class Store(SqlAlchemyExecutionStore):
        renewals = 0

        async def renew(self, lease: RunLease, *, lease_seconds: int) -> bool:
            self.renewals += 1
            if self.renewals >= 2:
                renewed.set()
            return await super().renew(lease, lease_seconds=lease_seconds)

    store, runtime = Store(configured.sessions), ScriptedRuntime()
    executor = RunExecutor(
        store=store, access=Access(), runtime=runtime, profile=PROFILE, lease_seconds=3, heartbeat_seconds=0.03
    )
    task = asyncio.create_task(
        executor.execute(workspace_id=UUID(configured.workspace_id), run_id=run_id, owner="prepare-test")
    )
    async with asyncio.timeout(3):
        await started.wait()
        await renewed.wait()
        cancelled = await configured.client.post(f"{configured.path}/{run_id}/cancel", headers=configured.headers())
        assert cancelled.status_code == 200
        await task
        await stopped.wait()
    detail = (await configured.client.get(f"{configured.path}/{run_id}")).json()
    assert detail["status"] == "CANCELLED" and runtime.calls == []
