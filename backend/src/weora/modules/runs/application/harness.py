"""受部署控制的 Harness 标识与运行工厂契约。"""

from collections.abc import Awaitable, Callable
from typing import Literal

from weora.modules.runs.application.execution import RuntimeSession, TextRuntime

HarnessId = Literal["pi", "oh-my-pi"]
PI_RUNTIME = "pi-agent-core@1.3.0"
OMP_RUNTIME = "oh-my-pi@18.16.0"
ToolCaller = Callable[[str, str, dict[str, object]], Awaitable[dict[str, object]]]
HarnessFactory = Callable[[RuntimeSession | None, ToolCaller], TextRuntime]
