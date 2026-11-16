"""独立复核原生主流程的跨会话隔离与完整资产继承。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import aiohttp
import pytest

from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.coordination.test_engine import setup
from tests.coordination.test_hosting_poll import MemoryCoordination
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.native_runtime import native_runtime
from weora.modules.agents.application.ports import RuntimeSkillPackage
from weora.modules.coordination.activities import RunActivities
from weora.modules.coordination.ports import CoordinationStore
from weora.modules.coordination.worker_children import ChildContext, WorkerChildren
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.projects.service import ProjectService
from weora.modules.runs.application.execution import RunLease, RuntimeFailure, RuntimeInput, RuntimeSession
from weora.modules.runs.application.harness import PI_RPC_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile, RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.native_model import NativeModelProxy
from weora.modules.runs.infrastructure.native_rpc import NativeCodingRuntime, NativeRpcConnection, NativeSandbox


@pytest.mark.anyio
async def test_native_parent_and_child_do_not_share_session_volume(runs: RunsHarness) -> None:
    """并发子会话必须有独立原生日志，不能复用父容器名或父 JSONL 文件。"""
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers("review-child-session"))
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    detail = await runs.app.state.run_service.get(
        actor=actor, workspace_id=actor.workspace_id, run_id=UUID(created.json()["id"]),
    )
    profile = ExecutionProfile(PI_RPC_RUNTIME, "deepseek", "deepseek-chat", None, 60)
    lease = RunLease(replace(detail.run, execution_profile=profile), "review-owner", 1)
    settings = IsolatedAppSettings().model_copy(update={"native_rpc_image": "native:test"})
    dependencies = (
        settings, lease, cast(RunService, SimpleNamespace()), cast(ProjectService, SimpleNamespace()),
        cast(SqlAlchemyExecutionStore, SimpleNamespace()), cast(DeliveryService, SimpleNamespace()), "fixture",
    )
    parent = native_runtime(*dependencies, RuntimeSession("parent-session"), AsyncMock())
    child = native_runtime(*dependencies, RuntimeSession("child-session", isolated_workspace=True), AsyncMock())
    assert parent.sandbox.volume != child.sandbox.volume
    # 失权后新历史不能继续挂载旧日志；同代次重新执行则必须稳定续接。
    renewed = native_runtime(
        *dependencies, RuntimeSession("parent-session", native_history_epoch="1"), AsyncMock(),
    )
    resumed = native_runtime(
        *dependencies, RuntimeSession("parent-session", native_history_epoch="1"), AsyncMock(),
    )
    assert renewed.sandbox.volume != parent.sandbox.volume
    assert renewed.sandbox.volume == resumed.sandbox.volume
    # 状态查询不可复用可写挂载；日志需另存而不是把主卷改回可写。
    observation = native_runtime(
        *dependencies, RuntimeSession("parent-session", read_only=True), AsyncMock(),
    )
    invocation = observation.sandbox.invocation("review", "review-owner", 60)
    mounts = [invocation[index + 1] for index, value in enumerate(invocation[:-1]) if value == "--mount"]
    assert any(item.endswith("target=/weora-session,readonly") for item in mounts)
    assert any(item.endswith("target=/weora-readonly-session") for item in mounts)


@pytest.mark.anyio
async def test_native_child_receives_complete_published_skill_archive() -> None:
    """父任务已加载的脚本和参考资料不能在 delegate 构造 RuntimeInput 时消失。"""
    _, state = setup()
    workspace_id = uuid4()
    profile = ExecutionProfile(PI_RPC_RUNTIME, "deepseek", "deepseek-chat", None, 60)
    package = RuntimeSkillPackage("fixture-skill", b"published-archive")
    payload = RuntimeInput("system", "original", skill_packages=(package,))
    context = ChildContext(workspace_id, state.config.id, str(uuid4()), "review-owner", payload, profile)
    captured: list[RuntimeInput] = []

    class CaptureRuntime:
        async def run(self, request: RuntimeInput, selected: ExecutionProfile) -> str:
            captured.append(request)
            assert selected == profile
            return "已完成委派调查"

    children = WorkerChildren(
        cast(CoordinationStore, MemoryCoordination(state)), context,
        RunActivities(context.run_id, context.owner, AsyncMock()),
        AsyncMock(return_value=state), lambda _: None, AsyncMock(),
        lambda _session, _call: CaptureRuntime(), (),
    )
    assert (await children.delegate("child", "使用已绑定 Skill 核查"))["ok"] is True
    assert captured[0].skill_packages == (package,)


@pytest.mark.anyio
async def test_revoked_native_history_is_not_sent_to_model() -> None:
    """平台已清正文但进程尚存时，下一次模型请求也必须被历史代次闸门阻断。"""
    session = RuntimeSession("review-revoked", pull=AsyncMock(side_effect=RuntimeFailure("SESSION_HISTORY_REVOKED")))
    runtime = NativeCodingRuntime(
        NativeSandbox(("docker",), "native:test", uuid4(), uuid4()), key="fixture", session=session,
        call=AsyncMock(), load_repositories=AsyncMock(), check_access=AsyncMock(return_value=True),
        progress=AsyncMock(), diff=AsyncMock(),
    )
    stream = AsyncMock()
    model = cast(NativeModelProxy, SimpleNamespace(stream=stream))
    connection = NativeRpcConnection(
        cast(asyncio.subprocess.Process, SimpleNamespace()), omp=False, model=model,
        http=cast(aiohttp.ClientSession, None), call=AsyncMock(), progress=AsyncMock(), diff=AsyncMock(),
        tools={}, repositories={}, authorize=runtime._authorize,
    )
    with pytest.raises(RuntimeFailure, match="SESSION_HISTORY_REVOKED"):
        await connection._serve({"channel": "model", "id": "revoked-request"})
    stream.assert_not_awaited()
