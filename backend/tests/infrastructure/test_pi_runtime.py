"""真实 Node 子进程验证自有协议、凭据隔离和取消；不是 DeepSeek 联调。"""

import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from weora.modules.runs.application.execution import RuntimeFailure, RuntimeInput
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.infrastructure.pi_runtime import PiTextRuntime
from weora.modules.tools.infrastructure.repository import READ_FILE

PROFILE = ExecutionProfile("pi-agent-core@1.3.0", "deepseek", "explicit-test-model", 128, 3)
ENTRY = Path(__file__).parents[1] / "fixtures" / "pi_protocol.mjs"
TEST_KEY = "dummy-not-a-real-api-key"


@pytest.fixture
def runtime() -> PiTextRuntime:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node 未安装，未执行真实子进程协议验证")
    return PiTextRuntime(node_executable=node, entry=ENTRY, api_key=TEST_KEY)


@pytest.mark.anyio
async def test_protocol_process_returns_text_without_key(runtime: PiTextRuntime) -> None:
    assert await runtime.run(RuntimeInput("系统提示", "正常请求"), PROFILE) == "协议测试回答。"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "instruction,code",
    [
        ("invalid", "RUNTIME_PROTOCOL_INVALID"),
        ("error", "MODEL_FAILED"),
        ("unknown-error", "MODEL_FAILED"),
        ("echo-key", "MODEL_RESPONSE_REDACTED"),
        ("large", "RUNTIME_PROTOCOL_OUTPUT_LIMIT"),
        ("exit", "RUNTIME_PROCESS_FAILED"),
    ],
)
async def test_protocol_rejects_invalid_or_sensitive_output(
    runtime: PiTextRuntime, instruction: str, code: str
) -> None:
    with pytest.raises(RuntimeFailure) as error:
        await runtime.run(RuntimeInput("系统提示", instruction), PROFILE)
    assert error.value.code == code
    assert TEST_KEY not in str(error.value)


@pytest.mark.anyio
async def test_process_environment_does_not_inherit_personal_credentials(
    runtime: PiTextRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NODE_OPTIONS", "--not-a-valid-node-flag")
    monkeypatch.setenv("OPENAI_API_KEY", "personal-key-must-not-leak")
    monkeypatch.setenv("HOME", "personal-home-must-not-load")
    result = json.loads(await runtime.run(RuntimeInput("系统提示", "environment"), PROFILE))
    assert result == {"nodeOptions": None, "otherKey": None, "home": None, "credentialReceivedViaStdin": True}


@pytest.mark.anyio
async def test_cancellation_waits_for_owned_process_exit(
    runtime: PiTextRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = asyncio.create_subprocess_exec
    children: list[asyncio.subprocess.Process] = []
    started = asyncio.Event()

    async def capture(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        # Any 仅用于标准库进程工厂的完整参数转发，不进入运行契约。
        process = await original(*args, **kwargs)
        if len(args) > 1 and args[1] == str(ENTRY.resolve()):
            children.append(process)
            started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(runtime.run(RuntimeInput("系统提示", "hold"), PROFILE))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert len(children) == 1
    assert children[0].returncode is not None


@pytest.mark.anyio
async def test_process_deadline_is_not_success(runtime: PiTextRuntime) -> None:
    with pytest.raises(RuntimeFailure, match="MODEL_TIMEOUT"):
        await runtime.run(RuntimeInput("系统提示", "hold"), replace(PROFILE, timeout_seconds=1))


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["deepseek", "openai", "anthropic", "google", "moonshotai-cn"])
async def test_installed_pi_protocol_rejects_unknown_model_before_network(provider: str) -> None:
    """使用实际构建的 Pi 入口，但选不在目录中的模型，保证不发送外部请求。"""
    entry = Path(__file__).parents[3] / "runtimes" / "pi" / "dist" / "index.js"
    node = shutil.which("node")
    if node is None or not entry.is_file():
        pytest.skip("Pi 尚未构建，未验证实际 SDK 子进程协议")
    runtime = PiTextRuntime(node_executable=node, entry=entry, api_key=TEST_KEY)
    with pytest.raises(RuntimeFailure, match="MODEL_UNKNOWN"):
        await runtime.run(
            RuntimeInput("系统提示", "不应触发网络请求"),
            replace(
                PROFILE,
                provider=provider,
                model_id="weora-test-model-does-not-exist",
                # 这里验证目录拒绝，不测试 SDK 冷启动速度；避免高负载下先命中无关时限。
                timeout_seconds=60,
                execution_timeout_seconds=60,
            ),
        )


@pytest.mark.anyio
async def test_bidirectional_protocol_routes_only_declared_tools() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node 未配置，不能验证工具进程协议")
    calls: list[tuple[str, str, dict[str, object]]] = []

    async def invoke(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((call_id, name, arguments))
        return {"ok": True, "content": "授权文件"}

    runtime = PiTextRuntime(
        node_executable=node, entry=ENTRY.parent / "pi_tools_protocol.mjs", api_key=TEST_KEY, call_tool=invoke
    )
    assert await runtime.run(RuntimeInput("系统", "read", (READ_FILE,)), PROFILE) == "工具事实已接收。"
    assert calls == [("call-1", "repo.file.read", {"path": "README.md"})]
    with pytest.raises(RuntimeFailure, match="RUNTIME_PROTOCOL_INVALID"):
        await runtime.run(RuntimeInput("系统", "forged", (READ_FILE,)), PROFILE)
    assert len(calls) == 1


@pytest.mark.anyio
async def test_slow_tool_does_not_consume_model_budget() -> None:
    """真实协议子进程等待 Tool 超过模型时限，仍可在整体预算内继续返回。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node 未配置")

    async def slow_tool(_id: str, _name: str, _args: dict[str, object]) -> dict[str, object]:
        await asyncio.sleep(1.2)
        return {"ok": True, "content": "真实长工具回执"}

    runtime = PiTextRuntime(
        node_executable=node, entry=ENTRY.parent / "pi_tools_protocol.mjs", api_key=TEST_KEY, call_tool=slow_tool
    )
    assert (
        await runtime.run(
            RuntimeInput("系统", "read", (READ_FILE,)), replace(PROFILE, timeout_seconds=1, execution_timeout_seconds=5)
        )
        == "工具事实已接收。"
    )


@pytest.mark.anyio
async def test_separate_wall_budget_still_stops_unfinished_runtime(runtime: PiTextRuntime) -> None:
    with pytest.raises(RuntimeFailure, match="RUN_TIMEOUT"):
        await runtime.run(
            RuntimeInput("系统", "hold"), replace(PROFILE, timeout_seconds=5, execution_timeout_seconds=1)
        )
