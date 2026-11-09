"""只有明确未执行的投递可以自动重试；模型失败、未知动作和取消不能重投整个回合。"""

import asyncio
import shutil
from pathlib import Path

import pytest

from weora.modules.runs.application.execution import RuntimeFailure, RuntimeInput
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.application.session_delivery import deliver_session
from weora.modules.runs.infrastructure.pi_runtime import PiSession, PiTextRuntime
from weora.modules.tools.application.gateway import ToolSpec


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code,expected", [("SESSION_DELIVERY_FAILED", 4), ("MODEL_FAILED", 1), ("RECOVERY_REQUIRED", 1)]
)
async def test_known_delivery_retries_three_times_but_other_failures_do_not(code: str, expected: int) -> None:
    attempts = 0
    statuses: list[dict[str, object] | None] = []

    async def fail() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeFailure(code)

    async def status(value: dict[str, object] | None) -> None:
        statuses.append(value)

    with pytest.raises(RuntimeFailure, match=code):
        await deliver_session(fail, status, base_delay_seconds=0)
    assert attempts == expected
    if expected == 4:
        assert statuses == [{"attempt": n, "delay_ms": 0} for n in (1, 2, 3)] + [None]


@pytest.mark.anyio
async def test_cancel_interrupts_delivery_backoff_without_a_second_attempt() -> None:
    waiting = asyncio.Event()
    attempts = 0

    async def fail() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeFailure("SESSION_DELIVERY_FAILED")

    async def status(value: dict[str, object] | None) -> None:
        if value:
            waiting.set()

    task = asyncio.create_task(deliver_session(fail, status))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts == 1


@pytest.mark.anyio
@pytest.mark.parametrize("called_tool", [False, True])
async def test_real_node_disconnect_is_retryable_only_before_tools(tmp_path: Path, called_tool: bool) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node")
    script = tmp_path / "disconnect.mjs"
    script.write_text(
        """
// 接收初始消息后主动断开；测试父进程能否区分工具边界。
import { createInterface } from 'node:readline';
const reader = createInterface({ input: process.stdin });
let first = true;
for await (const line of reader) {
  if (first) {
    first = false;
    if (CALLED_TOOL) process.stdout.write(JSON.stringify({
      protocol: 'weora.runtime.v2', type: 'tool_call', id: 'tool', name: 'read', arguments: {}
    }) + '\\n');
    else process.exit(1);
  } else process.exit(1);
}
""".replace("CALLED_TOOL", "true" if called_tool else "false"),
        encoding="utf-8",
    )
    calls: list[str] = []

    async def tool(identity: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append(identity)
        return {"ok": True}

    runtime = PiTextRuntime(
        node_executable=node, entry=script, api_key="test-only", session=PiSession("test"), call_tool=tool
    )
    with pytest.raises(RuntimeFailure) as error:
        await runtime.run(
            RuntimeInput("系统", "任务", (ToolSpec("read", "读取", {}),)),
            ExecutionProfile("pi-agent-core@1.3.0", "deepseek", "test-model", 100, 10),
        )
    assert error.value.code == ("RUNTIME_PROCESS_FAILED" if called_tool else "SESSION_DELIVERY_FAILED")
    assert len(calls) == int(called_tool)
