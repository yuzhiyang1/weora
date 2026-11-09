"""真实 Node 子进程协议验收；模型结果明确由夹具提供。"""

import asyncio
import shutil
from pathlib import Path

import pytest

from weora.modules.runs.application.execution import RuntimeFailure, RuntimeInput
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.application.session_delivery import session_history_window
from weora.modules.runs.infrastructure.pi_runtime import PiSession, PiTextRuntime
from weora.modules.tools.application.gateway import ToolSpec


def test_model_window_keeps_steering_with_its_original_tool_turn() -> None:
    """插话不能成为起点，防止留下缺少调用记录的工具结果。"""
    messages: list[dict[str, object]] = [
        {"role": "user", "content": "x" * 100000, "weoraInputIds": []},
        {"role": "assistant", "content": [{"type": "toolCall", "id": "old-call"}]},
        {"role": "user", "content": "插话", "weoraInputId": "followup"},
        {"role": "toolResult", "toolCallId": "old-call", "content": "结果"},
    ]
    prefix, window = session_history_window(messages)
    assert prefix == messages and window == []
    assert session_history_window(messages[-2:]) == ([], messages[-2:])


@pytest.mark.anyio
@pytest.mark.parametrize("steps", [30, 65])
async def test_long_tool_flow_crosses_real_parent_child_transport(steps: int) -> None:
    """必须穿过真实父进程，防止只调整 SDK 而漏掉运输层的旧上限。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node")
    calls: list[str] = []

    async def invoke(identity: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append(identity)
        return {"ok": True}

    runtime = PiTextRuntime(
        node_executable=node, entry=Path("tests/fixtures/pi_session.mjs"), api_key="test-only-key",
        call_tool=invoke, session=PiSession("long-protocol-session"),
    )
    profile = ExecutionProfile("pi-agent-core@1.3.0", "deepseek", "test-model", 100, 10)
    payload = RuntimeInput("系统", f"protocol-tools-{steps}", (ToolSpec("context.read", "读取", {}),))
    if steps > 64:
        with pytest.raises(RuntimeFailure, match="TOOL_BUDGET_EXCEEDED"):
            await runtime.run(payload, profile)
    else:
        assert await runtime.run(payload, profile) == "长流程协议通过"
    assert len(calls) == min(steps, 64)


@pytest.mark.anyio
async def test_model_history_window_preserves_the_full_durable_journal() -> None:
    """真实子进程收到短窗口，平台仍保存完整旧日志与新增检查点。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node")
    history: list[dict[str, object]] = [
        {"role": "user", "content": "旧事实" * 40000, "weoraInputIds": []},
        {"role": "assistant", "content": "已处理"},
        {"role": "user", "content": "最新轮次", "weoraInputIds": []},
        {"role": "assistant", "content": "当前事实"},
    ]
    saved: list[list[dict[str, object]]] = []

    async def checkpoint(messages: list[dict[str, object]], read: list[str]) -> None:
        saved.append(messages)

    runtime = PiTextRuntime(
        node_executable=node,
        entry=Path("tests/fixtures/pi_session.mjs"),
        api_key="test-only-key",
        session=PiSession("windowed-session", messages=history, checkpoint=checkpoint),
    )
    profile = ExecutionProfile(
        runtime_id="pi-agent-core@1.3.0", provider="deepseek", model_id="test-model", max_tokens=100, timeout_seconds=10
    )
    assert await runtime.run(RuntimeInput("系统", "window-history"), profile) == "模型窗口与完整审计通过"
    assert saved == [history + [{"role": "assistant", "content": "新检查点"}]]


@pytest.mark.anyio
@pytest.mark.parametrize("instruction,expected", [("many-checkpoints", 20), ("too-many-checkpoints", 256)])
async def test_repeated_session_snapshots_have_a_separate_bounded_budget(instruction: str, expected: int) -> None:
    """大网页日志重发可超过 2 MB，同时拒绝无限检查点输出。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node")
    saved: list[int] = []

    async def checkpoint(messages: list[dict[str, object]], read: list[str]) -> None:
        saved.append(len(str(messages[0]["content"])))

    runtime = PiTextRuntime(
        node_executable=node,
        entry=Path("tests/fixtures/pi_session.mjs"),
        api_key="test-only-key",
        session=PiSession("large-browser-session", checkpoint=checkpoint),
    )
    profile = ExecutionProfile(
        runtime_id="pi-agent-core@1.3.0", provider="deepseek", model_id="test-model", max_tokens=100, timeout_seconds=10
    )
    if instruction == "too-many-checkpoints":
        with pytest.raises(RuntimeFailure, match="RUNTIME_PROTOCOL_OUTPUT_LIMIT"):
            await runtime.run(RuntimeInput("系统", instruction), profile)
    else:
        assert await runtime.run(RuntimeInput("系统", instruction), profile) == "有界会话检查点通过"
    assert saved == [140000] * expected


@pytest.mark.anyio
async def test_failed_input_authorization_stops_runtime_without_waiting_for_model() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node")

    async def pull() -> list[tuple[str, str]]:
        raise RuntimeFailure("ACCESS_REVOKED")

    async def waiting_tool(identity: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        await asyncio.Future()
        return {}

    runtime = PiTextRuntime(
        node_executable=node,
        entry=Path("tests/fixtures/pi_session.mjs"),
        api_key="test-only-key",
        session=PiSession("revoked-session", pull=pull),
        call_tool=waiting_tool,
    )
    profile = ExecutionProfile(
        runtime_id="pi-agent-core@1.3.0", provider="deepseek", model_id="test-model", max_tokens=100, timeout_seconds=30
    )
    execution = asyncio.create_task(
        runtime.run(RuntimeInput("系统", "原始任务", (ToolSpec("context.read", "读取", {}),)), profile)
    )
    try:
        completed, _ = await asyncio.wait({execution}, timeout=3)
        assert completed, "输入授权失败必须立即中断读取/工具等待，不能等完整模型时限"
        with pytest.raises(RuntimeFailure, match="ACCESS_REVOKED"):
            await execution
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.anyio
async def test_runtime_accepts_steering_while_platform_tool_is_waiting() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node")
    accepted: list[str] = []
    saved: list[list[str]] = []
    waiting = False

    async def pull() -> list[tuple[str, str]]:
        return [("followup", "后续更正")] if waiting else []

    async def on_accepted(identity: str) -> None:
        accepted.append(identity)

    async def checkpoint(messages: list[dict[str, object]], read: list[str]) -> None:
        assert messages[0]["content"] == "保存的检查点"
        saved.append(read)

    async def tool(identity: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        nonlocal waiting
        waiting = True
        await asyncio.sleep(0.6)
        return {"ok": True}

    runtime = PiTextRuntime(
        node_executable=node,
        entry=Path("tests/fixtures/pi_session.mjs"),
        api_key="test-only-key",
        session=PiSession("durable-worker", pull=pull, accepted=on_accepted, checkpoint=checkpoint),
        call_tool=tool,
    )
    profile = ExecutionProfile(
        runtime_id="pi-agent-core@1.3.0", provider="deepseek", model_id="test-model", max_tokens=100, timeout_seconds=10
    )
    text = await runtime.run(RuntimeInput("系统", "原始任务", (ToolSpec("context.read", "读取", {}),)), profile)
    assert text == "连续会话插话通过"
    assert accepted == ["followup"] and saved == [["followup"]]
