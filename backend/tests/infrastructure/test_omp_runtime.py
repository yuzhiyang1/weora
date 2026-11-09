"""真实 OMP 子进程与平台租约、审批及会话检查点的契约验收。"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.delivery.test_run_integration import configure, proposal_scope
from tests.delivery.test_service import scope
from weora.bootstrap.harness import harness_factory
from weora.modules.runs.application.execution import RuntimeFailure, RuntimeInput, RuntimeSession
from weora.modules.runs.application.harness import OMP_RUNTIME, HarnessFactory
from weora.modules.runs.application.ports import ExecutionProfile, RunActor
from weora.modules.tools.application.gateway import ToolScope, ToolSpec

PROFILE = ExecutionProfile(OMP_RUNTIME, "deepseek", "deepseek-flash", 512, 25, 35)


@pytest.fixture
def omp(runs: RunsHarness) -> HarnessFactory:
    root = Path(__file__).resolve().parents[3] / "runtimes" / "omp"
    bun = os.environ.get("WEORA_TEST_OMP_BUN") or shutil.which("bun")
    if not bun or not (root / "node_modules" / "@oh-my-pi" / "pi-agent-core").is_dir():
        pytest.skip("请安装 OMP 依赖和 Bun 1.4.4 后运行")
    version = subprocess.check_output([bun, "--version"], text=True, timeout=5).strip()
    if tuple(int(part) for part in version.split("-", 1)[0].split(".")) < (1, 4, 4):
        pytest.skip("OMP 测试需要 Bun 1.4.4 或更新版本")
    settings = runs.app.state.settings.model_copy(
        update={
            "run_runtime": "pi",
            "omp_bun_executable": bun,
            "omp_runtime_entry": str(root / "tests" / "scripted-model.ts"),
        }
    )
    return harness_factory(settings, PROFILE, "fixture-key")


@pytest.mark.anyio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_omp_approval_revokes_old_lease_and_resumes_checkpoint(
    runs: RunsHarness, omp: HarnessFactory, decision: str
) -> None:
    run_id, gateway, delivery, executions = await configure(runs)
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    lease = await executions.claim(workspace_id=actor.workspace_id, run_id=run_id, owner="omp", lease_seconds=30)
    assert lease
    saved: list[dict[str, object]] = []
    receipts: list[str] = []

    async def checkpoint(messages: list[dict[str, object]], read: list[str]) -> None:
        saved[:] = messages
        receipts.extend(read)

    async def propose(identity: str, name: str, args: dict[str, object]) -> dict[str, object]:
        result = await gateway.invoke(
            ToolScope(run_id, actor, lease.owner, lease.fencing_token, "console"), identity, name, args
        )
        assert result["ok"] is True
        # 与 Worker 一样，提案让租约失效后立即停止内核进程。
        raise RuntimeFailure("RUN_PAUSED")

    proposal = {"scope": proposal_scope(scope()), "summary": "审批记录标记", "risk": "隔离测试"}
    tool = next(item for item in gateway.specifications() if item.name == "plan.propose")
    session = RuntimeSession("omp-approval", checkpoint=checkpoint, initial_input_ids=["first"])
    with pytest.raises(RuntimeFailure, match="RUN_PAUSED"):
        await omp(session, propose).run(
            RuntimeInput("测试审批", json.dumps(proposal, ensure_ascii=False), (tool,)), PROFILE
        )
    assert "first" in receipts
    assert not await executions.authorize_token(actor, run_id, lease.fencing_token, lease.owner)
    plan = await delivery.get_plan(actor, run_id)
    assert plan and plan.status == "PENDING"

    path = f"{runs.path}/{run_id}"
    assert (await runs.client.get(path)).json()["status"] == "WAITING_APPROVAL"
    assert (
        await runs.client.post(f"{path}/plan/{plan.id}/decision", json={"decision": decision}, headers=runs.headers())
    ).status_code == 200
    successor = await executions.claim(
        workspace_id=actor.workspace_id, run_id=run_id, owner="omp-next", lease_seconds=30
    )
    if decision == "reject":
        assert successor is None
        return
    assert successor and successor.fencing_token > lease.fencing_token

    async def unexpected(*_: object) -> dict[str, object]:
        raise AssertionError("恢复不得重放提案")

    resumed = RuntimeSession("omp-approval", messages=saved)
    assert await omp(resumed, unexpected).run(RuntimeInput("测试恢复", "恢复检查"), PROFILE) == "已恢复"


@pytest.mark.anyio
async def test_omp_cancellation_waits_for_process_exit(omp: HarnessFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[asyncio.subprocess.Process] = []
    create = asyncio.create_subprocess_exec

    async def track(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await create(*args, **kwargs)
        if str(args[1]).endswith("scripted-model.ts"):
            launched.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)
    waiting = asyncio.Event()

    async def hold(*_: object) -> dict[str, object]:
        waiting.set()
        await asyncio.Future()
        raise AssertionError("等待应由取消结束")

    tool = ToolSpec("probe.wait", "等候取消", {"type": "object", "properties": {}})
    task = asyncio.create_task(
        omp(RuntimeSession("omp-cancel"), hold).run(RuntimeInput("取消测试", "{}", (tool,)), PROFILE)
    )
    try:
        await asyncio.wait_for(waiting.wait(), timeout=20)
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(launched) == 1 and launched[0].returncode is not None
