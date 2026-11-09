"""显式启用的两轮真实模型验收；业务状态仅写 fixture 的隔离 SQLite。"""

import os
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_continuations import actor, create, follow
from weora.bootstrap.run_profile import execution_profile
from weora.bootstrap.settings import AppSettings
from weora.modules.agents.application.service import AgentRegistryService
from weora.modules.identity.application.service import IdentityService
from weora.modules.runs.application.execution import RunExecutor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.pi_runtime import PiTextRuntime
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_two_real_pi_runs_continue_same_task(runs: RunsHarness) -> None:
    if os.environ.get("WEORA_TEST_LIVE_PI") != "1":
        pytest.skip("真实模型验收需要显式 WEORA_TEST_LIVE_PI=1")
    # 只从既有后端配置取得模型凭据，不打印设置或对开发数据库建连。
    settings = AppSettings()
    profile = execution_profile(settings)
    assert profile is not None and settings.deepseek_api_key and settings.pi_runtime_entry
    service = cast(RunService, runs.app.state.run_service)
    service._execution_profile = profile
    runtime = PiTextRuntime(node_executable=settings.pi_node_executable, entry=Path(settings.pi_runtime_entry),
                            api_key=settings.deepseek_api_key.get_secret_value())
    executor = RunExecutor(
        store=SqlAlchemyExecutionStore(runs.sessions),
        access=ModulesExecutionAccess(
            identity=cast(IdentityService, runs.app.state.identity_service),
            agents=cast(AgentRegistryService, runs.app.state.agent_registry_service),
            uow_factory=lambda: SqlAlchemyRunsUnitOfWork(runs.sessions),
        ),
        runtime=runtime, profile=profile,
    )
    code = "青竹-" + uuid4().hex[:8]
    first = await create(runs, f"本任务的测试代号是 {code}。请记住，只回答“已记录”。")
    assert await executor.execute(workspace_id=UUID(runs.workspace_id), run_id=UUID(str(first["id"])), owner="live-1")
    original = await service.get(actor=actor(runs), workspace_id=UUID(runs.workspace_id),
                                 run_id=UUID(str(first["id"])))
    assert original.run.status == "SUCCEEDED", original.run.failure_code
    item = await follow(runs, first["id"], "我们这项任务的测试代号是什么？仅回答代号。")
    second_id = UUID(str(item["run_id"]))
    assert await executor.execute(workspace_id=UUID(runs.workspace_id), run_id=second_id, owner="live-2")
    continued = await service.get(actor=actor(runs), workspace_id=UUID(runs.workspace_id), run_id=second_id)
    assert continued.task.id == original.task.id and continued.run.id != original.run.id
    assert continued.run.status == "SUCCEEDED", continued.run.failure_code
    assert code in (continued.run.output_text or "")
