"""两套旧内核共用 Python 协议边界的真实进程预算回归。"""

import shutil
from pathlib import Path

import pytest

from weora.modules.runs.application.execution import RuntimeFailure, RuntimeInput
from weora.modules.runs.application.harness import OMP_RUNTIME, PI_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.infrastructure.process_runtime import ProcessRuntime
from weora.modules.tools.application.gateway import ToolSpec


@pytest.mark.anyio
@pytest.mark.parametrize("runtime_id", [PI_RUNTIME, OMP_RUNTIME])
@pytest.mark.parametrize("mode", ["complete", "partial", "unreported", "closing-limit"])
async def test_transport_keeps_bounded_partial_report_after_business_budget(runtime_id: str, mode: str) -> None:
    """第 64 次成功可正常结束；超预算不能执行业务、不能 ready，且只有有限收尾额度。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node 未安装，不能验证真实双向协议")
    calls: list[tuple[str, dict[str, object]]] = []

    async def invoke(_identity: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        return {"ok": True}

    runtime = ProcessRuntime(
        executable=node, runtime_id=runtime_id, providers=frozenset({"deepseek"}),
        entry=Path(__file__).parents[1] / "fixtures" / "pi_budget_protocol.mjs",
        api_key="budget-fixture-key", call_tool=invoke,
    )
    tools = tuple(ToolSpec(name, name, {"type": "object"}) for name in ("probe.read", "orchestrator_report"))
    payload = RuntimeInput("保持完整用户目标", mode, tools)
    profile = ExecutionProfile(runtime_id, "deepseek", "fixture-model", None, 10)
    if mode in {"unreported", "closing-limit"}:
        with pytest.raises(RuntimeFailure, match="TOOL_BUDGET_EXCEEDED"):
            await runtime.run(payload, profile)
    else:
        assert await runtime.run(payload, profile) == ("已读 A，仍须检查 B" if mode == "partial" else "目标全部完成")
    assert sum(name == "probe.read" for name, _ in calls) == 64
    reports = [arguments for name, arguments in calls if name == "orchestrator_report"]
    assert len(reports) == (1 if mode == "partial" else 4 if mode == "closing-limit" else 0)
    assert all(report.get("state") != "ready" for report in reports)
