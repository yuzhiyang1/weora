"""Pi 保留已有协议和部署入口。"""

from pathlib import Path

from weora.modules.runs.application.execution import RuntimeSession as PiSession
from weora.modules.runs.application.harness import PI_RUNTIME, ToolCaller
from weora.modules.runs.infrastructure.process_runtime import ProcessRuntime

__all__ = ["PiSession", "PiTextRuntime"]


class PiTextRuntime(ProcessRuntime):
    def __init__(
        self,
        *,
        node_executable: str,
        entry: Path,
        api_key: str,
        call_tool: ToolCaller | None = None,
        session: PiSession | None = None,
    ) -> None:
        super().__init__(
            executable=node_executable,
            runtime_id=PI_RUNTIME,
            providers=frozenset({"deepseek", "openai", "anthropic", "google", "moonshotai-cn"}),
            entry=entry,
            api_key=api_key,
            call_tool=call_tool,
            session=session,
            watch_parent=True,
        )
