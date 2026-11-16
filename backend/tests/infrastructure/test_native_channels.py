"""原生渠道接入回归：入口选择、来源撤权与真实输入回执不依赖模型猜测。"""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import aiohttp
import pytest

from tests.api.test_projects import project, repository
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.native_runtime import native_runtime
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.projects.domain import RepositoryArchive
from weora.modules.projects.service import ProjectService
from weora.modules.runs.application.execution import RunLease, RuntimeFailure, RuntimeInput
from weora.modules.runs.application.harness import OMP_RPC_RUNTIME, PI_RPC_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile, RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.domain.errors import RunNotFoundError, RunProjectSelectionError
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.native_model import NativeModelProxy
from weora.modules.runs.infrastructure.native_rpc import NativeCodingRuntime, NativeRpcConnection, NativeSandbox


@pytest.mark.anyio
@pytest.mark.parametrize("runtime_id", [PI_RPC_RUNTIME, OMP_RPC_RUNTIME])
async def test_group_project_uses_same_native_profile_and_keeps_source_gate(
    runs: RunsHarness, monkeypatch: pytest.MonkeyPatch, runtime_id: str,
) -> None:
    service = cast(RunService, runs.app.state.run_service)
    selected = ExecutionProfile(runtime_id, "deepseek", "deepseek-chat", None, 60, 600)
    profile = AsyncMock(return_value=selected)
    allowed = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_private_project_profile", profile)
    monkeypatch.setattr(service, "_channel_project_authorizer", allowed)
    p = await project(runs, "原生群任务")
    repo = await repository(runs, p["id"], "source")
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    origin: dict[str, object] = {"source_kind": "feishu", "chat_id": "group", "message_id": "original"}
    detail, _ = await service.create(
        actor=actor, workspace_id=actor.workspace_id, agent_id=UUID(runs.agent_id),
        agent_version="0.1.0", instruction="原始任务保持完整", idempotency_key="native-group",
        request_id="native-group", origin_metadata=origin,
        project_id=UUID(str(p["id"])), repository_ids=(UUID(str(repo["id"])),),
    )
    assert detail.run.execution_profile == selected
    assert detail.task.audience == "group" and detail.task.instruction == "原始任务保持完整"
    profile.assert_awaited_once_with(actor)
    assert allowed.await_count == 1
    assert await service.native_session_anchor(actor, detail.run.id) == detail.run.id
    allowed.return_value = False
    with pytest.raises(RunProjectSelectionError):
        await service.create(
            actor=actor, workspace_id=actor.workspace_id, agent_id=UUID(runs.agent_id),
            agent_version="0.1.0", instruction="未经授权不启动", idempotency_key="denied-group",
            request_id="denied-group", origin_metadata=origin,
            project_id=UUID(str(p["id"])), repository_ids=(UUID(str(repo["id"])),),
        )


@pytest.mark.anyio
async def test_native_group_access_checks_current_source_authorization(runs: RunsHarness) -> None:
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers("native-access"))
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    detail = await runs.app.state.run_service.get(
        actor=actor, workspace_id=actor.workspace_id, run_id=UUID(created.json()["id"]),
    )
    detail = replace(detail, task=replace(detail.task, audience="group"))
    getter = AsyncMock(return_value=detail)
    run_service = cast(RunService, SimpleNamespace(get=getter, access_metadata=AsyncMock(return_value=detail)))
    executions = cast(SqlAlchemyExecutionStore, SimpleNamespace(owns_lease=AsyncMock(return_value=True)))
    runtime = native_runtime(
        IsolatedAppSettings().model_copy(update={"native_rpc_image": "native:test"}),
        RunLease(detail.run, "owner", 1), run_service,
        cast(ProjectService, SimpleNamespace()), executions,
        cast(DeliveryService, SimpleNamespace()), "test-key", None, AsyncMock(return_value={"ok": True}),
    )
    assert await runtime.check_access()
    assert await runtime.load_repositories() == {}
    getter.side_effect = RunNotFoundError()
    assert not await runtime.check_access()


@pytest.mark.anyio
@pytest.mark.parametrize("omp", [False, True])
async def test_native_initial_inputs_are_acknowledged_before_report(omp: bool) -> None:
    seen: list[list[str]] = []

    async def acknowledged(identities: list[str]) -> None:
        seen.append(identities)

    profile = ExecutionProfile(OMP_RPC_RUNTIME if omp else PI_RPC_RUNTIME, "deepseek", "deepseek-chat", None, 60)
    conn = NativeRpcConnection(
        cast(asyncio.subprocess.Process, SimpleNamespace()), omp=omp,
        model=NativeModelProxy(profile, "test-key"), http=cast(aiohttp.ClientSession, None),
        call=AsyncMock(return_value={"ok": True}), progress=AsyncMock(), diff=AsyncMock(),
        tools={}, repositories={}, inputs_read=acknowledged,
    )
    conn.initial_input = ("未经改写的完整任务", ("input-original", "input-followup"))
    await conn._rpc({"type": "message_end", "message": {"role": "user", "content": "无关文本"}})
    assert not seen
    event: dict[str, object] = {"type": "message_end", "message": {
        "role": "user", "content": [{"type": "text", "text": "未经改写的完整任务"}],
    }}
    await conn._rpc(event)
    await conn._rpc(event)
    assert seen == [["input-original", "input-followup"]]

@pytest.mark.anyio
async def test_native_platform_budget_requires_partial_and_keeps_report() -> None:
    """平台配额不足不会封掉报告通道，也不能用 ready 掩盖尚未完成的工作。"""
    call = AsyncMock(side_effect=[
        {"ok": False, "error": {"code": "TOOL_BUDGET_EXCEEDED"}},
        {"ok": True, "status": "pending_run_validation"},
    ])
    profile = ExecutionProfile(PI_RPC_RUNTIME, "deepseek", "deepseek-chat", None, 60)
    conn = NativeRpcConnection(
        cast(asyncio.subprocess.Process, SimpleNamespace()), omp=False,
        model=NativeModelProxy(profile, "test-key"), http=cast(aiohttp.ClientSession, None),
        call=call, progress=AsyncMock(), diff=AsyncMock(),
        tools={"read_document": "lark.cli.exec", "report": "orchestrator_report"}, repositories={},
    )
    exhausted = await conn._tool("read_document", "budget-call", {})
    assert exhausted["ok"] is False and "partial" in str(exhausted["instruction"])
    rejected = await conn._tool("report", "premature-ready", {"state": "ready", "summary": "完成"})
    assert rejected["ok"] is False and call.await_count == 1
    accepted = await conn._tool("report", "partial", {
        "state": "partial", "summary": "已核对全科，Mini 尚待核实", "remaining_work": ["核对 Mini"],
    })
    assert accepted["ok"] is True and call.await_count == 2
    conn._message_end({"role": "assistant", "content": "", "stopReason": "stop"})
    assert NativeCodingRuntime._output(conn) == "已核对全科，Mini 尚待核实"


@pytest.mark.parametrize("stop_reason,code", [("length", "MODEL_TRUNCATED"), ("stop", "EMPTY_RESPONSE")])
def test_native_truncated_or_empty_final_response_is_not_success(stop_reason: str, code: str) -> None:
    """早先旁白及被截断的结论均不能冒充完整的最终交付。"""
    profile = ExecutionProfile(PI_RPC_RUNTIME, "deepseek", "deepseek-chat", None, 60)
    conn = NativeRpcConnection(
        cast(asyncio.subprocess.Process, SimpleNamespace()), omp=False,
        model=NativeModelProxy(profile, "test-key"), http=cast(aiohttp.ClientSession, None),
        call=AsyncMock(), progress=AsyncMock(), diff=AsyncMock(), tools={}, repositories={},
    )
    conn._message_end({"role": "assistant", "content": "我先检查", "stopReason": "stop"})
    conn._message_end({"role": "assistant", "content": "", "stopReason": stop_reason})
    with pytest.raises(RuntimeFailure, match=code):
        NativeCodingRuntime._output(conn)


@pytest.mark.anyio
@pytest.mark.parametrize("oversized", [False, True])
async def test_native_archive_preserves_all_repositories_with_total_budget(oversized: bool) -> None:
    """大于旧 64MiB 的仓库仍进入原任务；总量越界清理归档，不能静默删仓继续。"""
    run = SimpleNamespace(
        id=uuid4(), task_id=uuid4(), created_by=uuid4(), workspace_id=uuid4(),
        recovery_read_only=False, execution_profile=None,
    )
    repositories = [uuid4() for _ in range(5)]
    snapshot = {"repositories": [{"id": str(repository)} for repository in repositories]}
    detail = SimpleNamespace(task=SimpleNamespace(project_context=snapshot))
    service = SimpleNamespace(
        get=AsyncMock(return_value=detail), access_metadata=AsyncMock(return_value=detail),
        native_session_anchor=AsyncMock(return_value=run.id),
    )
    archives = []
    for index in range(5):
        path = MagicMock(spec=Path)
        path.stat.return_value = SimpleNamespace(st_size=(200 if oversized and index == 4 else 100) * 1024 * 1024)
        archives.append(RepositoryArchive(path, "frozen-revision", "digest"))
    exporter = AsyncMock(side_effect=archives)
    projects = SimpleNamespace(authorize_snapshot=AsyncMock(return_value=True), prepare_archive=exporter)
    runtime = native_runtime(
        IsolatedAppSettings().model_copy(update={"native_rpc_image": "native:test"}),
        cast(RunLease, SimpleNamespace(run=run)), cast(RunService, service), cast(ProjectService, projects),
        cast(SqlAlchemyExecutionStore, SimpleNamespace(owns_lease=AsyncMock(return_value=True))),
        cast(DeliveryService, SimpleNamespace()), "test-key", None, AsyncMock(),
    )
    if oversized:
        with pytest.raises(RuntimeFailure, match="NATIVE_REPOSITORY_TOO_LARGE"):
            await runtime.load_repositories()
        for archive in archives:
            cast(MagicMock, archive.path).unlink.assert_called_once_with(missing_ok=True)
    else:
        assert list(await runtime.load_repositories()) == repositories
    assert [call.kwargs["max_bytes"] for call in exporter.await_args_list] == [
        value * 1024 * 1024 for value in (512, 412, 312, 212, 112)
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("restricted", [False, True])
async def test_native_volume_reuses_only_repository_with_safe_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restricted: bool,
) -> None:
    """实际容器启动参数只能挂载净化后的卷，不能把曾含凭据的卷交给原生引擎。"""
    path = tmp_path / "source.tar"
    path.write_bytes(b"fixture")
    archive = RepositoryArchive(path, "a" * 40, "sha", restricted_paths_removed=restricted)
    sandbox = NativeSandbox(("docker",), "fixture", uuid4(), uuid4())
    start = AsyncMock(side_effect=OSError)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    runtime = NativeCodingRuntime(
        sandbox, key="", session=None, call=AsyncMock(), load_repositories=AsyncMock(return_value={uuid4(): archive}),
        check_access=AsyncMock(return_value=True), progress=AsyncMock(), diff=AsyncMock(),
    )
    with pytest.raises(RuntimeFailure, match="NATIVE_SANDBOX_UNAVAILABLE"):
        await runtime.run(RuntimeInput("", "核对工作目录", ()), ExecutionProfile(
            PI_RPC_RUNTIME, "deepseek", "deepseek-chat", None, 60, 600,
        ))
    args = start.await_args.args
    mount = next(arg for arg in args if "target=/weora-session" in arg)
    assert (f"source={sandbox.volume}," in mount) is not restricted
    assert not path.exists()
